"""Who owns purging: the takeover, its protection and its release."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .compat import (
    check_compat,
    dialect_problem,
    oldest_row_age_days,
)
from .const import (
    ALL_ISSUES,
    DOMAIN,
    ISSUE_DIALECT,
    ISSUE_INFERRED,
    ISSUE_PROTECTING,
    ISSUE_SAFETY_NET,
    ISSUE_TAKEOVER_BLOCKED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .issues import delete_issues

_LOGGER = logging.getLogger(__name__)


def _release_text(keep_days: Any) -> str:
    return (
        "Home Assistant's own purge is active again. At its next nightly run (about 04:12) it "
        f"deletes every state, event and statistic older than {keep_days} days. Thinned "
        "history older than that will be gone."
    )


async def async_release_for_removal(hass: HomeAssistant) -> None:
    """The integration is being removed: hand purging back and say what that means."""
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    startup = hass.data.get(DOMAIN, {}).get("startup")
    try:
        instance = get_instance(hass)
    except KeyError, RuntimeError:
        instance = None
    keep_days = startup["keep_days"] if startup else getattr(instance, "keep_days", "?")
    if instance is not None and startup:
        instance.auto_purge = startup["auto_purge"]
        instance.keep_days = startup["keep_days"]
    delete_issues(hass, *ALL_ISSUES)
    if data.get("owned_retention"):
        persistent_notification.async_create(
            hass,
            "Sediment was removed. " + _release_text(keep_days),
            title="Sediment was removed",
            notification_id="sediment_removed",
        )
    await store.async_remove()


class TakeoverMixin:
    """Takeover of the recorder's purge, for the manager."""

    # ------------------------------------------------------------ takeover
    async def _release(self, prefix: str) -> None:
        """Hand purging back to Home Assistant and clear the ownership."""
        assert self.takeover is not None
        was_owned = self.owned
        self.takeover.restore()
        self.data.pop("owned_retention", None)
        await self.store.async_save(deepcopy(self.data))
        if was_owned:
            persistent_notification.async_create(
                self.hass,
                prefix + _release_text(self.takeover.startup["keep_days"]),
                title="Sediment released purging",
                notification_id="sediment_released",
            )

    async def _protect(self) -> None:
        """The guard failed while the takeover was applied: do not let the recorder purge."""
        assert self.takeover is not None
        instance = get_instance(self.hass)
        age = None
        try:
            age = await instance.async_add_executor_job(oldest_row_age_days, instance)
        except Exception:
            _LOGGER.exception("Sediment could not read the age of the oldest row")
        horizon = max(self.settings.history_days, self.data.get("real_horizon_days", 0))
        how = self.takeover.protect(age, horizon)
        self.hass.data.setdefault(DOMAIN, {})["protected"] = True
        _LOGGER.warning("Sediment keeps the recorder from purging (%s)", how)

    async def _infer_ownership(self) -> None:
        """Ownership is stored in Sediment's own store. If that store is lost, the data shows.

        With the takeover ticked and rows older than the recorder's configured keep days
        plus two, something other than the recorder's purge has kept them: treat the
        retention as owned for protection (never for anything that deletes).
        """
        self._inferred_owned = False
        if not self.settings.takeover or self.owned or self.takeover is None:
            return
        instance = get_instance(self.hass)
        try:
            age = await instance.async_add_executor_job(oldest_row_age_days, instance)
        except Exception:
            _LOGGER.exception("Sediment could not read the age of the oldest row")
            return
        keep = self.takeover.startup.get("keep_days") or 0
        if age is not None and age > keep + 2:
            self._inferred_owned = True
            self._inferred_age = age
            _LOGGER.warning(
                "Sediment finds states %.0f days old, older than the recorder's %s days: "
                "treating the retention as owned and keeping the recorder from purging",
                age,
                keep,
            )

    def _safety_net_issue(self) -> None:
        assert self.takeover is not None
        startup = self.takeover.startup
        if self.settings.takeover and startup.get("auto_purge"):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_SAFETY_NET,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SAFETY_NET,
            )
        else:
            delete_issues(self.hass, ISSUE_SAFETY_NET)

    async def async_apply_takeover(self, at_startup: bool = True) -> list[str]:
        """Apply the takeover (see `_apply_takeover`) and keep the inference notice current."""
        problems = await self._apply_takeover(at_startup)
        if self._inferred_owned and not self.owned and self.settings.takeover:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_INFERRED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_INFERRED,
                translation_placeholders={
                    "days": f"{self._inferred_age:.0f}",
                    "keep_days": str(self.takeover.startup.get("keep_days")),
                },
            )
        else:
            delete_issues(self.hass, ISSUE_INFERRED)
        return problems

    async def _apply_takeover(self, at_startup: bool = True) -> list[str]:
        """Engage, hold or release the takeover according to the options.

        Returns the problems found. Rules:

        * a failed guard never hands purging back; if the takeover was applied before, the
          recorder is kept from purging and a repair issue says so;
        * an applied takeover stays applied when dry run is switched on (thinning pauses);
        * it is released only when the box is unticked (the options flow asked for
          confirmation first), never from a job.
        """
        assert self.takeover is not None
        s = self.settings
        instance = get_instance(self.hass)
        self._safety_net_issue()
        if not s.takeover:
            delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING, ISSUE_DIALECT)
            if self.owned and at_startup:
                await self._release("The takeover was switched off. ")
            elif at_startup and self.hass.data.get(DOMAIN, {}).pop("protected", False):
                # protection that was only inferred: the user has confirmed giving it up
                self.takeover.restore()
                persistent_notification.async_create(
                    self.hass,
                    "Sediment stopped protecting old states. "
                    + _release_text(self.takeover.startup["keep_days"]),
                    title="Sediment released purging",
                    notification_id="sediment_released",
                )
            return []
        if reason := dialect_problem(instance):
            _LOGGER.warning("Sediment does not touch this database: %s", reason)
            delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_DIALECT,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DIALECT,
                translation_placeholders={"reason": reason},
            )
            if self.owned or self._inferred_owned:
                await self._protect()
            return [reason]
        delete_issues(self.hass, ISSUE_DIALECT)
        problems = check_compat(instance)
        if problems:
            joined = "; ".join(problems)
            if self.owned or self._inferred_owned:
                _LOGGER.warning(
                    "Sediment cannot work with this recorder and keeps it from purging: %s",
                    joined,
                )
                await self._protect()
                delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED)
                key = ISSUE_PROTECTING
            else:
                _LOGGER.warning(
                    "Sediment does not take over purging, the recorder is untouched: %s", joined
                )
                delete_issues(self.hass, ISSUE_PROTECTING)
                key = ISSUE_TAKEOVER_BLOCKED
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                key,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders={"reasons": joined},
            )
            return problems
        delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING)
        if s.dry_run and not self.owned:
            # Nothing has been thinned yet and a dry run changes nothing: the guard has
            # passed, the recorder keeps purging as before until dry run is switched off.
            # Unless the data shows that thinned history exists (the store was lost).
            if self._inferred_owned:
                await self._protect()
            return []
        horizon = s.history_days
        if s.dry_run:
            # a dry run never shortens what the last real run allowed
            horizon = max(horizon, int(self.data.get("real_horizon_days", 0)))
        else:
            self.data["real_horizon_days"] = float(horizon)
        self.takeover.engage(horizon)
        if not self.owned:
            self.data["owned_retention"] = {
                "since": dt_util.utcnow().isoformat(),
                "keep_days": self.takeover.startup["keep_days"],
            }
            await self.store.async_save(deepcopy(self.data))
        return []
