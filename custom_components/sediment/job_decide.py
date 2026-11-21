"""Deciding which rows of an entity-day to keep. Read only."""

from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Any

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import codec
from .const import PARAM_REFRESH_DAYS
from .job_types import CONSERVATIVE, parse_stored
from .profiles import DAY, EntityMeta, resolve
from .recorder_thread import chunks

_GAP = codec.GAP_STATES
MAX_SAMPLE_ROWS = 300_000
ATTR_CACHE_MAX = 200_000
SAMPLE_DAYS = 10.0  # class and tolerance are estimated from this many newest days
MAX_ATTR_SAMPLE_ROWS = 50_000


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


class DecideMixin:
    """Which rows of an entity-day to keep: entity classes, parameters and the tier chain."""

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
                old = parse_stored(stored)
                if not self._sig_changed or CONSERVATIVE[old[0]] < CONSERVATIVE[got[0]]:
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
        for chunk in chunks(missing, bind):
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
