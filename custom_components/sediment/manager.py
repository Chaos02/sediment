"""Event loop side of Sediment: settings, schedule, takeover, jobs, reports, estimate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import logging
import os
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import solver
from .compat import (
    Takeover,
    check_compat,
    dialect_problem,
    oldest_row_age_days,
    snapshot_values,
)
from .const import (
    ALL_ISSUES,
    BACKLOG_LIMIT,
    BACKOFF_GIVE_UP_SECONDS,
    BACKOFF_SECONDS,
    CONF_COARSE_HORIZON_DAYS,
    CONF_CUSTOM_TIERS,
    CONF_DRY_RUN,
    CONF_FLAP_COLLAPSE,
    CONF_HISTORY_DAYS,
    CONF_RAW_DAYS,
    CONF_RESOLUTION,
    CONF_RULES,
    CONF_SIZE_CAP_GB,
    CONF_TAKEOVER,
    DEFAULT_COARSE_HORIZON_DAYS,
    DEFAULT_DRY_RUN,
    DEFAULT_FLAP_COLLAPSE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_RAW_DAYS,
    DEFAULT_RESOLUTION,
    DEFAULT_TAKEOVER,
    DOMAIN,
    ISSUE_DIALECT,
    ISSUE_INFERRED,
    ISSUE_JOB_FAILING,
    ISSUE_PROTECTING,
    ISSUE_SAFETY_NET,
    ISSUE_TAKEOVER_BLOCKED,
    JOB_FAIL_ISSUE_AFTER,
    JOB_HOUR,
    JOB_MINUTE,
    PROFILE_MAP_FILE,
    REPORT_FILE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .profiles import (
    EntityMeta,
    Schedule,
    build_schedules,
    parse_rules,
    profile_map_hash,
    profile_signature,
    resolve,
)

_LOGGER = logging.getLogger(__name__)

ROWSTATS_TTL = 3600.0
SAVE_INTERVAL = 30.0
UNLOAD_WAIT_SECONDS = 30.0
TOP_ENTITIES = 25


@dataclass
class Settings:
    """The options of the config entry with defaults filled in."""

    history_days: int = DEFAULT_HISTORY_DAYS
    resolution: str = DEFAULT_RESOLUTION
    custom_tiers: list[dict] | None = None
    size_cap_gb: float | None = None
    rules_text: str = ""
    flap_collapse: bool = DEFAULT_FLAP_COLLAPSE
    takeover: bool = DEFAULT_TAKEOVER
    raw_days: int = DEFAULT_RAW_DAYS
    dry_run: bool = DEFAULT_DRY_RUN
    coarse_horizon_days: int = DEFAULT_COARSE_HORIZON_DAYS

    @classmethod
    def from_options(cls, opts: dict[str, Any]) -> Settings:
        cap = opts.get(CONF_SIZE_CAP_GB)
        return cls(
            history_days=int(opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)),
            resolution=opts.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
            custom_tiers=opts.get(CONF_CUSTOM_TIERS),
            size_cap_gb=float(cap) if cap else None,
            rules_text=opts.get(CONF_RULES, ""),
            flap_collapse=bool(opts.get(CONF_FLAP_COLLAPSE, DEFAULT_FLAP_COLLAPSE)),
            takeover=bool(opts.get(CONF_TAKEOVER, DEFAULT_TAKEOVER)),
            raw_days=int(opts.get(CONF_RAW_DAYS, DEFAULT_RAW_DAYS)),
            dry_run=bool(opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)),
            coarse_horizon_days=int(
                opts.get(CONF_COARSE_HORIZON_DAYS, DEFAULT_COARSE_HORIZON_DAYS)
            ),
        )

    def schedules(self) -> dict[str, Schedule]:
        return build_schedules(
            self.resolution,
            self.custom_tiers,
            self.raw_days,
            self.history_days,
            self.coarse_horizon_days,
        )


# ---------------------------------------------------------------- helpers shared with the flow
def build_meta(hass: HomeAssistant) -> dict[str, EntityMeta]:
    """What rules can match: device class, integration, state class."""
    meta: dict[str, EntityMeta] = {}
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        meta[ent.entity_id] = EntityMeta(
            device_class=ent.device_class or ent.original_device_class,
            integration=ent.platform,
        )
    for st in hass.states.async_all():
        old = meta.get(st.entity_id, EntityMeta())
        meta[st.entity_id] = EntityMeta(
            device_class=st.attributes.get("device_class") or old.device_class,
            integration=old.integration,
            state_class=st.attributes.get("state_class"),
        )
    return meta


def sqlite_db_path(db_url: str) -> str | None:
    """File path of a SQLite recorder URL, else None."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix) or ":memory:" in db_url:
        return None
    return db_url[len(prefix) :].split("?", 1)[0] or None


