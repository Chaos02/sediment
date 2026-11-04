"""Import older history from a file made by `tools/merge_history.py`.

The file holds rows that are already thinned, from periods older than the live database.
They are inserted as ordinary `states` rows, entity by entity, one transaction each, as
`RecorderTask`s on the recorder thread (the same pattern as the nightly job). Rules:

* only rows strictly older than the oldest live row of the same entity are inserted
  (an entity without live rows is bounded by the oldest live row of any entity, measured
  once at the first attempt and remembered, so a re-run after an interruption converges),
  so the chain of the live rows is never touched: the first live row keeps the
  `old_state_id` Home Assistant wrote, NULL stays NULL;
* rows that the next nightly run would delete anyway are not inserted: beyond the history
  length, a `coarse` entity beyond its horizon, a `drop` entity beyond the raw window;
* nothing in the file is trusted beyond its content hash: rows with a missing or non-finite
  time, or a `last_changed_ts` after `last_updated_ts`, are rejected; an `old_state_id`
  link is accepted only if it points at an earlier row of the same entity; a state longer
  than Home Assistant stores becomes NULL, as Home Assistant itself does;
* `states_meta` is mapped by `entity_id`; a missing one is created and registered with the
  recorder's own cache; attributes are matched by Home Assistant's hash and text;
* importing the same file twice inserts nothing;
* it refuses a file of another schema, a database that is not SQLite, and an import that the
  recorder's own purge would delete at its next nightly run.

Imports of recorder internals are at module level on purpose: the manager imports this module
only when the service is called, after `compat.check_compat` has passed.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from homeassistant.components.recorder.db_schema import StateAttributes, States, StatesMeta
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.recorder import get_instance, session_scope
from sqlalchemy import func, select, update

from .compat import check_compat, dialect_problem
from .const import (
    BACKLOG_LIMIT,
    BACKOFF_GIVE_UP_SECONDS,
    BACKOFF_SECONDS,
    BATCH_SECONDS,
    IMPORT_DIR,
    SUPPORTED_SCHEMA_VERSIONS,
)
from .profiles import DAY
from .recorder_bridge import run_on_recorder

_LOGGER = logging.getLogger(__name__)

FORMAT = "sediment-import"
FORMAT_VERSION = 1
IMPORT_REPORT_FILE = "sediment/last_import.json"
TOP_ENTITIES = 50
MAX_STATE_LENGTH = 255  # Home Assistant's MAX_LENGTH_STATE_STATE
HASHED_META = ("source_schema", "profile_signature", "profile_mode", "profile_map_hash")


class ImportFileError(Exception):
    """The file is not a usable import database."""


# ---------------------------------------------------------------- the file
def _open(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def content_hash(conn: sqlite3.Connection, extra: dict[str, Any]) -> str:
    """Hash of everything the import writes, as in the tool (the two must agree).

    Every column of every row, the attribute text, the entity classes and profiles, the
    per-day widths, and the manifest fields that decide how the file is treated.
    """
    h = hashlib.sha256()
    h.update((json.dumps(extra, sort_keys=True) + "\n").encode())

    def feed(rows: Any) -> None:
        for row in rows:
            h.update(("\x1f".join(repr(x) for x in row) + "\n").encode())

    feed(
        conn.execute(
            "SELECT s.row_id, s.entity_id, s.ts, s.state, s.last_changed_ts, s.last_reported_ts,"
            " s.old_row_id, s.origin_idx, a.shared_attrs FROM states s"
            " LEFT JOIN attributes a ON a.attr_id = s.attr_id"
            " ORDER BY s.entity_id, s.ts, s.row_id"
        )
    )
    h.update(b"--days\n")
    feed(conn.execute("SELECT day, profile, width FROM days ORDER BY day, profile"))
    h.update(b"--entities\n")
    feed(conn.execute("SELECT entity_id, class, profile FROM entities ORDER BY entity_id"))
    return h.hexdigest()


def read_header(path: str) -> dict[str, Any]:
    """Validate the file and return its manifest plus time span. Runs in an executor."""
    if not os.path.isfile(path):
        raise ImportFileError("the file does not exist")
    try:
        conn = _open(path)
        try:
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            if meta.get("format") != FORMAT or meta.get("version") != FORMAT_VERSION:
                raise ImportFileError(
                    "not a Sediment import file, or a version this release cannot read"
                )
            lo, hi, n = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM states").fetchone()
            extra = {k: meta.get(k) for k in HASHED_META}
            if meta.get("content_hash") != content_hash(conn, extra):
                raise ImportFileError("the content hash does not match, the file is damaged")
            days = [list(r) for r in conn.execute("SELECT day, profile, width FROM days")]
            profiles = dict(conn.execute("SELECT entity_id, profile FROM entities"))
        finally:
            conn.close()
    except sqlite3.Error as err:
        raise ImportFileError(f"not a readable import database ({err})") from err
    meta.update(
        {
            "first_ts": lo,
            "last_ts": hi,
            "rows_in_file": n,
            "days": days,
            "entity_profiles": profiles,
        }
    )
    return meta


def check_import_path(config_dir: str, is_allowed: Any, path: str) -> str | None:
    """Why this path may not be used, or None. Blocking: runs in an executor.

    Files belong in `<config>/sediment/`. Anything under `<config>/www` is refused because
    that folder is served to the network without a login.
    """
    real = os.path.realpath(path)
    www = os.path.realpath(os.path.join(config_dir, "www"))
    if real == www or real.startswith(www + os.sep):
        return (
            "files under <config>/www are served over the network without a login. "
            "Put the import file in <config>/sediment/ instead"
        )
    own = os.path.realpath(os.path.join(config_dir, IMPORT_DIR))
    if real == own or real.startswith(own + os.sep) or is_allowed(path):
        return None
    return (
        "this path is not allowed. Put the file in <config>/sediment/ "
        "(or in a folder listed in allowlist_external_dirs)"
    )


# ---------------------------------------------------------------- the job
@dataclass
class ImportConfig:
    path: str
    write: bool
    # rows older than this would be deleted by Sediment's history length anyway
    horizon_ts: float | None = None
    periods: list[dict] = field(default_factory=list)
    # the oldest live row of any entity, measured once per file (None = empty database)
    global_oldest: float | None = None
    global_known: bool = False
    # entity id -> (profile, flap) as the nightly job would resolve it now
    profiles: dict[str, tuple[str, bool | None]] = field(default_factory=dict)
    now: float = 0.0
    raw_days: float = 10.0
    coarse_days: float = 365.0  # horizon of the `coarse` profile


@dataclass
class ImportStats:
    entities: int = 0
    entities_with_rows: int = 0
    rows_in_file: int = 0
    rows_to_insert: int = 0  # inserted, or in a dry run what would be
    skipped_not_older: int = 0
    would_be_deleted: int = 0  # beyond the history length, the coarse horizon, the raw window
    rejected_rows: int = 0
    chains_rejected: int = 0
    states_too_long: int = 0
    attributes_created: int = 0
    attributes_reused: int = 0
    meta_created: int = 0
    by_period: dict[int, int] = field(default_factory=dict)
    per_entity: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def read_global_oldest(instance: Any) -> float | None:
    """Oldest live `states` row of any entity. Runs on the recorder thread."""
    with session_scope(session=instance.get_session(), read_only=True) as session:
        return session.execute(select(func.min(States.last_updated_ts))).scalar()


class ImportJob:
    """Plan or perform the import. `step` runs on the recorder thread, like `Job.step`."""

    def __init__(self, cfg: ImportConfig) -> None:
        self.cfg = cfg
        self.stats = ImportStats()
        self.cancelled = False
        self.finished = False
        self.global_oldest: float | None = cfg.global_oldest if cfg.global_known else None
        self._gen = None
        self._attr_ids: dict[tuple[int, str], int] = {}

    def step(self, instance: Any) -> bool:
        if self.finished:
            return True
        if self._gen is None:
            self._gen = self._work(instance)
        deadline = time.monotonic() + BATCH_SECONDS
        while True:
            if self.cancelled:
                self._gen.close()
                self.finished = True
                return True
            try:
                next(self._gen)
            except StopIteration:
                self.finished = True
                return True
            except Exception as err:
                # one entity is one transaction: it rolled back, nothing is half imported
                _LOGGER.exception("Sediment import failed")
                if len(self.stats.errors) < 50:
                    self.stats.errors.append(repr(err))
                self.finished = True
                return True
            if time.monotonic() >= deadline:
                return False

    def _work(self, instance: Any):
        conn = _open(self.cfg.path)
        try:
            with session_scope(session=instance.get_session(), read_only=True) as session:
                live = dict(
                    session.execute(
                        select(StatesMeta.entity_id, func.min(States.last_updated_ts))
                        .join(States, States.metadata_id == StatesMeta.metadata_id)
                        .group_by(StatesMeta.entity_id)
                    ).all()
                )
            if not self.cfg.global_known:
                self.global_oldest = read_global_oldest(instance)
            entities = [r[0] for r in conn.execute("SELECT entity_id FROM entities ORDER BY 1")]
            self.stats.entities = len(entities)
            for eid in entities:
                bound = live.get(eid)
                if bound is None:
                    bound = self.global_oldest
                self._entity(instance, conn, eid, math.inf if bound is None else bound)
                yield
            if self.cfg.write:
                with session_scope(session=instance.get_session(), read_only=True) as session:
                    instance.states_manager.load_from_db(session)
        finally:
            conn.close()

    def _deleted_anyway(self, eid: str, ts: float) -> bool:
        """Would the nightly job delete a row of this age of this entity?"""
        cfg = self.cfg
        if cfg.horizon_ts is not None and ts < cfg.horizon_ts:
            return True
        profile = cfg.profiles.get(eid, ("default", None))[0]
        if profile == "drop":
            return ts < cfg.now - cfg.raw_days * DAY
        if profile == "coarse":
            return ts < cfg.now - cfg.coarse_days * DAY
        return False

    def _entity(self, instance: Any, conn: sqlite3.Connection, eid: str, bound: float) -> None:
        stats = self.stats
        raw = conn.execute(
            "SELECT row_id, period, ts, state, last_changed_ts, last_reported_ts, old_row_id,"
            " attr_id, origin_idx FROM states WHERE entity_id = ? ORDER BY ts, row_id",
            (eid,),
        ).fetchall()
        stats.rows_in_file += len(raw)
        valid = []
        for r in raw:
            ts, changed, reported = r[2], r[4], r[5]
            if (
                not _finite(ts)
                or (changed is not None and (not _finite(changed) or changed > ts))
                or (reported is not None and not _finite(reported))
            ):
                stats.rejected_rows += 1
                continue
            valid.append(r)
        older = [r for r in valid if r[2] < bound]
        stats.skipped_not_older += len(valid) - len(older)
        take = [r for r in older if not self._deleted_anyway(eid, r[2])]
        stats.would_be_deleted += len(older) - len(take)
        if not take:
            return
        links = self._links(valid, take)
        if self.cfg.write:
            self._write(instance, conn, eid, take, links)
        stats.entities_with_rows += 1
        stats.rows_to_insert += len(take)
        stats.per_entity[eid] = len(take)
        for r in take:
            stats.by_period[r[1]] = stats.by_period.get(r[1], 0) + 1

    def _links(self, valid: list[tuple], take: list[tuple]) -> list[tuple[int, int]]:
        """Accepted `old_state_id` links (row id, target row id).

        A link counts only if its target is an earlier row of the same entity in the file.
        Anything else is written as NULL; a target that was merely left out (for example
        beyond the history length) is a NULL without complaint.
        """
        pos = {r[0]: i for i, r in enumerate(valid)}
        keep = {r[0] for r in take}
        out = []
        for r in take:
            link = r[6]
            if link is None:
                continue
            j = pos.get(link)
            if j is None or j >= pos[r[0]] or valid[j][2] > r[2]:
                self.stats.chains_rejected += 1
            elif link in keep:
                out.append((r[0], link))
        return out

    # ------------------------------------------------------------ writing one entity
    def _metadata_id(self, instance: Any, eid: str) -> int:
        manager = instance.states_meta_manager
        with session_scope(session=instance.get_session()) as session:
            mid = manager.get(eid, session, True)
            if mid is None:
                meta = StatesMeta(entity_id=eid)
                session.add(meta)
                session.flush()
                mid = meta.metadata_id
                self.stats.meta_created += 1
        with session_scope(session=instance.get_session(), read_only=True) as session:
            manager.get(eid, session, True)  # now committed: let the recorder's cache know
        return mid

    def _attributes(
        self, instance: Any, session: Any, conn: sqlite3.Connection, attr_ids: set[int]
    ) -> dict[int, int]:
        """Map import attribute ids to live ones, reusing rows with the same hash and text."""
        out: dict[int, int] = {}
        if not attr_ids:
            return out
        wanted: dict[int, tuple[int, str]] = {}
        todo = sorted(attr_ids)
        for i in range(0, len(todo), 500):
            chunk = todo[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for aid, blob in conn.execute(
                f"SELECT attr_id, shared_attrs FROM attributes WHERE attr_id IN ({marks})", chunk
            ):
                wanted[aid] = (StateAttributes.hash_shared_attrs_bytes(blob.encode()), blob)
        missing: dict[tuple[int, str], list[int]] = {}
        for aid, key in wanted.items():
            if key in self._attr_ids:
                out[aid] = self._attr_ids[key]
                self.stats.attributes_reused += 1
            else:
                missing.setdefault(key, []).append(aid)
        hashes = sorted({h for h, _b in missing})
        for i in range(0, len(hashes), instance.max_bind_vars):
            chunk = hashes[i : i + instance.max_bind_vars]
            for live_id, h, blob in session.execute(
                select(
                    StateAttributes.attributes_id,
                    StateAttributes.hash,
                    StateAttributes.shared_attrs,
                ).where(StateAttributes.hash.in_(chunk))
            ).all():
                key = (h, blob)
                if key in missing:
                    self._attr_ids[key] = live_id
                    for aid in missing.pop(key):
                        out[aid] = live_id
                        self.stats.attributes_reused += 1
        for (h, blob), aids in missing.items():
            row = StateAttributes(hash=h, shared_attrs=blob)
            session.add(row)
            session.flush()
            self._attr_ids[(h, blob)] = row.attributes_id
            for aid in aids:
                out[aid] = row.attributes_id
            self.stats.attributes_created += 1
        if len(self._attr_ids) > 100_000:
            self._attr_ids.clear()
        return out

    def _write(
        self,
        instance: Any,
        conn: sqlite3.Connection,
        eid: str,
        take: list[tuple],
        links: list[tuple[int, int]],
    ) -> None:
        mid = self._metadata_id(instance, eid)
        with session_scope(session=instance.get_session()) as session:
            amap = self._attributes(
                instance, session, conn, {r[7] for r in take if r[7] is not None}
            )
            objs = []
            for r in take:
                state = r[3]
                if state is not None and len(state) > MAX_STATE_LENGTH:
                    state = None  # what Home Assistant stores for a state it cannot hold
                    self.stats.states_too_long += 1
                objs.append(
                    States(
                        metadata_id=mid,
                        state=state,
                        last_updated_ts=r[2],
                        last_changed_ts=r[4],
                        last_reported_ts=r[5],
                        attributes_id=amap.get(r[7]) if r[7] is not None else None,
                        origin_idx=r[8],
                        old_state_id=None,
                    )
                )
            session.add_all(objs)
            session.flush()
            new_id = {r[0]: o.state_id for r, o in zip(take, objs, strict=True)}
            chain = [{"state_id": new_id[a], "old_state_id": new_id[b]} for a, b in links]
            if chain:
                session.execute(update(States), chain)


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


# ---------------------------------------------------------------- the service, loop side
async def async_import_history(manager: Any, path: str, dry_run: bool = True) -> dict[str, Any]:
    """Validate, then plan (dry run) or perform the import. Returns the report."""
    hass = manager.hass
    started = time.monotonic()
    if manager._closing or manager.running:
        raise ServiceValidationError("Sediment is busy with a job, try again when it is done")
    why = await hass.async_add_executor_job(
        check_import_path, hass.config.config_dir, hass.config.is_allowed_path, path
    )
    if why:
        raise ServiceValidationError(f"Cannot import {os.path.basename(path)}: {why}")
    try:
        header = await hass.async_add_executor_job(read_header, path)
    except ImportFileError as err:
        raise ServiceValidationError(f"Cannot import {os.path.basename(path)}: {err}") from err

    from .manager import build_meta, build_profile_map

    instance = get_instance(hass)
    now = time.time()
    s = manager.settings
    problems: list[str] = []
    if header.get("source_schema") not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"the file was made from recorder schema {header.get('source_schema')}, "
            f"only {sorted(SUPPORTED_SCHEMA_VERSIONS)} can be imported"
        )
    if reason := dialect_problem(instance):
        problems.append(reason)
    problems.extend(check_compat(instance))
    first_ts = header.get("first_ts")
    if first_ts is not None and instance.auto_purge:
        age_days = (now - first_ts) / DAY
        if age_days > instance.keep_days:
            problems.append(
                f"the recorder's own purge is on and keeps {instance.keep_days} days, so it would "
                f"delete the imported rows (the oldest is {age_days:.0f} days old) at its next "
                "nightly run. Turn the takeover on first: tick it in the options and switch dry "
                "run off"
            )
    content = header["content_hash"]
    data = manager.data
    done = data.get("imports", {}).get(content)

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "file": os.path.basename(path),
        "content_hash": content,
        "problems": problems,
        "already_imported": bool(done),
        "profile_mode": header.get("profile_mode"),
        "periods": header.get("periods", []),
        "gaps": header.get("gaps", []),
    }
    if problems and not dry_run:
        raise ServiceValidationError("Import refused: " + "; ".join(problems))
    if done and not dry_run:
        report.update({"rows_inserted": 0, "note": "this file was imported before"})
        return report

    # profiles as the nightly job resolves them now: for the rows it would delete anyway
    # and to decide whether the days can be marked thinned
    entity_ids = sorted(header["entity_profiles"])
    current = build_profile_map(s, data, build_meta(hass), entity_ids)

    # the bound for entities without live rows is measured once per file and remembered, so
    # that a re-run after an interruption sees the same database as the first attempt
    runs = data.setdefault("import_runs", {})
    if content in runs:
        global_oldest = runs[content]["global_oldest"]
    else:
        global_oldest = await run_on_recorder(instance, read_global_oldest)
        if not dry_run:
            runs[content] = {"global_oldest": global_oldest, "at": now}
            await manager.store.async_save(deepcopy(data))

    cfg = ImportConfig(
        path=path,
        write=not dry_run,
        horizon_ts=now - s.history_days * DAY,
        periods=report["periods"],
        global_oldest=global_oldest,
        global_known=True,
        profiles=current,
        now=now,
        raw_days=float(s.raw_days),
        coarse_days=float(min(s.coarse_horizon_days, s.history_days)),
    )
    job = ImportJob(cfg)
    manager.running = True
    manager._idle.clear()
    manager.job = job
    manager._run_task = asyncio.current_task()
    waited = 0.0
    try:
        while True:
            if manager._closing or job.cancelled:
                job.cancelled = True
                break
            if instance.backlog > BACKLOG_LIMIT:
                await asyncio.sleep(BACKOFF_SECONDS)
                waited += BACKOFF_SECONDS
                if waited > BACKOFF_GIVE_UP_SECONDS:
                    job.stats.errors.append("recorder backlog did not drain, import stopped")
                    job.cancelled = True
                    break
                continue
            fut = run_on_recorder(instance, job.step)
            try:
                finished = await asyncio.shield(fut)
            except asyncio.CancelledError:
                job.cancelled = True
                with suppress(Exception):
                    await asyncio.wait([fut], timeout=30)
                raise
            if finished:
                break
    finally:
        manager.running = False
        manager.job = None
        manager._run_task = None
        manager._idle.set()

    st = job.stats
    report.update(
        {
            "seconds": round(time.monotonic() - started, 1),
            "entities_in_file": st.entities,
            "entities_with_rows": st.entities_with_rows,
            "rows_in_file": st.rows_in_file,
            "rows_would_insert" if dry_run else "rows_inserted": st.rows_to_insert,
            "skipped_not_older_than_live": st.skipped_not_older,
            "skipped_would_be_deleted": st.would_be_deleted,
            "rejected_rows": st.rejected_rows,
            "chains_rejected": st.chains_rejected,
            "states_too_long": st.states_too_long,
            "attributes_created": st.attributes_created,
            "attributes_reused": st.attributes_reused,
            "states_meta_created": st.meta_created,
            "by_period": {str(i): st.by_period.get(i, 0) for i in range(len(report["periods"]))},
            "per_entity": dict(sorted(st.per_entity.items(), key=lambda kv: -kv[1])[:TOP_ENTITIES]),
            "errors": st.errors,
        }
    )
    if not dry_run and not st.errors and not job.cancelled:
        await _finish(manager, header, job, report, current)
    full = deepcopy(report)
    full["per_entity"] = dict(sorted(st.per_entity.items()))
    await hass.async_add_executor_job(_write_report, hass, full)
    if not dry_run and not manager._closing:
        await manager.async_update_projection()
        await manager.store.async_save(deepcopy(manager.data))
        await manager.async_refresh()
    return report


async def _finish(
    manager: Any,
    header: dict,
    job: ImportJob,
    report: dict,
    current: dict[str, tuple[str, bool | None]],
) -> None:
    """Remember the file, and tell the nightly job which imported days are already thinned."""
    data = manager.data
    data.setdefault("imports", {})[header["content_hash"]] = {
        "at": time.time(),
        "rows": job.stats.rows_to_insert,
    }
    data.get("import_runs", {}).pop(header["content_hash"], None)
    # Days count as thinned only if the file was made from a profile map of the current
    # signature and every entity in it still has the profile it was thinned with.
    same_signature = header.get("profile_signature") == manager._profile_sig()
    mismatches = sum(
        1
        for eid, prof in header["entity_profiles"].items()
        if current.get(eid, ("", None))[0] != prof
    )
    thin_again = not (header.get("profile_mode") == "map" and same_signature and mismatches == 0)
    marked = 0
    if not thin_again and job.global_oldest is not None:
        # only days that end before every live row: no live entity shares them
        last_day = int(job.global_oldest // DAY) - 1
        applied = data.setdefault("applied", {})
        for day, profile, width in header.get("days", []):
            if day <= last_day:
                key = f"{day}|{profile}"
                applied[key] = max(applied.get(key, 0.0), float(width))
                marked += 1
    report["days_marked_thinned"] = marked
    report["thin_again"] = thin_again
    report["profile_mismatches"] = mismatches
    if thin_again:
        if header.get("profile_mode") != "map":
            why = "it was made without a profile map (export one with sediment.export_profile_map)"
        elif not same_signature:
            why = "its profile settings are not the current ones"
        else:
            why = f"{mismatches} entities now have another profile than when it was made"
        report["note"] = (
            f"The days are not marked as thinned because {why}; the nightly job thins them."
        )
    await manager.store.async_save(deepcopy(data))


def _write_report(hass: Any, report: dict) -> None:
    path = hass.config.path(IMPORT_REPORT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    os.replace(tmp, path)
