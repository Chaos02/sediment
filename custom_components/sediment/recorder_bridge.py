"""Everything that reads or writes the recorder's tables. This module deletes data.

All of it runs on the recorder thread, inside `RecorderTask`s. The unit of work is one
entity-day and one transaction. Rules that keep the database valid:

* only rows older than the raw window (`cutoff`) are ever deleted; rows inside it are
  read, and modified only by the same `old_state_id` disconnect HA's own purge does;
* `old_state_id` is relinked to the previous kept row of the same entity, except that a
  row HA stored with a NULL `old_state_id` stays NULL;
* a row that something newer still points to is never deleted blindly: the pointer is
  moved to the previous kept row, or, if the pointing row is inside the raw window, the
  pointed-to row is kept instead;
* the first and last row of an entity's whole series are kept; the last row of a day is
  kept when the next row lies inside the raw window (so that row is not touched);
* attribute rows are removed only when no `states` row references them any more, and the
  recorder's id caches are evicted exactly as `purge.py` does.

Imports of recorder internals are at module level on purpose: `compat.check_compat` runs
first, and the manager imports this module only after it passed.
"""

from __future__ import annotations

from collections.abc import Iterator
import logging
import time
from typing import Any

from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
)
from homeassistant.components.recorder.repack import repack_database
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from .const import BATCH_SECONDS
from .job_cleanup import CleanupMixin
from .job_decide import DecideMixin
from .job_simulate import SimulateMixin
from .job_thin import ThinMixin
from .job_types import JobConfig, JobStats
from .pace import Timers, unfinished_days

_LOGGER = logging.getLogger(__name__)

QUARANTINE_AFTER_TRANSIENT = 3


# ---------------------------------------------------------------- the job
class Job(DecideMixin, ThinMixin, SimulateMixin, CleanupMixin):
    """One run of the nightly work. `step` is called repeatedly on the recorder thread."""

    def __init__(self, cfg: JobConfig, state: dict) -> None:
        self.cfg = cfg
        self.state = state
        self.state.setdefault("applied", {})
        self.state.setdefault("entities", {})
        self.state.setdefault("progress", {})
        self.state.setdefault("quarantine", {})
        self.state.setdefault("fail_counts", {})
        self._sig_changed = (
            self.state.get("profile_map") is not None
            and self.state.get("profile_sig") != cfg.profile_sig
        )
        self._thinned_groups: set[tuple[int, str]] = set()
        self._current: str | None = None
        self.stats = JobStats(
            mode="dry_run" if cfg.dry_run else "run", housekeeping=cfg.housekeeping
        )
        self.cancelled = False
        self.finished = False
        self.timers = Timers()
        self._gen: Iterator[None] | None = None
        self._attr_cache: dict[int, dict] = {}
        self._emap: dict[int, str] = {}
        self._profile_cache: dict[int, tuple[str, bool | None]] = {}

    # ------------------------------------------------------------ driver
    def step(self, instance: Any) -> bool:
        """Run units of work for about BATCH_SECONDS. Returns True when the job is over."""
        began = time.monotonic()
        try:
            return self._step(instance)
        finally:
            self.timers.seconds["step"] += time.monotonic() - began

    def count_unfinished(self, instance: Any) -> int:
        """Days older than the raw window that still have thinning to do. Read only."""
        with session_scope(session=instance.get_session(), read_only=True) as session:
            min_ts = session.execute(select(func.min(States.last_updated_ts))).scalar()
        cfg = self.cfg
        return unfinished_days(min_ts, cfg.now, cfg.cutoff, cfg.schedules, self.state["applied"])

    def _step(self, instance: Any) -> bool:
        if self.finished:
            return True
        if self._gen is None:
            self._gen = self._work(instance)
        deadline = time.monotonic() + BATCH_SECONDS
        while True:
            if self.cancelled:
                self.finished = True
                return True
            try:
                next(self._gen)
            except StopIteration:
                self.finished = True
                return True
            except Exception as err:
                # Phases are guarded individually; this is a bug in the driver itself.
                _LOGGER.exception("Sediment job failed in phase %s", self.stats.phase)
                self.stats.errors.append(f"{self.stats.phase}: {err!r}")
                self.finished = True
                return True
            if time.monotonic() >= deadline:
                return False

    def _work(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        try:
            self.timers.lap()
            with session_scope(session=instance.get_session(), read_only=True) as session:
                self._emap = {
                    mid: eid
                    for mid, eid in session.execute(
                        select(StatesMeta.metadata_id, StatesMeta.entity_id)
                    ).all()
                }
            self.timers.add("plan")
        except Exception as err:
            _LOGGER.exception("Sediment could not read the entity list")
            self._record_error("start", err)
            return
        # Each phase stops at its first error but never prevents the next one: horizon
        # deletion and cleanup are what the recorder's own purge would have done.
        if cfg.dry_run:
            yield from self._guarded("simulate", self._simulate(instance))
        else:
            yield from self._guarded("thin", self._thin(instance))
            yield from self._guarded("reprofile", self._reprofile(instance))
        yield from self._guarded("drop", self._drop(instance))
        if cfg.takeover:
            yield from self._guarded("horizon", self._horizon(instance))
            yield from self._guarded("cleanup", self._cleanup(instance))
        if cfg.repack and (not cfg.dry_run or cfg.housekeeping):
            self.stats.phase = "repack"
            yield
            self.timers.lap()
            try:
                repack_database(instance)
            except Exception as err:
                self._record_error("repack", err)
            self.timers.add("repack")
        self.stats.phase = "done"

    def _guarded(self, phase: str, gen: Iterator[None]) -> Iterator[None]:
        """Run a phase unit by unit; an error ends this phase only."""
        self.stats.phase = phase
        while True:
            if self.cancelled:
                return
            try:
                next(gen)
            except StopIteration:
                return
            except Exception as err:
                self._record_error(phase, err)
                return
            yield

    def _record_error(self, phase: str, err: Exception) -> None:
        _LOGGER.exception("Sediment job failed in phase %s", phase)
        if len(self.stats.errors) < 50:
            self.stats.errors.append(f"{phase}: {err!r}")
        key, self._current = self._current, None
        if key is None:
            return
        # A transient database error (locked, busy) is retried on the next run and only
        # quarantined after three failures; anything else is quarantined at once.
        if isinstance(err, OperationalError):
            fails = self.state["fail_counts"].get(key, 0) + 1
            if fails < QUARANTINE_AFTER_TRANSIENT:
                self.state["fail_counts"][key] = fails
                return
        self.state["fail_counts"].pop(key, None)
        # The entity-day is skipped from now on (its rows stay as they are), so one odd row
        # cannot block the job every night; the report lists it.
        self.state["quarantine"][key] = {"ts": self.cfg.now, "error": repr(err)[:200]}
