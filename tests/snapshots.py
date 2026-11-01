"""Synthetic recorder snapshots and the merge tool, for the import tests."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sqlite3

TOOL = Path(__file__).resolve().parent.parent / "tools" / "merge_history.py"
_spec = importlib.util.spec_from_file_location("merge_history", TOOL)
merge_history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_history)


def make_snapshot(
    path: Path,
    series: dict[str, list[tuple[float, str, dict | None]]],
    schema: int = 53,
    chain_first: bool = False,
) -> Path:
    """A minimal recorder database: only the tables and columns the tool reads.

    The first row of every entity has a NULL `old_state_id`, as in a real snapshot whose
    older rows were purged (`chain_first=True` points it at a row that does not exist
    instead, which the tool must not treat as a valid chain).
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_changes (change_id INTEGER PRIMARY KEY, schema_version INTEGER);
        CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT);
        CREATE TABLE state_attributes (
            attributes_id INTEGER PRIMARY KEY, hash INTEGER, shared_attrs TEXT);
        CREATE TABLE states (
            state_id INTEGER PRIMARY KEY, metadata_id INTEGER, state TEXT,
            last_updated_ts REAL, last_changed_ts REAL, last_reported_ts REAL,
            old_state_id INTEGER, attributes_id INTEGER, origin_idx INTEGER);
        """
    )
    conn.execute("INSERT INTO schema_changes (schema_version) VALUES (?)", (schema,))
    attr_ids: dict[str, int] = {}
    sid = 0
    for mid, (eid, rows) in enumerate(series.items(), 1):
        conn.execute("INSERT INTO states_meta VALUES (?, ?)", (mid, eid))
        prev = None
        for i, (ts, state, attrs) in enumerate(rows):
            aid = None
            if attrs is not None:
                blob = json.dumps(attrs, sort_keys=True)
                aid = attr_ids.get(blob)
                if aid is None:
                    aid = attr_ids[blob] = len(attr_ids) + 1
                    conn.execute("INSERT INTO state_attributes VALUES (?, ?, ?)", (aid, 0, blob))
            sid += 1
            old = prev
            if i == 0 and chain_first:
                old = 999_999
            conn.execute(
                "INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, mid, state, ts, None, None, old, aid, 0),
            )
            prev = sid
    conn.commit()
    conn.close()
    return path


def run_tool(out: Path, snapshots: list[Path], now: float, *extra: str) -> dict:
    """Run the merge tool in process; returns its manifest."""
    day = datetime.fromtimestamp(now, UTC).date().isoformat()
    assert merge_history.main([str(out), *map(str, snapshots), "--now", day, *extra]) == 0
    return json.loads(Path(str(out) + ".manifest.json").read_text())


def reseal(path: Path) -> None:
    """Recompute the content hash after a test changed an import file on purpose."""
    from custom_components.sediment import importer

    conn = sqlite3.connect(path)
    meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    extra = {k: meta.get(k) for k in importer.HASHED_META}
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'content_hash'",
        (json.dumps(importer.content_hash(conn, extra)),),
    )
    conn.commit()
    conn.close()
