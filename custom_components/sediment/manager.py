"""Event loop side of Sediment: settings, schedule, takeover, jobs, reports, estimate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
import os
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import solver
from .compat import (
    Takeover,
    check_compat,
    oldest_row_age_days,
    snapshot_values,
)
from .const import (
    ALL_ISSUES,
    BACKLOG_LIMIT,
    BACKOFF_GIVE_UP_SECONDS,
    BACKOFF_SECONDS,
    CONF_TAKEOVER,
    DOMAIN,
    JOB_HOUR,
    JOB_MINUTE,
    PROFILE_MAP_FILE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .estimate import (
    async_estimate,
    build_meta,
    build_profile_map,
    db_size_bytes,
)
from .issues import delete_issues
from .manager_report import ReportMixin
from .manager_takeover import TakeoverMixin
from .pace import note_unfinished, run_log_line
from .profiles import (
    parse_rules,
    profile_map_hash,
    profile_signature,
)
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

SAVE_INTERVAL = 30.0
UNLOAD_WAIT_SECONDS = 30.0


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


class SedimentManager(TakeoverMixin, ReportMixin):
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
        # the issues are removed on unload; the last real run's finding survives in the store
        note_unfinished(self.hass, int(self.data.get("last_run", {}).get("unfinished_days") or 0))
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
            delete_issues(self.hass, *ALL_ISSUES)
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
        delete_issues(self.hass, *ALL_ISSUES)
        await self.store.async_save(deepcopy(self.data))

    def _spawn(self, coro: Any) -> None:
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            from .rowstats import states_meta_entity_ids

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
            from .job_types import JobConfig
            from .recorder_bridge import Job
            from .recorder_thread import run_on_recorder
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
        queue_wait = 0.0
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
                queued = time.monotonic()
                ran = job.timers.seconds["step"]
                fut = run_on_recorder(instance, job.step)
                try:
                    done = await asyncio.shield(fut)
                    queue_wait += time.monotonic() - queued - (job.timers.seconds["step"] - ran)
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
        if not dry and not self._closing:
            # what is left decides the falling-behind issue (read only, off the recorder thread)
            with suppress(Exception):
                job.stats.unfinished_days = await self.hass.async_add_executor_job(
                    job.count_unfinished, instance
                )
                note_unfinished(self.hass, job.stats.unfinished_days)
        if not self._closing:
            await self.async_update_projection()
        seconds = time.monotonic() - started
        report = self._make_report(job, dry, seconds, waited)
        if not dry:
            st = job.stats
            _LOGGER.info(
                "Sediment run finished, %d rows deleted in %d entity-days, %d days left, "
                "%d errors; %s",
                st.rows_deleted,
                st.entity_days,
                st.unfinished_days or 0,
                len(st.errors),
                run_log_line(job.timers, seconds, waited, queue_wait),
            )
        self._note_result(job.stats.errors)
        if not dry and active and not job.cancelled:
            self.data["real_horizon_days"] = float(s.history_days)
        self.data["last_run"] = {
            "at": dt_util.utcnow().isoformat(),
            "mode": report["mode"],
            "rows_deleted": job.stats.rows_deleted,
            "entity_days": job.stats.entity_days,
            "errors": job.stats.errors,
            # only a finished real run judges; a dry run or an unload keeps the last finding
            "unfinished_days": job.stats.unfinished_days
            if job.stats.unfinished_days is not None
            else self.data.get("last_run", {}).get("unfinished_days"),
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

    def _profile_sig(self) -> str:
        """Hash of everything that decides which profile an entity gets."""
        s = self.settings
        return profile_signature(
            s.rules_text, s.resolution, s.custom_tiers, s.flap_collapse, s.coarse_horizon_days
        )

    @staticmethod
    def _is_second_sunday(now: datetime) -> bool:
        return now.weekday() == 6 and 8 <= now.day <= 14

    # ------------------------------------------------------------ sensor values
    @property
    def db_size_gb(self) -> float | None:
        return None if self.db_size is None else round(self.db_size / 1e9, 3)

    @property
    def projected_gb(self) -> float | None:
        return self.data.get("projected_gb")
