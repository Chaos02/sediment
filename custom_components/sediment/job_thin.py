"""Thinning old days: the plan, the relink and the delete statements."""

from __future__ import annotations

from collections.abc import Iterator
import logging
import math
from typing import Any

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
)
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import Integer, cast, delete, distinct, func, select, update
from sqlalchemy.orm import Session

from . import codec
from .job_types import day_key
from .profiles import DAY
from .recorder_thread import chunks

_LOGGER = logging.getLogger(__name__)


MAX_UNIT_ROWS = 200_000  # an entity-day with more rows is processed in time slices


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


class ThinMixin:
    """Thinning of old days, profile changes, and the statements that delete."""

    # ------------------------------------------------------------ thinning (real run)
    def _thin(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        cutoff = cfg.cutoff
        self.timers.lap()
        with session_scope(session=instance.get_session(), read_only=True) as session:
            min_ts = session.execute(select(func.min(States.last_updated_ts))).scalar()
        self.timers.add("plan")
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
            self.timers.lap()
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
            self.timers.add("plan")
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
        with self.timers.write_scope(instance) as session:
            res = self._decide(session, instance, mid, d0, d1, cfgs, abs_idx, flap)
            self.timers.add("plan")
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
            self.timers.add("delete")

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
        for chunk in chunks(list(id_to_pos), bind):
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
            for chunk in chunks(list(ids), bind):
                session.execute(
                    update(States)
                    .where(States.state_id.in_(chunk), States.last_updated_ts < cutoff)
                    .values(old_state_id=None)
                    .execution_options(synchronize_session=False)
                )
        for chunk in chunks(list(ids), bind):
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
            for chunk in chunks(list(attr_ids), bind):
                seen.update(
                    a
                    for (a,) in session.execute(
                        select(distinct(States.attributes_id)).where(
                            States.attributes_id.in_(chunk)
                        )
                    ).all()
                )
            unused = attr_ids - seen
            for chunk in chunks(list(unused), bind):
                session.execute(
                    delete(StateAttributes)
                    .where(StateAttributes.attributes_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            self.stats.attr_deleted += len(unused)
            instance.state_attributes_manager.evict_purged(unused)
        instance.states_manager.evict_purged_state_ids(ids)