def db_size_bytes(instance: Any) -> float | None:
    """Size of the recorder database file (with its WAL), SQLite only."""
    path = sqlite_db_path(getattr(instance, "db_url", "") or "")
    if not path or not os.path.exists(path):
        return None
    total = os.path.getsize(path)
    for suffix in ("-wal",):
        if os.path.exists(path + suffix):
            total += os.path.getsize(path + suffix)
    return float(total)


async def async_get_rowstats(hass: HomeAssistant, raw_days: float, force: bool = False) -> dict:
    """Rows per entity over the raw window; cached for an hour."""
    from .recorder_bridge import collect_rowstats

    cache = hass.data.setdefault(DOMAIN, {}).setdefault("rowstats", {})
    now = time.time()
    if (
        not force
        and cache.get("data")
        and now - cache["ts"] < ROWSTATS_TTL
        and cache.get("raw_days") == raw_days
    ):
        return cache["data"]
    instance = get_instance(hass)
    data = await instance.async_add_executor_job(collect_rowstats, instance, now, raw_days)
    cache.update({"data": data, "ts": now, "raw_days": raw_days})
    return data


async def async_estimate(
    hass: HomeAssistant,
    settings: Settings,
    measured: dict[str, dict[str, float]] | None = None,
) -> solver.Estimate:
    """Projected database size for these settings, from the recorder's real numbers."""
    from .recorder_bridge import aggregate_by_class_profile

    rs = await async_get_rowstats(hass, settings.raw_days)
    meta = build_meta(hass)
    rules, _errors = parse_rules(settings.rules_text)
    states = hass.states

    def classify(eid: str) -> str:
        st = states.get(eid)
        m = meta.get(eid)
        return solver.guess_class(eid, st.state if st else None, m.state_class if m else None)

    def profile_of(eid: str) -> str:
        return resolve(eid, meta.get(eid), rules)[0]

    rows_per_day = aggregate_by_class_profile(rs, classify, profile_of)
    instance = get_instance(hass)
    inputs = solver.Inputs(
        rows_per_day=rows_per_day,
        schedules=settings.schedules(),
        raw_days=settings.raw_days,
        horizon_days=settings.history_days,
        db_size_bytes=db_size_bytes(instance),
        rows_in_db=float(rs["rows"]),
        stats_rows_per_day=float(rs["stats_rows_per_day"]),
        measured_shares=measured,
    )
    return solver.estimate(inputs)


# ---------------------------------------------------------------- the manager
def _delete_issues(hass: HomeAssistant, *issue_ids: str) -> None:
    for issue_id in issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


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
    _delete_issues(hass, *ALL_ISSUES)
    if data.get("owned_retention"):
        persistent_notification.async_create(
            hass,
            "Sediment was removed. " + _release_text(keep_days),
            title="Sediment was removed",
            notification_id="sediment_removed",
        )
    await store.async_remove()


def build_profile_map(
    settings: Settings,
    data: dict[str, Any],
    meta: dict[str, EntityMeta],
    entity_ids: list[str],
) -> dict[str, tuple[str, bool | None]]:
    """The profile each entity gets right now, as the nightly job resolves it.

    An entity whose metadata is missing keeps its stored profile, as in the job.
    """
    rules, _errors = parse_rules(settings.rules_text)
    stored = data.get("profile_map") or {}
    out: dict[str, tuple[str, bool | None]] = {}
    for eid in entity_ids:
        got = resolve(eid, meta.get(eid), rules)
        if eid not in meta and eid in stored:
            profile, _, flap = stored[eid].partition("|")
            got = (profile, {"True": True, "False": False}.get(flap))
        out[eid] = got
    return out


