"""The dry run simulation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.components.recorder.db_schema import (
    States,
)
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import func, select

from .const import DRY_RUN_MAX_DAYS
from .pace import unfinished_days
from .profiles import DAY


class SimulateMixin:
    """The dry run: what a real run would do, without deleting."""

    # ------------------------------------------------------------ dry run
    def _simulate(self, instance: Any) -> Iterator[None]:
        cfg = self.cfg
        today = int(cfg.now // DAY)
        stats = self.stats
        # Count what the real run would still have to do
        cutoff = cfg.cutoff
        with session_scope(session=instance.get_session(), read_only=True) as session:
            min_ts = session.execute(select(func.min(States.last_updated_ts))).scalar()
        _cur, _old, changed = self._profile_changes()
        stats.reprofiled_entities = len(changed)
        stats.pending_days += unfinished_days(
            min_ts, cfg.now, cutoff, cfg.schedules, self.state["applied"]
        )
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
