"""The offline merge tool, on synthetic recorder snapshots."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from custom_components.sediment import codec

from .common import DAY, midnight_days_ago
from .snapshots import make_snapshot, merge_history, run_tool

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


def test_copy_with_a_valid_chain_is_preferred(tmp_path):
    full = temp_series(40, 35)
    cut = len(full) // 2
    # snapshot B starts inside A: its first rows have no chain (older rows purged)
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": full})
    b = make_snapshot(tmp_path / "b.db", {"sensor.t": full[cut:]})
    manifest = run_tool(tmp_path / "out.db", [b, a], NOW)  # order must not matter
    assert manifest["chain_preferred"] >= 1
    assert manifest["rows_merged"] == len(full)


def test_gaps_stay_gaps(tmp_path):
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": temp_series(60, 50)})
    b = make_snapshot(tmp_path / "b.db", {"sensor.t": temp_series(30, 20)})
    manifest = run_tool(tmp_path / "out.db", [a, b], NOW)
    assert len(manifest["periods"]) == 2
    assert len(manifest["gaps"]) == 1
    gap = manifest["gaps"][0]
    assert 19.5 * DAY < gap["seconds"] < 20.5 * DAY
    rows = out_rows(tmp_path / "out.db", "sensor.t")
    assert not any(gap["start"] < r[2] < gap["end"] for r in rows), "no row invented"
    assert {r[1] for r in rows} == {0, 1}
    # the chain does not cross the gap: each period starts a new one
    first_of_second = min(r for r in rows if r[1] == 1)
    assert first_of_second[4] is None
    assert sum(p["rows_kept"] for p in manifest["periods"]) == manifest["rows_kept"]


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


def test_counter_stays_exact(tmp_path):
    rows = []
    t = midnight_days_ago(40)
    total = 100.0
    for i in range(2000):
        total += 0.5 if i % 4 == 0 else 0
        rows.append((t + 40 * i + 1, f"{total:.1f}", {"state_class": "total_increasing"}))
    a = make_snapshot(tmp_path / "a.db", {"sensor.energy": rows})
    run_tool(tmp_path / "out.db", [a], NOW)
    kept_ts = {r[2] for r in out_rows(tmp_path / "out.db", "sensor.energy")}
    ts = [r[0] for r in rows]
    vals = [float(r[1]) for r in rows]
    pos = [i for i, x in enumerate(ts) if x in kept_ts]
    total_buckets, bad = codec.counter_exact(ts, vals, pos, 900.0)
    assert total_buckets > 0 and bad == 0


def test_transitions_of_a_binary_sensor_all_stay(tmp_path):
    t = midnight_days_ago(40)
    rows = [(t + 500 * i + 1, "on" if i in (3, 4, 9) else "off", {"x": i % 4}) for i in range(60)]
    a = make_snapshot(tmp_path / "a.db", {"binary_sensor.d": rows})
    run_tool(tmp_path / "out.db", [a], NOW)
    kept = out_rows(tmp_path / "out.db", "binary_sensor.d")
    changes = [r for i, r in enumerate(rows) if i == 0 or r[1] != rows[i - 1][1]]
    assert {c[0] for c in changes} <= {r[2] for r in kept}
    assert len(kept) <= len(changes) + 2


def test_age_decides_the_tier_and_the_horizon_drops_rows(tmp_path):
    young = temp_series(40, 39, step=60)
    old = temp_series(250, 249, step=60)  # beyond 6 months: hourly tier
    ancient = temp_series(2000, 1999, step=60)  # beyond the 1830 day history length
    a = make_snapshot(
        tmp_path / "a.db", {"sensor.young": young, "sensor.old": old, "sensor.gone": ancient}
    )
    manifest = run_tool(tmp_path / "out.db", [a], NOW)
    assert len(out_rows(tmp_path / "out.db", "sensor.old")) < len(
        out_rows(tmp_path / "out.db", "sensor.young")
    )
    assert out_rows(tmp_path / "out.db", "sensor.gone") == []
    conn = sqlite3.connect(tmp_path / "out.db")
    widths = {w for _d, _p, w in conn.execute("SELECT day, profile, width FROM days")}
    conn.close()
    assert 3600.0 in widths and 900.0 in widths
    assert manifest["rows_kept"] < manifest["rows_merged"]


def test_profile_rules_apply(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("sensor.noise = drop\nsensor.exact = keep\n")
    rows = temp_series(40, 39)
    a = make_snapshot(
        tmp_path / "a.db",
        {"sensor.noise": rows, "sensor.exact": rows, "sensor.normal": rows},
    )
    run_tool(tmp_path / "out.db", [a], NOW, "--rules", str(rules))
    assert out_rows(tmp_path / "out.db", "sensor.noise") == []
    assert len(out_rows(tmp_path / "out.db", "sensor.exact")) == len(rows)
    assert 0 < len(out_rows(tmp_path / "out.db", "sensor.normal")) < len(rows)


def test_wrong_schema_is_refused(tmp_path):
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": temp_series(40, 39)}, schema=52)
    with pytest.raises(SystemExit, match="schema 52"):
        run_tool(tmp_path / "out.db", [a], NOW)


def test_sources_are_not_modified(tmp_path):
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": temp_series(40, 39)})
    before = a.read_bytes()
    run_tool(tmp_path / "out.db", [a], NOW)
    assert a.read_bytes() == before


def test_content_hash_matches_the_file(tmp_path):
    a = make_snapshot(tmp_path / "a.db", {"sensor.t": temp_series(40, 39)})
    manifest = run_tool(tmp_path / "out.db", [a], NOW)
    conn = sqlite3.connect(tmp_path / "out.db")
    try:
        extra = {key: manifest[key] for key in merge_history.HASHED_META}
        assert merge_history.content_hash(conn, extra) == manifest["content_hash"]
    finally:
        conn.close()