async def async_retention_owned(hass: HomeAssistant, entry: Any) -> bool:
    """Does thinned history (stored or inferred) depend on Sediment holding purging off?

    Decided from the store and the data, so it also works while the entry is not loaded.
    """
    manager = getattr(entry, "runtime_data", None)
    if isinstance(manager, SedimentManager):
        return manager.owned or manager._inferred_owned
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    if (await store.async_load() or {}).get("owned_retention"):
        return True
    if not entry.options.get(CONF_TAKEOVER):
        return False
    try:
        instance = get_instance(hass)
    except KeyError, RuntimeError:
        return False
    startup = hass.data.get(DOMAIN, {}).get("startup") or snapshot_values(instance)
    try:
        age = await instance.async_add_executor_job(oldest_row_age_days, instance)
    except Exception:
        return False
    return age is not None and age > (startup.get("keep_days") or 0) + 2


class SedimentManager:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = Settings.from_options(dict(entry.options))
        self.store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {}
        self.takeover: Takeover | None = None
        self.running = False
        self.job: Any = None
        self.last_report: dict[str, Any] | None = None
        self.db_size: float | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task] = set()
        self._last_save = 0.0
        self._closing = False
        self._inferred_owned = False
        self._inferred_age = 0.0
        self._run_task: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def owned(self) -> bool:
        """Sediment has applied the takeover and the stored history depends on it."""
        return bool(self.data.get("owned_retention"))

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        instance = get_instance(self.hass)
        domain = self.hass.data.setdefault(DOMAIN, {})
        # The recorder's configured values, taken before Sediment first changes them. A
        # reload must not mistake Sediment's own values for the originals.
        startup = domain.setdefault("startup", snapshot_values(instance))
        self.takeover = Takeover(instance, startup)
        # the advice does not depend on anything that can fail, so it comes first
        self._safety_net_issue()
        self.data = await self.store.async_load() or {}
        for key in ("applied", "entities", "progress", "quarantine", "fail_counts"):
            self.data.setdefault(key, {})
        await self._infer_ownership()
        await self.async_apply_takeover()
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._nightly, hour=JOB_HOUR, minute=JOB_MINUTE, second=0
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._refresh_tick, timedelta(minutes=30))
        )
        self._spawn(self._initial_refresh())

    async def async_abort(self) -> None:
        """Setup failed half way: remove the timers and tasks so no zombie manager is left."""
        self._closing = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def async_unload(self) -> None:
        """Stop. The recorder keeps Sediment's settings while the takeover is applied.

        A reload (options change, restart) sets them again at once. Disabling the entry
        keeps them too, until the next restart. Handing purging back is a deliberate act:
        unticking the box after a confirmation, or removing the integration.
        """
        self._closing = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.job is not None:
            self.job.cancelled = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        # a job that is mid-step keeps mutating the stored state until its step returns
        try:
            await asyncio.wait_for(self._idle.wait(), UNLOAD_WAIT_SECONDS)
        except TimeoutError:
            _LOGGER.warning("Sediment job did not stop in %s s, cancelling it", UNLOAD_WAIT_SECONDS)
            if self._run_task is not None:
                self._run_task.cancel()
            _delete_issues(self.hass, *ALL_ISSUES)
            return  # the stored state may still be changing; do not copy it now
        if self.entry.disabled_by is not None and self.owned:
            persistent_notification.async_create(
                self.hass,
                "Sediment was disabled. Its thinned history is still protected: the "
                "recorder's own purge stays off until Home Assistant restarts. After a "
                "restart the recorder runs with its configured values and deletes everything "
                f"older than {self.takeover.startup['keep_days']} days at its next nightly "
                "run, unless you set `recorder: auto_purge: false` in configuration.yaml. "
                "Enabling Sediment again restores the protection.",
                title="Sediment was disabled",
                notification_id="sediment_disabled",
            )
        _delete_issues(self.hass, *ALL_ISSUES)
        await self.store.async_save(deepcopy(self.data))

    def _spawn(self, coro: Any) -> None:
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            _delete_issues(self.hass, ISSUE_SAFETY_NET)

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
            _delete_issues(self.hass, ISSUE_INFERRED)
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
            _delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING, ISSUE_DIALECT)
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
            _delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING)
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
        _delete_issues(self.hass, ISSUE_DIALECT)
        problems = check_compat(instance)
        if problems:
            joined = "; ".join(problems)
            if self.owned or self._inferred_owned:
                _LOGGER.warning(
                    "Sediment cannot work with this recorder and keeps it from purging: %s",
                    joined,
                )
                await self._protect()
                _delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED)
                key = ISSUE_PROTECTING
            else:
                _LOGGER.warning(
                    "Sediment does not take over purging, the recorder is untouched: %s", joined
                )
                _delete_issues(self.hass, ISSUE_PROTECTING)
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
        _delete_issues(self.hass, ISSUE_TAKEOVER_BLOCKED, ISSUE_PROTECTING)
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

    # ------------------------------------------------------------ listeners (sensors)
    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ------------------------------------------------------------ refresh (sizes, estimate)
    async def _initial_refresh(self) -> None:
        instance = get_instance(self.hass)
        await instance.async_db_ready
        await self.async_refresh()
        # The projection comes from the nightly job and is kept in the store. While Home
        # Assistant is still starting, entities have no state yet and would be classified
        # wrongly, so only a reload with changed options computes it here.
        if self.hass.state is CoreState.running and self.data.get("projected_options") != dict(
            self.entry.options
        ):
            await self.async_update_projection()
            await self.store.async_save(deepcopy(self.data))

    async def _refresh_tick(self, _now: datetime) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> None:
        """Update the DB size."""
        instance = get_instance(self.hass)
        self.db_size = await self.hass.async_add_executor_job(db_size_bytes, instance)
        self._notify()

    async def async_update_projection(self) -> None:
        """Compute the projection into self.data. The caller saves the store."""
        try:
            est = await async_estimate(self.hass, self.settings, self.data.get("measured"))
        except Exception:
            _LOGGER.exception("Sediment could not compute the size projection")
            return
        self.data["projected_gb"] = round(est.total_gb, 2)
        self.data["projected_options"] = dict(self.entry.options)
        self._notify()

    async def async_export_profile_map(self) -> dict[str, Any]:
        """Write <config>/sediment/profile_map.json for tools/merge_history.py."""
        instance = get_instance(self.hass)

        def ids(inst: Any) -> list[str]:
            from .recorder_bridge import states_meta_entity_ids

            return states_meta_entity_ids(inst)

        db_ids = await instance.async_add_executor_job(ids, instance)
        meta = build_meta(self.hass)
        entity_ids = sorted(set(db_ids) | set(meta))
        assignments = build_profile_map(self.settings, self.data, meta, entity_ids)
        signature = self._profile_sig()
        document = {
            "format": "sediment-profile-map",
            "version": 1,
            "signature": signature,
            "map_hash": profile_map_hash(signature, assignments),
            "entities": {e: {"profile": p, "flap": f} for e, (p, f) in assignments.items()},
        }

        def write() -> str:
            path = self.hass.config.path(PROFILE_MAP_FILE)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(document, fh)
            os.replace(path + ".tmp", path)
            return path

        path = await self.hass.async_add_executor_job(write)
        return {
            "path": path,
            "entities": len(assignments),
            "signature": signature,
            "map_hash": document["map_hash"],
        }

    async def async_import_history(self, path: str, dry_run: bool = True) -> dict[str, Any]:
        """Import older history from a file made by tools/merge_history.py."""
        from .importer import async_import_history

        return await async_import_history(self, path, dry_run)

    # ------------------------------------------------------------ quarantine
    async def async_clear_quarantine(self) -> int:
        """Let skipped entity-days be tried again. Returns how many were released."""
        if self.running:
            raise HomeAssistantError("Sediment is running a job, try again when it is done")
        quarantine = self.data["quarantine"]
        count = len(quarantine)
        for key in quarantine:
            day = key.split("|", 1)[0]
            for profile in ("default", "coarse"):
                self.data["applied"].pop(f"{day}|{profile}", None)
        quarantine.clear()
        self.data["fail_counts"].clear()
        await self.store.async_save(deepcopy(self.data))
        return count

    # ------------------------------------------------------------ nightly job
    async def _nightly(self, _now: datetime) -> None:
        await self.async_run(None, now_dt=dt_util.now())

    async def async_run(
        self, dry_run: bool | None = None, now_dt: datetime | None = None
    ) -> dict[str, Any] | None:
        """Run the job once. Returns the report, or None if it did not run."""
        if self._closing:
            return None
        if self.running:
            _LOGGER.warning("Sediment job already running")
            return None
        assert self.takeover is not None
        instance = get_instance(self.hass)
        s = self.settings
        dry = s.dry_run if dry_run is None else dry_run
        problems = check_compat(instance, require_sqlite=not dry)
        if problems:
            _LOGGER.warning(
                "Sediment job skipped, recorder not compatible: %s", "; ".join(problems)
            )
            # keeps the issue current and the recorder from purging; never releases
            await self.async_apply_takeover(at_startup=False)
            return None
        try:
            from .recorder_bridge import Job, JobConfig, run_on_recorder
        except Exception:
            _LOGGER.exception("Sediment cannot load its recorder bridge")
            return None
        now_dt = now_dt or dt_util.now()
        rules, _errors = parse_rules(s.rules_text)
        active = self.takeover.active
        horizon = float(s.history_days)
        if dry and active:
            # a dry run never shortens what the last real run allowed
            horizon = max(horizon, float(self.data.get("real_horizon_days", 0)))
        cfg = JobConfig(
            dry_run=dry,
            now=time.time(),
            raw_days=float(s.raw_days),
            horizon_days=horizon,
            coarse_horizon_days=float(s.coarse_horizon_days),
            schedules=s.schedules(),
            rules=rules,
            flap_default=s.flap_collapse,
            takeover=s.takeover and (dry or active),
            housekeeping=active,
            profile_sig=self._profile_sig(),
            cleanup_days=float(self.takeover.startup.get("keep_days") or 0),
            meta=build_meta(self.hass),
            repack=bool(
                active
                and self.takeover.startup.get("auto_repack")
                and self._is_second_sunday(now_dt)
            ),
        )
        self.running = True
        self._idle.clear()
        self._run_task = asyncio.current_task()
        job = Job(cfg, self.data)
        self.job = job
        started = time.monotonic()
        waited = 0.0
        try:
            while True:
                if self._closing or job.cancelled:
                    job.cancelled = True
                    break
                if instance.backlog > BACKLOG_LIMIT:
                    await asyncio.sleep(BACKOFF_SECONDS)
                    waited += BACKOFF_SECONDS
                    if waited > BACKOFF_GIVE_UP_SECONDS:
                        job.stats.errors.append("recorder backlog did not drain, job stopped")
                        job.cancelled = True
                        break
                    continue
                fut = run_on_recorder(instance, job.step)
                try:
                    done = await asyncio.shield(fut)
                except asyncio.CancelledError:
                    job.cancelled = True
                    # the step is running on the recorder thread; give it a while to
                    # finish, but never wait for a recorder that is gone
                    with suppress(Exception):
                        await asyncio.wait([fut], timeout=UNLOAD_WAIT_SECONDS)
                    raise
                if time.monotonic() - self._last_save > SAVE_INTERVAL:
                    await self.store.async_save(deepcopy(self.data))
                    self._last_save = time.monotonic()
                if done:
                    break
        finally:
            self.running = False
            self.job = None
            self._run_task = None
            self._idle.set()
        if dry and job.stats.share_counts:
            measured = solver.measured_shares_from_counts(job.stats.share_counts)
            if measured:
                self.data["measured"] = measured
        if not self._closing:
            await self.async_update_projection()
        report = self._make_report(job, dry, time.monotonic() - started, waited)
        self._note_result(job.stats.errors)
        if not dry and active and not job.cancelled:
            self.data["real_horizon_days"] = float(s.history_days)
        self.data["last_run"] = {
            "at": dt_util.utcnow().isoformat(),
            "mode": report["mode"],
            "rows_deleted": job.stats.rows_deleted,
            "entity_days": job.stats.entity_days,
            "errors": job.stats.errors,
        }
        await self.store.async_save(deepcopy(self.data))
        self.last_report = report
        await self.hass.async_add_executor_job(self._write_report, report)
        if dry:
            persistent_notification.async_create(
                self.hass,
                self._notification_text(report),
                title="Sediment dry run",
                notification_id="sediment_report",
            )
        if not self._closing:
            await self.async_refresh()
        return report

    def _note_result(self, errors: list[str]) -> None:
        """Count consecutive failed jobs; three in a row raise a repair issue."""
        if not errors:
            self.data["failed_jobs"] = 0
            _delete_issues(self.hass, ISSUE_JOB_FAILING)
            return
        self.data["failed_jobs"] = self.data.get("failed_jobs", 0) + 1
        if self.data["failed_jobs"] >= JOB_FAIL_ISSUE_AFTER and self.settings.takeover:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_JOB_FAILING,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_JOB_FAILING,
                translation_placeholders={
                    "count": str(self.data["failed_jobs"]),
                    "errors": "; ".join(errors[:3]),
                },
            )

    def _profile_sig(self) -> str:
        """Hash of everything that decides which profile an entity gets."""
        s = self.settings
        return profile_signature(
            s.rules_text, s.resolution, s.custom_tiers, s.flap_collapse, s.coarse_horizon_days
        )

    @staticmethod
    def _is_second_sunday(now: datetime) -> bool:
        return now.weekday() == 6 and 8 <= now.day <= 14

    def _make_report(self, job: Any, dry: bool, seconds: float, waited: float) -> dict[str, Any]:
        st = job.stats
        stats = asdict(st)
        top = sorted(st.top_entities.items(), key=lambda kv: -kv[1][0])[:TOP_ENTITIES]
        stats["top_entities"] = {eid: {"raw_rows": v[0], "kept_rows": v[1]} for eid, v in top}
        measured = solver.measured_shares_from_counts(st.share_counts)
        days = max(1, st.days_simulated)
        report: dict[str, Any] = {
            "version": 1,
            "generated": dt_util.utcnow().isoformat(),
            "mode": "dry_run" if dry else "run",
            "seconds": round(seconds, 1),
            "backoff_seconds": waited,
            "settings": {
                "history_days": self.settings.history_days,
                "resolution": self.settings.resolution,
                "raw_days": self.settings.raw_days,
                "takeover": self.settings.takeover,
                "takeover_applied": bool(self.takeover and self.takeover.active),
                "flapping_collapse": self.settings.flap_collapse,
                "size_cap_gb": self.settings.size_cap_gb,
            },
            "stats": stats,
            "quarantine": dict(list(self.data.get("quarantine", {}).items())[:50]),
            "measured_kept_share": measured,
            "raw_rows_per_day_by_class_and_profile": {
                k: round(v / days, 1) for k, v in st.raw_rows_by_class_profile.items()
            },
        }
        if self.data.get("projected_gb") is not None:
            report["projected_gb"] = self.data["projected_gb"]
        return report

    @staticmethod
    def _notification_text(report: dict[str, Any]) -> str:
        st = report["stats"]
        lines = [
            f"Simulated {st['days_simulated']} day(s), {st['entity_days']} entity-days, "
            f"{int(st['rows_seen']):,} rows. Nothing was thinned and no entity was dropped.",
        ]
        if st.get("housekeeping"):
            lines.append(
                "The takeover is applied, so the history length and the cleanup of events, "
                f"short-term statistics and runs did run: {st['horizon_rows']:,} old state "
                f"rows, {st['events_deleted']:,} events and {st['short_term_deleted']:,} "
                "short-term statistics were removed."
            )
        shares = report.get("measured_kept_share") or {}
        for cls, by_bucket in shares.items():
            parts = ", ".join(
                f"{float(b) / 60:g} min: {s * 100:.1f} %"
                for b, s in sorted(by_bucket.items(), key=lambda kv: float(kv[0]))
            )
            lines.append(f"Kept share of {cls} rows: {parts}")
        if st["dropped_rows"]:
            lines.append(f"{st['dropped_rows']:,} rows of drop entities would be deleted.")
        if st.get("reprofiled_entities"):
            lines.append(
                f"{st['reprofiled_entities']} entities changed profile since the last run; "
                "their old days would be thinned again."
            )
        lines.append("Full report: sediment/last_report.json in the config directory.")
        return "\n\n".join(lines)

    def _write_report(self, report: dict[str, Any]) -> None:
        path = self.hass.config.path(REPORT_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        os.replace(tmp, path)

    # ------------------------------------------------------------ sensor values
    @property
    def db_size_gb(self) -> float | None:
        return None if self.db_size is None else round(self.db_size / 1e9, 3)

    @property
    def projected_gb(self) -> float | None:
        return self.data.get("projected_gb")
