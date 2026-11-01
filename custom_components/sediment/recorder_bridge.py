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

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import math
import time
from typing import Any

from homeassistant.components.recorder.db_schema import (
    EventData,
    Events,
    EventTypes,
    RecorderRuns,
    StateAttributes,
    States,
    StatesMeta,
    StatisticsRuns,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.repack import repack_database
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import Integer, cast, delete, distinct, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import codec
from .const import BATCH_SECONDS, DRY_RUN_MAX_DAYS, PARAM_REFRESH_DAYS
from .profiles import DAY, EntityMeta, Rule, Schedule, resolve

_LOGGER = logging.getLogger(__name__)

_GAP = codec.GAP_STATES
MAX_SAMPLE_ROWS = 300_000
ATTR_CACHE_MAX = 200_000
SAMPLE_DAYS = 10.0  # class and tolerance are estimated from this many newest days
MAX_ATTR_SAMPLE_ROWS = 50_000
MAX_UNIT_ROWS = 200_000  # an entity-day with more rows is processed in time slices
QUARANTINE_AFTER_TRANSIENT = 3


# ---------------------------------------------------------------- recorder thread plumbing
@dataclass(slots=True)
class CallableTask(RecorderTask):
    """Run a function on the recorder thread and hand the result back to the event loop."""

    fn: Callable[[Any], Any]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future

    def run(self, instance: Any) -> None:
        try:
            result = self.fn(instance)
        except BaseException as err:
            self.loop.call_soon_threadsafe(_set_exception, self.future, err)
        else:
            self.loop.call_soon_threadsafe(_set_result, self.future, result)


def _set_result(fut: asyncio.Future, result: Any) -> None:
    if not fut.done():
        fut.set_result(result)


def _set_exception(fut: asyncio.Future, err: BaseException) -> None:
    if not fut.done():
        fut.set_exception(err)


def run_on_recorder(instance: Any, fn: Callable[[Any], Any]) -> asyncio.Future:
    """Queue `fn(instance)` on the recorder thread; await the returned future."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    instance.queue_task(CallableTask(fn, loop, fut))
    return fut


def _chunks(items: list[int], size: int) -> Iterator[list[int]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------- job configuration
@dataclass
class JobConfig:
    """Everything a job needs, captured on the event loop before it starts."""

    dry_run: bool
    now: float
    raw_days: float
    horizon_days: float
    coarse_horizon_days: float
    schedules: dict[str, Schedule]
    rules: list[Rule]
    flap_default: bool
    takeover: bool
    meta: dict[str, EntityMeta] = field(default_factory=dict)
    repack: bool = False
    # The takeover is applied: horizon deletion and cleanup do real work even in a dry run,
    # because they are what the recorder's own purge would otherwise have done.
    housekeeping: bool = False
    # Events, short-term statistics and runs older than this many days are cleaned up
    # (the larger of the raw window and the recorder's own keep_days). 0 = use raw_days.
    cleanup_days: float = 0.0
    # Hash of everything that decides how entities are assigned to profiles (rules text,
    # resolution, custom tiers, flapping default). A change re-thins; a flip of an entity's
    # resolved profile (metadata missing for a night) does not.
    profile_sig: str = ""

    @property
    def cleanup_cutoff(self) -> float:
        return self.now - max(self.raw_days, self.cleanup_days) * DAY

    @property
    def cutoff(self) -> float:
        """Rows with last_updated_ts at or after this are inside the raw window."""
        return self.now - self.raw_days * DAY


@dataclass
class JobStats:
    """Counters of one job. Plain numbers and dicts so they serialise to JSON."""

    mode: str = "run"
    phase: str = "start"
    entity_days: int = 0
    rows_seen: int = 0
    rows_deleted: int = 0
    attr_deleted: int = 0
    relinked: int = 0
    protected: int = 0
    dropped_rows: int = 0
    horizon_rows: int = 0
    events_deleted: int = 0
    short_term_deleted: int = 0
    runs_deleted: int = 0
    orphan_attr_deleted: int = 0
    days_simulated: int = 0
    errors: list[str] = field(default_factory=list)
    # dry run: [rows in, kept] by class and bucket, and by class and profile
    share_counts: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    raw_rows_by_class_profile: dict[str, float] = field(default_factory=dict)
    kept_by_profile_tier: dict[str, float] = field(default_factory=dict)
    top_entities: dict[str, list[float]] = field(default_factory=dict)
    pending_days: int = 0
    quarantined: int = 0
    oversize: list[str] = field(default_factory=list)
    stale_meta_deleted: int = 0
    stale_event_types_deleted: int = 0
    reprofiled_entities: int = 0
    housekeeping: bool = False
    coarse_horizon_rows: int = 0


# lower = more conservative with the data
_CONSERVATIVE = {"keep": 0, "default": 1, "coarse": 2, "drop": 3}


def _parse_stored(value: str) -> tuple[str, bool | None]:
    profile, _, flap = value.partition("|")
    return profile, {"True": True, "False": False}.get(flap)


def day_key(day: int, profile: str) -> str:
    return f"{day}|{profile}"


# ---------------------------------------------------------------- the job
class Job:
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
        self._gen: Iterator[None] | None = None
        self._attr_cache: dict[int, dict] = {}
        self._emap: dict[int, str] = {}
        self._profile_cache: dict[int, tuple[str, bool | None]] = {}

    # ------------------------------------------------------------ driver
    def step(self, instance: Any) -> bool:
        """Run units of work for about BATCH_SECONDS. Returns True when the job is over."""
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
            with session_scope(session=instance.get_session(), read_only=True) as session:
                self._emap = {
                    mid: eid
                    for mid, eid in session.execute(
                        select(StatesMeta.metadata_id, StatesMeta.entity_id)
                    ).all()
                }
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
            try:
                repack_database(instance)
            except Exception as err:
                self._record_error("repack", err)
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

    # ------------------------------------------------------------ helpers
    def _profile_of(self, mid: int) -> tuple[str, bool | None]:
        """The profile of an entity.

        When the entity's metadata is known (a live state or a registry entry) the profile
        is resolved fresh, and `_reprofile` deals with a change. When it is missing now (no
        live state, removed, integration not loaded) the stored profile is used, so a missing
        device class can never move an entity to a less conservative profile; if the rules
        changed as well, the more conservative of the two applies.
        """
        got = self._profile_cache.get(mid)
        if got is None:
            eid = self._emap.get(mid, "")
            stored = (self.state.get("profile_map") or {}).get(eid)
            got = resolve(eid, self.cfg.meta.get(eid), self.cfg.rules)
            if stored is not None and eid not in self.cfg.meta:
                old = _parse_stored(stored)
                if not self._sig_changed or _CONSERVATIVE[old[0]] < _CONSERVATIVE[got[0]]:
                    got = old
            self._profile_cache[mid] = got
        return got

    def _flap(self, override: bool | None) -> bool:
        return self.cfg.flap_default if override is None else override

    @staticmethod
    def _load_rows(session: Session, mid: int, t0: float, t1: float) -> list[tuple]:
        return list(
            session.execute(
                select(
                    States.state_id,
                    States.state,
                    States.last_updated_ts,
                    States.old_state_id,
                    States.attributes_id,
                )
                .where(
                    States.metadata_id == mid,
                    States.last_updated_ts >= t0,
                    States.last_updated_ts < t1,
                )
                .order_by(States.last_updated_ts, States.state_id)
            ).all()
        )

    @staticmethod
    def _neighbour(session: Session, mid: int, ts: float, before: bool) -> tuple | None:
        stmt = select(
            States.state_id,
            States.state,
            States.last_updated_ts,
            States.old_state_id,
            States.attributes_id,
        ).where(States.metadata_id == mid)
        if before:
            stmt = stmt.where(States.last_updated_ts < ts).order_by(
                States.last_updated_ts.desc(), States.state_id.desc()
            )
        else:
            stmt = stmt.where(States.last_updated_ts >= ts).order_by(
                States.last_updated_ts, States.state_id
            )
        row = session.execute(stmt.limit(1)).first()
        return tuple(row) if row else None

    def _attrs_for(self, session: Session, ids: Iterable[int | None], bind: int) -> dict[int, dict]:
        """Parsed attributes for exactly these ids.

        Returns the map the caller needs. The shared cache is trimmed only of ids that this
        call does not use, so what is returned is never evicted from under the caller.
        """
        need = {i for i in ids if i is not None}
        missing = [i for i in need if i not in self._attr_cache]
        if missing and len(self._attr_cache) + len(missing) > ATTR_CACHE_MAX:
            self._attr_cache = {k: v for k, v in self._attr_cache.items() if k in need}
        for chunk in _chunks(missing, bind):
            for aid, blob in session.execute(
                select(StateAttributes.attributes_id, StateAttributes.shared_attrs).where(
                    StateAttributes.attributes_id.in_(chunk)
                )
            ).all():
                try:
                    self._attr_cache[aid] = json.loads(blob) if blob else {}
                except ValueError:
                    self._attr_cache[aid] = {}
        return {i: self._attr_cache.get(i, {}) for i in need}

    # ------------------------------------------------------------ entity info
    def _entity_info(
        self, session: Session, instance: Any, mid: int, day_rows: list[tuple]
    ) -> dict:
        """Class and tolerance parameters of an entity, refreshed weekly."""
        eid = self._emap[mid]
        cached = self.state["entities"].get(eid)
        if cached and self.cfg.now - cached["ts"] < PARAM_REFRESH_DAYS * DAY:
            return cached
        sample = list(
            session.execute(
                select(States.state, States.last_updated_ts, States.attributes_id)
                .where(
                    States.metadata_id == mid,
                    States.last_updated_ts >= self.cfg.now - SAMPLE_DAYS * DAY,
                )
                .order_by(States.last_updated_ts)
                .limit(MAX_SAMPLE_ROWS)
            ).all()
        )
        if len(sample) < 20:
            sample = [(r[1], r[2], r[4]) for r in day_rows]
        states = ["None" if r[0] is None else r[0] for r in sample]
        domain = eid.split(".", 1)[0]
        sc = (self.cfg.meta.get(eid) or EntityMeta()).state_class
        if sc is None and cached:
            # unavailable right now: keep what we knew about it last week
            sc = cached.get("state_class")
        if sc is None:
            # read it from the newest attribute rows that carry one
            tail = [r[2] for r in reversed(sample) if r[2] is not None]
            recent = list(dict.fromkeys(tail))[:20]
            if recent:
                attrs = self._attrs_for(session, recent, instance.max_bind_vars)
                sc = next(
                    (attrs[a]["state_class"] for a in recent if attrs[a].get("state_class")),
                    None,
                )
        cls = codec.classify(domain, states, sc)
        info: dict[str, Any] = {
            "cls": cls,
            "params": {},
            "ts": self.cfg.now,
            "state_class": sc,
        }
        if cls == "continuous":
            info["params"] = codec.series_params(codec.numeric_view(states))
        elif cls == "attribute":
            self._attribute_info(session, instance, eid, sample, states, info)
        self.state["entities"][eid] = info
        return info

    def _attribute_info(
        self,
        session: Session,
        instance: Any,
        eid: str,
        sample: list[tuple],
        states: list[str],
        info: dict,
    ) -> None:
        names = codec.ATTR_DRIVEN[eid.split(".", 1)[0]]
        cut = sample[-MAX_ATTR_SAMPLE_ROWS:]
        st = states[-MAX_ATTR_SAMPLE_ROWS:]
        attrs = self._attrs_for(session, [r[2] for r in cut], instance.max_bind_vars)
        comps: list[list[str]] = [["state", "enum"]]
        params: dict[str, dict] = {}
        for name in names:
            raw = [
                None if (r[2] is None or st[i] in _GAP) else attrs[r[2]].get(name)
                for i, r in enumerate(cut)
            ]
            kind = _component_kind(raw)
            comps.append([name, kind])
            if kind == "continuous":
                num = [codec.to_float(x) if not isinstance(x, (str, bool)) else None for x in raw]
                params[name] = codec.series_params(num)
        info["comps"] = comps
        info["params"] = params

    # ------------------------------------------------------------ the decision
    def _decide(
        self,
        session: Session,
        instance: Any,
        mid: int,
        d0: float,
        d1: float,
        cfgs: list[codec.TierCfg],
        abs_idx: list[int],
        flap: bool,
    ) -> dict | None:
        """Run the tier chain for one entity-day. Returns None if there is nothing to do.

        The result holds the sequence (context rows included), the kept position set
        after every tier and the positions of the day's rows.
        """
        rows = self._load_rows(session, mid, d0, d1)
        if not rows:
            return None
        info = self._entity_info(session, instance, mid, rows)
        prev = self._neighbour(session, mid, d0, before=True)
        nxt = self._neighbour(session, mid, d1, before=False)
        seq = ([prev] if prev else []) + rows + ([nxt] if nxt else [])
        off = 1 if prev else 0
        n = len(seq)
        t = [r[2] for r in seq]
        s = ["None" if r[1] is None else r[1] for r in seq]
        tend = self.cfg.now if nxt is None else nxt[2]
        flap_from = next((j for j, a in enumerate(abs_idx) if a >= 1), None) if flap else None
        cls = info["cls"]
        if cls == "attribute":
            attrs = self._attrs_for(session, [r[4] for r in seq], instance.max_bind_vars)
            comps, params = self._components(info, seq, s, attrs)
            kept_per_tier = codec.thin_attribute_chain(
                t, s, comps, params, cfgs, tend, flap_from=flap_from
            )
            outs = [set(k) for k in kept_per_tier]
        else:
            v = codec.numeric_view(s) if cls in ("continuous", "counter") else None
            chain = codec.run_chain(cls, t, v, s, info["params"], cfgs, tend, flap_from=flap_from)
            outs = [set(o[0]) for o in chain]
        # context rows and the series ends are always kept
        force = set()
        if prev:
            force.add(0)
        if nxt:
            force.add(n - 1)
            if nxt[2] >= self.cfg.cutoff:
                force.add(n - 2)  # the day's last row: the next row is not touched
        outs = [o | force for o in outs]
        return {
            "seq": seq,
            "off": off,
            "n_day": len(rows),
            "outs": outs,
            "cls": cls,
            "has_prev": prev is not None,
            "has_next": nxt is not None,
        }

    def _components(
        self, info: dict, seq: list[tuple], s: list[str], attrs: dict[int, dict]
    ) -> tuple[list[tuple], list[dict | None]]:
        t_len = len(seq)
        comps: list[tuple] = [("state", "enum", None, s)]
        params: list[dict | None] = [None]
        for name, kind in info["comps"][1:]:
            raw = [
                None if (seq[i][4] is None or s[i] in _GAP) else attrs[seq[i][4]].get(name)
                for i in range(t_len)
            ]
            strs = [str(x) for x in raw]
            if kind == "continuous":
                num = [codec.to_float(x) if not isinstance(x, (str, bool)) else None for x in raw]
                comps.append((name, "continuous", num, strs))
                params.append(info["params"].get(name) or codec.series_params(num))
            else:
                comps.append((name, "enum", None, strs))
                params.append(None)
        return comps, params

    # ------------------------------------------------------------ thinning (real run)
    def _thin(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        cutoff = cfg.cutoff
        with session_scope(session=instance.get_session(), read_only=True) as session:
            min_ts = session.execute(select(func.min(States.last_updated_ts))).scalar()
        if min_ts is None:
            return
        first_day = int(min_ts // DAY)
        last_day = int(cutoff // DAY) - 1  # the day must end at or before the cutoff
        applied: dict[str, float] = self.state["applied"]
        for day in range(first_day, last_day + 1):
            d0 = day * DAY
            d1 = d0 + DAY
            age = (cfg.now - d1) / DAY
            todo = []
            for profile, sched in cfg.schedules.items():
                k = sched.target_index(age)
                if k is None or k < 0:
                    continue
                width = sched.tiers[k].cfg.bucket
                if applied.get(day_key(day, profile), 0.0) >= width:
                    continue
                todo.append((profile, sched, k, width))
            if not todo:
                continue
            self.stats.pending_days += 1
            with session_scope(session=instance.get_session(), read_only=True) as session:
                counts = {
                    mid: n
                    for mid, n in session.execute(
                        select(States.metadata_id, func.count(States.state_id))
                        .where(States.last_updated_ts >= d0, States.last_updated_ts < d1)
                        .group_by(States.metadata_id)
                    ).all()
                    if mid is not None
                }
            for profile, sched, k, width in todo:
                key = day_key(day, profile)
                prog = self.state["progress"]
                last_mid = prog.get("last_mid", -1) if prog.get("key") == key else -1
                done_w = applied.get(key, 0.0)
                idxs = [i for i in range(k + 1) if sched.tiers[i].cfg.bucket > done_w]
                cfgs = [sched.tiers[i].cfg for i in idxs]
                for mid in sorted(counts):
                    if mid <= last_mid or mid not in self._emap:
                        continue
                    eprofile, eflap = self._profile_of(mid)
                    if eprofile != profile:
                        continue
                    qkey = f"{day}|{mid}"
                    if qkey in self.state["quarantine"]:
                        self.stats.quarantined += 1
                        continue
                    self._current = qkey
                    yield from self._thin_unit(
                        instance, mid, d0, d1, counts[mid], cfgs, idxs, self._flap(eflap)
                    )
                    self.state["fail_counts"].pop(qkey, None)
                    self._current = None
                    prog["key"] = key
                    prog["last_mid"] = mid
                    yield
                applied[key] = width
                self._thinned_groups.add((day, profile))
                prog.clear()
                yield

    def _thin_unit(
        self,
        instance: Any,
        mid: int,
        d0: float,
        d1: float,
        n_rows: int,
        cfgs: list[codec.TierCfg],
        abs_idx: list[int],
        flap: bool,
    ) -> Iterator[None]:
        """One entity-day, or a few time slices of it if it has too many rows.

        Slices are multiples of the widest bucket of the chain, so no bucket is cut.
        """
        if n_rows <= MAX_UNIT_ROWS:
            self._thin_entity_day(instance, mid, d0, d1, cfgs, abs_idx, flap)
            return
        widest = max(c.bucket for c in cfgs)
        n_slices = -(-n_rows // MAX_UNIT_ROWS)
        length = widest * math.ceil((d1 - d0) / n_slices / widest)
        if length >= d1 - d0:
            note = f"{self._emap[mid]} {int(d0 // DAY)}: {n_rows} rows, bucket too wide to slice"
            _LOGGER.warning("Sediment skips an oversized entity-day: %s", note)
            if len(self.stats.oversize) < 50:
                self.stats.oversize.append(note)
            return
        start = d0
        while start < d1:
            stop = min(d1, start + length)
            self._thin_entity_day(instance, mid, start, stop, cfgs, abs_idx, flap)
            start = stop
            if start < d1:
                yield

    # ------------------------------------------------------------ profile changes
    def _profile_changes(self) -> tuple[dict[str, str], dict[str, str] | None, list[str]]:
        """(current map, map at the last run, entity ids whose profile or flap rule changed).

        Only non-default assignments are stored; an absent entity is `default`.
        """
        cur: dict[str, str] = {}
        for mid, eid in self._emap.items():
            profile, flap = self._profile_of(mid)
            if profile != "default" or flap is not None:
                cur[eid] = f"{profile}|{flap}"
        old = self.state.get("profile_map")
        if old is None:
            return cur, None, []
        changed = sorted(e for e in set(cur) | set(old) if cur.get(e) != old.get(e))
        return cur, old, changed

    def _reprofile(self, instance: Any) -> Iterator[None]:
        """Thin an entity's old days again when its profile changed since the last run.

        The tiers are applied from the first one on the rows that survived. A finer profile
        cannot bring back rows that an earlier, coarser one deleted.
        """
        cfg = self.cfg
        cur, old, changed = self._profile_changes()
        if old is None:
            self.state["profile_map"] = cur
            self.state["profile_sig"] = cfg.profile_sig
            return
        by_eid = {eid: mid for mid, eid in self._emap.items()}
        cutoff_day = int(cfg.cutoff // DAY)
        for eid in changed:
            mid = by_eid.get(eid)
            profile, flap = self._profile_of(mid) if mid is not None else ("default", None)
            sched = cfg.schedules.get(profile) if mid is not None else None
            if sched is not None and mid is not None:
                with session_scope(session=instance.get_session(), read_only=True) as session:
                    day_col = cast(States.last_updated_ts / DAY, Integer)
                    days = session.execute(
                        select(day_col, func.count(States.state_id))
                        .where(States.metadata_id == mid, States.last_updated_ts < cfg.cutoff)
                        .group_by(day_col)
                        .order_by(day_col)
                    ).all()
                for day, n in days:
                    if day >= cutoff_day:
                        continue
                    k = sched.target_index((cfg.now - (day + 1) * DAY) / DAY)
                    qkey = f"{day}|{mid}"
                    if k is None or k < 0 or qkey in self.state["quarantine"]:
                        continue
                    if (day, profile) in self._thinned_groups:
                        continue  # this run already thinned the day under this profile
                    idxs = list(range(k + 1))
                    self._current = qkey
                    yield from self._thin_unit(
                        instance,
                        mid,
                        day * DAY,
                        (day + 1) * DAY,
                        n,
                        [sched.tiers[i].cfg for i in idxs],
                        idxs,
                        self._flap(flap),
                    )
                    self.state["fail_counts"].pop(qkey, None)
                    self._current = None
                    yield
            new = cur.get(eid)
            if new is None:
                self.state["profile_map"].pop(eid, None)
            else:
                self.state["profile_map"][eid] = new
            self.stats.reprofiled_entities += 1
            yield
        # entities that are new to the map (metadata arrived) and the signature
        for eid, value in cur.items():
            if eid in self.cfg.meta:
                self.state["profile_map"].setdefault(eid, value)
        self.state["profile_sig"] = cfg.profile_sig

    def _thin_entity_day(
        self,
        instance: Any,
        mid: int,
        d0: float,
        d1: float,
        cfgs: list[codec.TierCfg],
        abs_idx: list[int],
        flap: bool,
    ) -> None:
        cutoff = self.cfg.cutoff
        bind = instance.max_bind_vars
        with session_scope(session=instance.get_session()) as session:
            res = self._decide(session, instance, mid, d0, d1, cfgs, abs_idx, flap)
            if res is None:
                return
            seq, off, n_day = res["seq"], res["off"], res["n_day"]
            kept = set(res["outs"][-1])
            self.stats.entity_days += 1
            self.stats.rows_seen += n_day
            day_pos = range(off, off + n_day)
            if all(p in kept for p in day_pos):
                return
            # Resolve the plan until stable: relink, deletions, pointers from outside.
            for _ in range(4):
                plan = _plan(seq, kept, off, n_day, cutoff)
                protected = self._protect(session, plan, seq, off, n_day, cutoff, bind)
                if not protected:
                    break
                kept |= protected
                self.stats.protected += len(protected)
            else:
                _LOGGER.warning("Sediment: could not settle a plan for metadata_id %s", mid)
                return
            delete_ids: list[int] = plan["delete_ids"]
            if not delete_ids:
                return
            if plan["relink"]:
                session.execute(
                    update(States),
                    [{"state_id": a, "old_state_id": b} for a, b in plan["relink"]],
                )
                self.stats.relinked += len(plan["relink"])
            if plan["external"]:
                session.execute(
                    update(States),
                    [{"state_id": a, "old_state_id": b} for a, b in plan["external"]],
                )
            attr_ids = {seq[p][4] for p in plan["delete_pos"] if seq[p][4] is not None}
            self._delete_states(session, instance, delete_ids, attr_ids, cutoff, null_own=True)

    def _protect(
        self,
        session: Session,
        plan: dict,
        seq: list[tuple],
        off: int,
        n_day: int,
        cutoff: float,
        bind: int,
    ) -> set[int]:
        """Find rows outside the day that point at rows about to be deleted.

        Pointers from rows older than the cutoff are moved (plan["external"]); a pointer
        from a row inside the raw window instead keeps the pointed-to row. Returns the
        positions to add to the kept set (empty = plan is final).
        """
        id_to_pos = {seq[p][0]: p for p in plan["delete_pos"]}
        relinked_ids = {a for a, _b in plan["relink"]}
        external: list[tuple[int, int | None]] = []
        extra: set[int] = set()
        for chunk in _chunks(list(id_to_pos), bind):
            for sid, old, ts in session.execute(
                select(States.state_id, States.old_state_id, States.last_updated_ts).where(
                    States.old_state_id.in_(chunk)
                )
            ).all():
                if sid in relinked_ids or sid in id_to_pos:
                    continue
                if ts is None or ts >= cutoff:
                    extra.add(id_to_pos[old])
                else:
                    external.append((sid, plan["prev_kept"][id_to_pos[old]]))
        plan["external"] = external
        return extra

    def _delete_states(
        self,
        session: Session,
        instance: Any,
        state_ids: list[int],
        attr_ids: set[int],
        cutoff: float,
        disconnect: bool = False,
        null_own: bool = False,
    ) -> None:
        """Delete states rows, then the attribute rows nothing references any more.

        Deleting in chunks must not leave a surviving reference between chunks.
        `disconnect` (HA's own purge method) nulls the pointers of whatever points at a
        chunk; `null_own` nulls the pointers of the rows being deleted themselves, which
        is safe when the caller has already moved every pointer of a surviving row.
        """
        bind = instance.max_bind_vars
        ids = set(state_ids)
        if null_own:
            for chunk in _chunks(list(ids), bind):
                session.execute(
                    update(States)
                    .where(States.state_id.in_(chunk), States.last_updated_ts < cutoff)
                    .values(old_state_id=None)
                    .execution_options(synchronize_session=False)
                )
        for chunk in _chunks(list(ids), bind):
            if disconnect:
                session.execute(
                    update(States)
                    .where(States.old_state_id.in_(chunk))
                    .values(old_state_id=None)
                    .execution_options(synchronize_session=False)
                )
            session.execute(
                delete(States)
                .where(States.state_id.in_(chunk), States.last_updated_ts < cutoff)
                .execution_options(synchronize_session=False)
            )
        self.stats.rows_deleted += len(ids)
        attr_ids = {a for a in attr_ids if a is not None}
        if attr_ids:
            seen: set[int] = set()
            for chunk in _chunks(list(attr_ids), bind):
                seen.update(
                    a
                    for (a,) in session.execute(
                        select(distinct(States.attributes_id)).where(
                            States.attributes_id.in_(chunk)
                        )
                    ).all()
                )
            unused = attr_ids - seen
            for chunk in _chunks(list(unused), bind):
                session.execute(
                    delete(StateAttributes)
                    .where(StateAttributes.attributes_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            self.stats.attr_deleted += len(unused)
            instance.state_attributes_manager.evict_purged(unused)
        instance.states_manager.evict_purged_state_ids(ids)

    # ------------------------------------------------------------ dry run
    def _simulate(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        today = int(cfg.now // DAY)
        stats = self.stats
        # Count what the real run would still have to do
        cutoff = cfg.cutoff
        with session_scope(session=instance.get_session(), read_only=True) as session:
            min_ts = session.execute(select(func.min(States.last_updated_ts))).scalar()
        applied = self.state["applied"]
        _cur, _old, changed = self._profile_changes()
        stats.reprofiled_entities = len(changed)
        if min_ts is not None:
            for day in range(int(min_ts // DAY), int(cutoff // DAY)):
                age = (cfg.now - (day + 1) * DAY) / DAY
                for profile, sched in cfg.schedules.items():
                    k = sched.target_index(age)
                    if (
                        k is not None
                        and k >= 0
                        and applied.get(day_key(day, profile), 0.0) < sched.tiers[k].cfg.bucket
                    ):
                        stats.pending_days += 1
                        break
        yield
        for day in range(today - DRY_RUN_MAX_DAYS, today):
            d0 = day * DAY
            d1 = d0 + DAY
            with session_scope(session=instance.get_session(), read_only=True) as session:
                counts = session.execute(
                    select(States.metadata_id, func.count(States.state_id))
                    .where(States.last_updated_ts >= d0, States.last_updated_ts < d1)
                    .group_by(States.metadata_id)
                ).all()
            if not counts:
                continue
            stats.days_simulated += 1
            for mid, n in sorted(counts):
                if mid is None or mid not in self._emap:
                    continue
                profile, eflap = self._profile_of(mid)
                eid = self._emap[mid]
                self._simulate_entity_day(instance, mid, eid, n, profile, self._flap(eflap), d0, d1)
                yield

    def _simulate_entity_day(
        self,
        instance: Any,
        mid: int,
        eid: str,
        n: int,
        profile: str,
        flap: bool,
        d0: float,
        d1: float,
    ) -> None:
        stats = self.stats
        with session_scope(session=instance.get_session(), read_only=True) as session:
            if profile in ("keep", "drop"):
                cls = (self.state["entities"].get(eid) or {}).get("cls", "continuous")
                stats.raw_rows_by_class_profile[f"{cls}|{profile}"] = (
                    stats.raw_rows_by_class_profile.get(f"{cls}|{profile}", 0.0) + n
                )
                return
            sched = self.cfg.schedules[profile]
            cfgs = [t.cfg for t in sched.tiers]
            if not cfgs:
                return
            res = self._decide(session, instance, mid, d0, d1, cfgs, list(range(len(cfgs))), flap)
            if res is None:
                return
            cls = res["cls"]
            n_day = res["n_day"]
            off = res["off"]
            stats.entity_days += 1
            stats.rows_seen += n_day
            key = f"{cls}|{profile}"
            stats.raw_rows_by_class_profile[key] = (
                stats.raw_rows_by_class_profile.get(key, 0.0) + n_day
            )
            by_bucket = stats.share_counts.setdefault(cls, {})
            kept_last = n_day
            for cfg_t, out in zip(cfgs, res["outs"], strict=True):
                kept = sum(1 for p in range(off, off + n_day) if p in out)
                slot = by_bucket.setdefault(str(cfg_t.bucket), [0.0, 0.0])
                slot[0] += n_day
                slot[1] += kept
                tk = f"{profile}|{cfg_t.bucket}"
                stats.kept_by_profile_tier[tk] = stats.kept_by_profile_tier.get(tk, 0.0) + kept
                kept_last = kept
            top = stats.top_entities.setdefault(eid, [0.0, 0.0])
            top[0] += n_day
            top[1] += kept_last

    # ------------------------------------------------------------ drop profile
    def _drop(self, instance: Any) -> Iterator[None]:
        """Delete every row of `drop` entities that is older than the raw window."""
        cfg = self.cfg
        cutoff = cfg.cutoff
        bind = instance.max_bind_vars
        mids = [m for m in self._emap if self._profile_of(m)[0] == "drop"]
        for mid in mids:
            if cfg.dry_run:
                with session_scope(session=instance.get_session(), read_only=True) as session:
                    self.stats.dropped_rows += (
                        session.execute(
                            select(func.count(States.state_id)).where(
                                States.metadata_id == mid, States.last_updated_ts < cutoff
                            )
                        ).scalar()
                        or 0
                    )
                yield
                continue
            while True:
                with session_scope(session=instance.get_session()) as session:
                    rows = session.execute(
                        select(States.state_id, States.attributes_id)
                        .where(States.metadata_id == mid, States.last_updated_ts < cutoff)
                        .limit(bind)
                    ).all()
                    if not rows:
                        break
                    self._delete_states(
                        session,
                        instance,
                        [r[0] for r in rows],
                        {r[1] for r in rows if r[1] is not None},
                        cutoff,
                        disconnect=True,
                    )
                    self.stats.dropped_rows += len(rows)
                yield
        if mids and not cfg.dry_run:
            self._refresh_oldest(instance)

    # ------------------------------------------------------------ horizon
    def _horizon(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        cutoff = cfg.cutoff
        bind = instance.max_bind_vars
        cut_all = min(cfg.now - cfg.horizon_days * DAY, cutoff)
        cut_coarse = min(cfg.now - min(cfg.horizon_days, cfg.coarse_horizon_days) * DAY, cutoff)
        # coarse entities first (shorter horizon), then everything at the global horizon
        passes: list[tuple[list[int] | None, float]] = []
        # In a dry run under an applied takeover the coarse pass only counts: a rule that is
        # being tried out must not delete.
        count_only_coarse = cfg.dry_run and cfg.housekeeping
        if cut_coarse > cut_all:
            with session_scope(session=instance.get_session(), read_only=True) as session:
                old_mids = {
                    m
                    for (m,) in session.execute(
                        select(States.metadata_id)
                        .where(States.last_updated_ts < cut_coarse)
                        .distinct()
                    ).all()
                }
            coarse = [m for m in old_mids if m in self._emap and self._profile_of(m)[0] == "coarse"]
            passes.append((coarse, cut_coarse))
        passes.append((None, cut_all))
        deleted_any = False
        if cfg.dry_run and not cfg.housekeeping:
            with session_scope(session=instance.get_session(), read_only=True) as session:
                for mids, cut in passes:
                    stmt = select(func.count(States.state_id)).where(States.last_updated_ts < cut)
                    if mids is not None:
                        if not mids:
                            continue
                        stmt = stmt.where(States.metadata_id.in_(mids))
                    self.stats.horizon_rows += session.execute(stmt).scalar() or 0
            yield
            return
        for mids, cut in passes:
            if mids is not None and count_only_coarse:
                if mids:
                    with session_scope(session=instance.get_session(), read_only=True) as ses:
                        for chunk in _chunks(mids, bind):
                            self.stats.coarse_horizon_rows += (
                                ses.execute(
                                    select(func.count(States.state_id)).where(
                                        States.last_updated_ts < cut,
                                        States.metadata_id.in_(chunk),
                                    )
                                ).scalar()
                                or 0
                            )
                yield
                continue
            targets: list[int | None] = list(mids) if mids is not None else [None]
            for mid in targets:
                while True:
                    with session_scope(session=instance.get_session()) as session:
                        stmt = select(States.state_id, States.attributes_id).where(
                            States.last_updated_ts < cut
                        )
                        if mid is not None:
                            stmt = stmt.where(States.metadata_id == mid)
                        rows = session.execute(stmt.limit(bind)).all()
                        if not rows:
                            break
                        self._delete_states(
                            session,
                            instance,
                            [r[0] for r in rows],
                            {r[1] for r in rows if r[1] is not None},
                            cutoff,
                            disconnect=True,
                        )
                        self.stats.horizon_rows += len(rows)
                        deleted_any = True
                    yield
        if deleted_any:
            self._refresh_oldest(instance)

    @staticmethod
    def _refresh_oldest(instance: Any) -> None:
        with session_scope(session=instance.get_session(), read_only=True) as session:
            instance.states_manager.load_from_db(session)

    # ------------------------------------------------------------ cleanup (takeover)
    def _cleanup(self, instance: Any) -> Iterator[None]:
        """What purge.py does besides states: events, short term statistics, runs, ids.

        The cutoff is the larger of the raw window and the recorder's own keep_days, so
        nothing disappears earlier than it would have with the recorder's purge. Every
        step loops until its backlog is gone, one batch per unit of work.
        """
        cfg = self.cfg
        bind = instance.max_bind_vars
        cut_ts = cfg.cleanup_cutoff
        cut_dt = datetime.fromtimestamp(cut_ts, UTC)
        stats = self.stats
        if cfg.dry_run and not cfg.housekeeping:
            with session_scope(session=instance.get_session(), read_only=True) as session:
                stats.events_deleted = (
                    session.execute(
                        select(func.count(Events.event_id)).where(Events.time_fired_ts < cut_ts)
                    ).scalar()
                    or 0
                )
                stats.short_term_deleted = (
                    session.execute(
                        select(func.count(StatisticsShortTerm.id)).where(
                            StatisticsShortTerm.start_ts < cut_ts
                        )
                    ).scalar()
                    or 0
                )
            yield
            return
        # events and their data
        while True:
            with session_scope(session=instance.get_session()) as session:
                rows = session.execute(
                    select(Events.event_id, Events.data_id)
                    .where(Events.time_fired_ts < cut_ts)
                    .limit(bind)
                ).all()
                if not rows:
                    break
                ids = [r[0] for r in rows]
                data_ids = {r[1] for r in rows if r[1] is not None}
                removed = 0
                for chunk in _chunks(ids, bind):
                    removed += (
                        session.execute(
                            delete(Events)
                            .where(Events.event_id.in_(chunk))
                            .execution_options(synchronize_session=False)
                        ).rowcount
                        or 0
                    )
                stats.events_deleted += removed
                seen: set[int] = set()
                for chunk in _chunks(list(data_ids), bind):
                    seen.update(
                        d
                        for (d,) in session.execute(
                            select(distinct(Events.data_id)).where(Events.data_id.in_(chunk))
                        ).all()
                    )
                unused = data_ids - seen
                for chunk in _chunks(list(unused), bind):
                    session.execute(
                        delete(EventData)
                        .where(EventData.data_id.in_(chunk))
                        .execution_options(synchronize_session=False)
                    )
                instance.event_data_manager.evict_purged(unused)
            yield
            if not removed:
                break
        # short term statistics
        while True:
            with session_scope(session=instance.get_session()) as session:
                ids = [
                    r[0]
                    for r in session.execute(
                        select(StatisticsShortTerm.id)
                        .where(StatisticsShortTerm.start_ts < cut_ts)
                        .limit(bind)
                    ).all()
                ]
                if not ids:
                    break
                removed = (
                    session.execute(
                        delete(StatisticsShortTerm)
                        .where(StatisticsShortTerm.id.in_(ids))
                        .execution_options(synchronize_session=False)
                    ).rowcount
                    or 0
                )
                stats.short_term_deleted += removed
            yield
            if not removed:
                break
        # statistics runs (keep the newest) and recorder runs (keep the current)
        with session_scope(session=instance.get_session()) as session:
            latest = session.execute(select(func.max(StatisticsRuns.run_id))).scalar()
            run_ids = [
                r[0]
                for r in session.execute(
                    select(StatisticsRuns.run_id).where(StatisticsRuns.start < cut_dt)
                ).all()
                if r[0] != latest
            ]
            for chunk in _chunks(run_ids, bind):
                session.execute(
                    delete(StatisticsRuns)
                    .where(StatisticsRuns.run_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            current = instance.recorder_runs_manager.current.run_id
            res = session.execute(
                delete(RecorderRuns)
                .where(
                    RecorderRuns.end.is_not(None),
                    RecorderRuns.end < cut_dt,
                    RecorderRuns.run_id != current,
                )
                .execution_options(synchronize_session=False)
            )
            stats.runs_deleted += len(run_ids) + (res.rowcount or 0)
        with session_scope(session=instance.get_session(), read_only=True) as session:
            instance.recorder_runs_manager.load_from_db(session)
        yield
        # attribute rows nothing references (left behind by earlier purges or imports)
        while True:
            with session_scope(session=instance.get_session()) as session:
                ids = [
                    r[0]
                    for r in session.execute(
                        select(StateAttributes.attributes_id)
                        .where(
                            ~select(States.state_id)
                            .where(States.attributes_id == StateAttributes.attributes_id)
                            .exists()
                        )
                        .limit(bind)
                    ).all()
                ]
                if not ids:
                    break
                removed = (
                    session.execute(
                        delete(StateAttributes)
                        .where(StateAttributes.attributes_id.in_(ids))
                        .execution_options(synchronize_session=False)
                    ).rowcount
                    or 0
                )
                instance.state_attributes_manager.evict_purged(set(ids))
                stats.orphan_attr_deleted += removed
            yield
            if not removed:
                break
        # entity ids and event types nothing uses any more, as the recorder's purge does
        with session_scope(session=instance.get_session()) as session:
            stale = session.execute(
                select(StatesMeta.metadata_id, StatesMeta.entity_id).where(
                    ~select(States.state_id)
                    .where(States.metadata_id == StatesMeta.metadata_id)
                    .exists()
                )
            ).all()
            for chunk in _chunks([m for m, _e in stale], bind):
                session.execute(
                    delete(StatesMeta)
                    .where(StatesMeta.metadata_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            names = {e for _m, e in stale}
            instance.states_meta_manager.evict_purged(names)
            instance.states_manager.evict_purged_entity_ids(names)
            stats.stale_meta_deleted += len(stale)
            stale_types = session.execute(
                select(EventTypes.event_type_id, EventTypes.event_type).where(
                    ~select(Events.event_id)
                    .where(Events.event_type_id == EventTypes.event_type_id)
                    .exists()
                )
            ).all()
            for chunk in _chunks([t for t, _n in stale_types], bind):
                session.execute(
                    delete(EventTypes)
                    .where(EventTypes.event_type_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            instance.event_type_manager.evict_purged({n for _t, n in stale_types})
            stats.stale_event_types_deleted += len(stale_types)
        yield


# ---------------------------------------------------------------- plan (pure)
def _plan(seq: list[tuple], kept: set[int], off: int, n_day: int, cutoff: float) -> dict:
    """From the kept positions of a sequence compute relinks and deletions.

    seq rows are (state_id, state, ts, old_state_id, attributes_id): the optional
    previous row, the day's rows, the optional next row. Pure function, unit tested.
    """
    n = len(seq)
    has_next = off + n_day < n
    deleted_ids = {seq[p][0] for p in range(n) if p not in kept}
    relink: list[tuple[int, int | None]] = []
    prev_kept_id: int | None = None
    prev_kept_of: dict[int, int | None] = {}
    delete_pos: list[int] = []
    for p in range(n):
        sid, _state, ts, old, _attr = seq[p]
        if p not in kept:
            prev_kept_of[p] = prev_kept_id
            delete_pos.append(p)
            continue
        context_prev = off == 1 and p == 0
        context_next = has_next and p == n - 1
        modifiable = not context_prev and (not context_next or (ts is not None and ts < cutoff))
        # Only a pointer to a row that is going away moves. HA's own NULLs stay NULL and a
        # pointer to a surviving row stays as it is.
        if modifiable and old is not None and old in deleted_ids:
            relink.append((sid, prev_kept_id))
        prev_kept_id = sid
    return {
        "relink": relink,
        "delete_pos": delete_pos,
        "delete_ids": [seq[p][0] for p in delete_pos],
        "prev_kept": prev_kept_of,
        "external": [],
    }


def _component_kind(raw: list) -> str:
    """continuous, or enum (also for few-level numeric components), for an attribute."""
    nn = [x for x in raw if x is not None]
    num = [codec.to_float(x) if not isinstance(x, (str, bool)) else None for x in nn]
    isnum = sum(1 for x in num if x is not None)
    if not nn or isnum < 0.9 * len(nn) or isnum == 0:
        return "enum"
    distinct_vals = {x for x in num if x is not None}
    if len(distinct_vals) <= codec.FEWLEVEL_MAX_LEVELS and codec.levels_far_apart(
        [str(x) for x in nn], distinct_vals
    ):
        return "enum"
    return "continuous"


def states_meta_entity_ids(instance: Any) -> list[str]:
    """Every entity id the recorder knows. Read only."""
    with session_scope(session=instance.get_session(), read_only=True) as session:
        return [e for (e,) in session.execute(select(StatesMeta.entity_id)).all()]


# ---------------------------------------------------------------- statistics for the estimate
def collect_rowstats(instance: Any, now: float, raw_days: float) -> dict:
    """Rows per entity over the raw window, plus table sizes. Read only.

    Rows older than the raw window may already be thinned, so rates are taken from the
    raw window alone.
    """
    from homeassistant.components.recorder.db_schema import Statistics

    with session_scope(session=instance.get_session(), read_only=True) as session:
        lo = session.execute(select(func.min(States.last_updated_ts))).scalar()
        total_rows = session.execute(select(func.count(States.state_id))).scalar() or 0
        since = now - raw_days * DAY
        if lo is not None:
            since = max(since, lo)
        per = session.execute(
            select(StatesMeta.entity_id, func.count(States.state_id))
            .join(States, States.metadata_id == StatesMeta.metadata_id)
            .where(States.last_updated_ts >= since)
            .group_by(StatesMeta.entity_id)
        ).all()
        stats_total = session.execute(select(func.count(Statistics.id))).scalar() or 0
        stats_max = session.execute(select(func.max(Statistics.start_ts))).scalar()
        stats_recent = 0
        if stats_max:
            stats_recent = (
                session.execute(
                    select(func.count(Statistics.id)).where(
                        Statistics.start_ts > stats_max - 10 * DAY
                    )
                ).scalar()
                or 0
            )
    return {
        "span_days": max(1.0, (now - since) / DAY),
        "entities": {eid: n for eid, n in per},
        "rows": total_rows,
        "stats_rows": stats_total,
        "stats_rows_per_day": stats_recent / 10.0,
        "now": now,
    }


def aggregate_by_class_profile(
    rowstats: dict,
    classify: Callable[[str], str],
    profile_of: Callable[[str], str],
) -> dict[tuple[str, str], float]:
    """(class, profile) -> raw rows per day, from per entity row counts."""
    out: dict[tuple[str, str], float] = defaultdict(float)
    span = rowstats["span_days"]
    for eid, n in rowstats["entities"].items():
        out[(classify(eid), profile_of(eid))] += n / span
    return dict(out)
