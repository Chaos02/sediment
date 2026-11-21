"""Drop, horizon and cleanup steps."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import delete, distinct, func, select

from .profiles import DAY
from .recorder_thread import chunks


class CleanupMixin:
    """Drop, horizon and cleanup: what the recorder's own purge would otherwise have done."""

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
                        for chunk in chunks(mids, bind):
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
                for chunk in chunks(ids, bind):
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
                for chunk in chunks(list(data_ids), bind):
                    seen.update(
                        d
                        for (d,) in session.execute(
                            select(distinct(Events.data_id)).where(Events.data_id.in_(chunk))
                        ).all()
                    )
                unused = data_ids - seen
                for chunk in chunks(list(unused), bind):
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
            for chunk in chunks(run_ids, bind):
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
            for chunk in chunks([m for m, _e in stale], bind):
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
            for chunk in chunks([t for t, _n in stale_types], bind):
                session.execute(
                    delete(EventTypes)
                    .where(EventTypes.event_type_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            instance.event_type_manager.evict_purged({n for _t, n in stale_types})
            stats.stale_event_types_deleted += len(stale_types)
        yield
