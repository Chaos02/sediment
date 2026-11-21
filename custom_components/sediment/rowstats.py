"""Row statistics for the size estimate. Read only."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
)
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import func, select

from .profiles import DAY


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
