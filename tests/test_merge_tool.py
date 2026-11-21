"""The offline merge tool, on synthetic recorder snapshots."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from .common import midnight_days_ago
from .snapshots import make_snapshot, run_tool

NOW = midnight_days_ago(0)


def temp_series(first_day: int, last_day: int, step: int = 60, spike_at: int | None = None):
    """A sensor with a flat plateau, every `step` seconds, from day -first_day to -last_day."""
    rows = []
    t = midnight_days_ago(first_day)
    end = midnight_days_ago(last_day)
    i = 0
    while t < end:
        v = 20.0 + (i % 3) * 0.1
        if spike_at is not None and i == spike_at:
            v = 45.0
        rows.append((t + 1, f"{v:.1f}", {"unit_of_measurement": "C"}))
        t += step
        i += 1
    return rows


def out_rows(path: Path, entity: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT row_id, period, ts, state, old_row_id FROM states WHERE entity_id = ? ORDER BY ts",
            (entity,),
        ).fetchall()
    finally:
        conn.close()


def test_overlap_is_deduped(tmp_path):
    full = temp_series(40, 30)
    cut = len(full) // 2
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": full[: cut + 200]})
    b = make_snapshot(tmp_path / "b.db", {"sensor.t": full[cut:]})
    manifest = run_tool(tmp_path / "out.db", [a, b], NOW)
    assert manifest["rows_merged"] == len(full)
    assert manifest["duplicates_dropped"] == 200
    assert len(manifest["periods"]) == 1 and manifest["gaps"] == []
    rows = out_rows(tmp_path / "out.db", "sensor.t")
    assert len({r[2] for r in rows}) == len(rows), "no timestamp twice"
    assert len(rows) < 0.3 * len(full), "thinned"


def test_thinned_output_keeps_the_codec_invariants(tmp_path):
    rows = temp_series(40, 38, step=30, spike_at=1000)
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": rows})
    manifest = run_tool(tmp_path / "out.db", [a], NOW)
    kept = out_rows(tmp_path / "out.db", "sensor.t")
    kept_ts = {r[2] for r in kept}
    assert rows[0][0] in kept_ts and rows[-1][0] in kept_ts, "series ends stay"
    assert rows[1000][0] in kept_ts, "the spike stays"
    assert kept_ts <= {r[0] for r in rows}, "only source rows, nothing invented"
    assert len(kept) < 0.2 * len(rows) and manifest["rows_kept"] == len(kept)
    # exact extremes of the series survive
    t = [r[0] for r in rows]
    v = [float(r[1]) for r in rows]
    pos = [i for i, ts in enumerate(t) if ts in kept_ts]
    assert max(v[i] for i in pos) == max(v) and min(v[i] for i in pos) == min(v)
    # the day markers say which width each day was thinned to
    conn = sqlite3.connect(tmp_path / "out.db")
    widths = {w for _d, _p, w in conn.execute("SELECT day, profile, width FROM days")}
    conn.close()
    assert widths == {900.0}


def test_sources_are_not_modified(tmp_path):
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": temp_series(40, 39)})
    before = a.read_bytes()
    run_tool(tmp_path / "out.db", [a], NOW)
    assert a.read_bytes() == before
