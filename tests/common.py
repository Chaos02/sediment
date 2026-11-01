"""Helpers: seed a recorder database and inspect it, all on the recorder thread."""

from __future__ import annotations

import json
import time
from typing import Any

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
)
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import func, select, text

from custom_components.sediment.recorder_bridge import run_on_recorder

DAY = 86400.0


def midnight_days_ago(days: int, now: float | None = None) -> float:
    """UTC midnight `days` days before today's UTC midnight."""
    now = time.time() if now is None else now
    return (int(now // DAY) - days) * DAY


async def seed(
    instance: Any,
    entity_id: str,
    points: list[tuple[float, str, dict | None]],
    null_old: frozenset[int] = frozenset(),
) -> list[int]:
    """Insert rows (ts, state, attrs) with a valid old_state_id chain. Returns state ids."""

    def _do(inst: Any) -> list[int]:
        ids: list[int] = []
        with session_scope(session=inst.get_session()) as s:
            meta = s.execute(
                select(StatesMeta).where(StatesMeta.entity_id == entity_id)
            ).scalar_one_or_none()
            if meta is None:
                meta = StatesMeta(entity_id=entity_id)
                s.add(meta)
                s.flush()
            attr_cache: dict[str, int] = {}
            prev: int | None = None
            for i, (ts, state, attrs) in enumerate(points):
                blob = json.dumps(attrs or {"unit": "u"}, sort_keys=True)
                aid = attr_cache.get(blob)
                if aid is None:
                    row = StateAttributes(
                        shared_attrs=blob,
                        hash=StateAttributes.hash_shared_attrs_bytes(blob.encode()),
                    )
                    s.add(row)
                    s.flush()
                    aid = row.attributes_id
                    attr_cache[blob] = aid
                st = States(
                    metadata_id=meta.metadata_id,
                    state=state,
                    last_updated_ts=ts,
                    last_changed_ts=None,
                    old_state_id=None if i in null_old else prev,
                    attributes_id=aid,
                )
                s.add(st)
                s.flush()
                prev = st.state_id
                ids.append(st.state_id)
        return ids

    return await run_on_recorder(instance, _do)


async def read(instance: Any, fn) -> Any:
    """Run fn(session) on the recorder thread and return its result."""

    def _do(inst: Any) -> Any:
        with session_scope(session=inst.get_session(), read_only=True) as s:
            return fn(s)

    return await run_on_recorder(instance, _do)


async def snapshot(instance: Any) -> list[tuple]:
    """Every states row, ordered, as plain tuples."""
    return await read(
        instance,
        lambda s: [
            tuple(r)
            for r in s.execute(
                select(
                    States.state_id,
                    States.metadata_id,
                    States.state,
                    States.last_updated_ts,
                    States.old_state_id,
                    States.attributes_id,
                )
                .join(StatesMeta, StatesMeta.metadata_id == States.metadata_id)
                .where(~StatesMeta.entity_id.like("sensor.sediment_%"))
                .order_by(States.state_id)
            ).all()
        ],
    )


async def table_counts(instance: Any) -> dict[str, int]:
    """Rows older than the raw window in the tables Sediment cleans, plus recorder runs."""
    from homeassistant.components.recorder.db_schema import (
        Events,
        RecorderRuns,
        StatisticsShortTerm,
    )

    cutoff = time.time() - 10 * DAY

    def q(s: Any) -> dict[str, int]:
        return {
            "old_events": s.execute(
                select(func.count(Events.event_id)).where(Events.time_fired_ts < cutoff)
            ).scalar()
            or 0,
            "old_short_term": s.execute(
                select(func.count(StatisticsShortTerm.id)).where(
                    StatisticsShortTerm.start_ts < cutoff
                )
            ).scalar()
            or 0,
            "runs": s.execute(select(func.count(RecorderRuns.run_id))).scalar() or 0,
        }

    return await read(instance, q)


async def attribute_count(instance: Any) -> int:
    return await read(
        instance, lambda s: s.execute(select(func.count(StateAttributes.attributes_id))).scalar()
    )


async def integrity(instance: Any) -> dict[str, Any]:
    """Foreign key check, chain validity and orphan attributes."""

    def q(s: Any) -> dict[str, Any]:
        conn = s.connection()
        fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        bad_chain = conn.execute(
            text(
                "SELECT COUNT(*) FROM states a JOIN states b ON a.old_state_id = b.state_id "
                "WHERE a.metadata_id <> b.metadata_id OR b.last_updated_ts > a.last_updated_ts"
            )
        ).scalar()
        orphan_attrs = conn.execute(
            text(
                "SELECT COUNT(*) FROM state_attributes a WHERE NOT EXISTS "
                "(SELECT 1 FROM states s WHERE s.attributes_id = a.attributes_id)"
            )
        ).scalar()
        dangling_attrs = conn.execute(
            text(
                "SELECT COUNT(*) FROM states s WHERE s.attributes_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM state_attributes a WHERE a.attributes_id = s.attributes_id)"
            )
        ).scalar()
        return {
            "fk": len(fk),
            "bad_chain": bad_chain,
            "orphan_attrs": orphan_attrs,
            "dangling_attrs": dangling_attrs,
        }

    return await read(instance, q)
