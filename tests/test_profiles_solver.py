"""Profiles, schedules, rules, the size estimate and the relink plan."""

from __future__ import annotations

from custom_components.sediment import solver
from custom_components.sediment.profiles import (
    DAY,
    EntityMeta,
    build_schedules,
    coarsen_oldest,
    parse_rules,
    resolution_ladder,
    resolve,
)
from custom_components.sediment.recorder_bridge import _plan


# ---------------------------------------------------------------- rules
def test_parse_rules_and_resolve():
    text = """
    # comment
    sensor.host_* = coarse
    device_class:temperature = keep
    integration:my_hub = coarse noflap
    domain:device_tracker = default flap
    sensor.scratch = drop
    """
    rules, errors = parse_rules(text)
    assert errors == []
    assert len(rules) == 5
    meta = EntityMeta(device_class="temperature", integration="x")
    assert resolve("sensor.host_cpu", meta, rules) == ("coarse", None)
    assert resolve("sensor.living", meta, rules) == ("keep", None)
    assert resolve("sensor.other", EntityMeta(integration="my_hub"), rules) == ("coarse", False)
    assert resolve("device_tracker.phone", None, rules) == ("default", True)
    assert resolve("sensor.scratch", None, rules) == ("drop", None)
    assert resolve("light.kitchen", None, rules) == ("default", None)


def test_parse_rules_errors():
    _rules, errors = parse_rules("sensor.a = nonsense\nbroken line\nsensor.b = keep wat")
    assert len(errors) == 3


def test_first_match_wins():
    rules, _ = parse_rules("sensor.a* = keep\nsensor.* = drop")
    assert resolve("sensor.abc", None, rules)[0] == "keep"
    assert resolve("sensor.xyz", None, rules)[0] == "drop"


# ---------------------------------------------------------------- schedules
def test_standard_schedule_matches_phase0_tiers():
    sched = build_schedules("standard", None, 10, 1830, 365)
    d = sched["default"]
    assert [t.cfg.bucket for t in d.tiers] == [900.0, 3600.0, 21600.0]
    assert d.tiers[0].start_age_days == 10
    assert d.tiers[-1].end_age_days == 1830
    assert d.target_index(5) == -1
    assert d.target_index(10) == 0
    assert d.target_index(200) == 1
    assert d.target_index(1000) == 2
    assert d.target_index(1830) is None


def test_fine_and_coarse_presets():
    fine = build_schedules("fine", None, 10, 1830, 365)["default"]
    assert fine.tiers[0].end_age_days == 365 and fine.tiers[1].end_age_days == 1095
    coarse = build_schedules("coarse", None, 10, 1830, 365)["default"]
    assert [t.cfg.bucket for t in coarse.tiers] == [3600.0, 21600.0, 86400.0]


def test_short_horizon_clips_tiers():
    d = build_schedules("standard", None, 10, 100, 365)["default"]
    assert len(d.tiers) == 1
    assert d.tiers[0].end_age_days == 100


def test_coarse_profile_has_own_horizon():
    c = build_schedules("standard", None, 10, 1830, 365)["coarse"]
    assert c.horizon_days == 365
    assert [t.cfg.bucket for t in c.tiers] == [3600.0, 21600.0]


def test_custom_tiers():
    custom = [
        {"bucket_min": 5, "until_days": 30},
        {"bucket_min": 30, "until_days": 365},
        {"bucket_min": 120, "until_days": 0},
    ]
    d = build_schedules("custom", custom, 10, 1000, 365)["default"]
    assert [t.cfg.bucket for t in d.tiers] == [300.0, 1800.0, 7200.0]
    assert d.tiers[1].end_age_days == 365 and d.tiers[2].end_age_days == 1000


def test_coarsen_oldest_only_touches_the_oldest_tier():
    res, tiers = coarsen_oldest("standard", None)
    assert res == "custom"
    assert [t["bucket_min"] for t in tiers] == [15.0, 60.0, 1440.0]
    ladder = resolution_ladder(res, tiers)
    assert ladder[0] == (900.0, 182.0) and ladder[1] == (3600.0, 730.0)
    # already as coarse as it goes: unchanged
    _res2, tiers2 = coarsen_oldest("coarse", None)
    assert [t["bucket_min"] for t in tiers2] == [60.0, 360.0, 1440.0]


# ---------------------------------------------------------------- solver
def test_kept_share_matches_phase0_points():
    assert abs(solver.kept_share("continuous", 900) - 0.200) < 1e-9
    assert abs(solver.kept_share("continuous", 3600) - 0.065) < 1e-9
    assert abs(solver.kept_share("counter", 21600) - 0.0095) < 1e-9
    assert solver.kept_share("continuous", 86400) < solver.kept_share("continuous", 21600)
    assert solver.kept_share("continuous", 300) > solver.kept_share("continuous", 900)
    assert solver.kept_share("continuous", 300) <= 1.0


def test_measured_shares_override():
    m = {"continuous": {"900.0": 0.5}}
    assert abs(solver.kept_share("continuous", 900, m) - 0.5) < 1e-9


def _phase0_inputs(resolution: str = "standard", rates=None) -> solver.Inputs:
    rates = rates or {
        ("continuous", "default"): 518_031,
        ("enum", "default"): 86_527,
        ("counter", "default"): 81_440,
        ("fewlevel", "default"): 23_090,
        ("attribute", "default"): 515,
    }
    return solver.Inputs(
        rows_per_day=rates,
        schedules=build_schedules(resolution, None, 10, 1830, 365),
        raw_days=10,
        horizon_days=1830,
        db_size_bytes=2.34e9,
        rows_in_db=7_665_830,
        stats_rows_per_day=18_493,
    )


def test_estimate_reproduces_phase0_baseline():
    """The reference measurements project 22.9 GB at 5 years for the baseline with every entity default."""
    est = solver.estimate(_phase0_inputs())
    assert 19.0 < est.total_gb < 26.0, est.total_gb
    assert 90e6 < est.rows < 120e6


def test_estimate_orders_resolutions():
    fine = solver.estimate(_phase0_inputs("fine")).total_gb
    std = solver.estimate(_phase0_inputs("standard")).total_gb
    coarse = solver.estimate(_phase0_inputs("coarse")).total_gb
    assert fine > std > coarse


def test_estimate_profiles():
    keep = solver.estimate(_phase0_inputs(rates={("continuous", "keep"): 100_000})).total_gb
    default = solver.estimate(_phase0_inputs(rates={("continuous", "default"): 100_000})).total_gb
    coarse = solver.estimate(_phase0_inputs(rates={("continuous", "coarse"): 100_000})).total_gb
    drop = solver.estimate(_phase0_inputs(rates={("continuous", "drop"): 100_000})).total_gb
    assert keep > default > coarse > drop


def test_over_cap():
    est = solver.estimate(_phase0_inputs())
    assert est.over_cap(10)
    assert not est.over_cap(100)
    assert not est.over_cap(None)


def test_horizon_scales_estimate():
    short = _phase0_inputs()
    short.horizon_days = 365
    short.schedules = build_schedules("standard", None, 10, 365, 365)
    assert solver.estimate(short).total_gb < solver.estimate(_phase0_inputs()).total_gb


def test_guess_class():
    assert solver.guess_class("binary_sensor.x", "on", None) == "enum"
    assert solver.guess_class("sensor.x", "12.5", "measurement") == "continuous"
    assert solver.guess_class("sensor.x", "12.5", "total_increasing") == "counter"
    assert solver.guess_class("sensor.x", "0", "measurement") == "continuous"  # never fewlevel


# ---------------------------------------------------------------- relink plan
def _seq(n, base=100):
    return [(base + i, "1", 1000.0 + i, base + i - 1 if i else None, None) for i in range(n)]


def test_plan_relinks_only_pointers_to_deleted_rows():
    seq = _seq(6)
    plan = _plan(seq, kept={0, 2, 5}, off=0, n_day=6, cutoff=1e9)
    # row 2 pointed at 1 (deleted) and now points at 0; row 5 pointed at 4 and now at 2
    assert dict(plan["relink"]) == {102: 100, 105: 102}
    assert plan["delete_ids"] == [101, 103, 104]


def test_plan_keeps_ha_nulls():
    seq = _seq(5)
    seq[3] = (103, "1", 1003.0, None, None)  # HA stored a NULL in the middle of the series
    plan = _plan(seq, kept={0, 3, 4}, off=0, n_day=5, cutoff=1e9)
    assert plan["relink"] == []  # 103 stays NULL, 104 points at 103 which is kept


def test_plan_context_rows():
    seq = _seq(6)
    # previous context row at index 0, day rows 1..4, next context row at index 5
    plan = _plan(seq, kept={0, 1, 5}, off=1, n_day=4, cutoff=1e9)
    assert plan["delete_ids"] == [102, 103, 104]
    assert dict(plan["relink"]) == {105: 101}  # the next row moves to the last kept row
    # a next row inside the raw window is not touched
    plan2 = _plan(seq, kept={0, 1, 5}, off=1, n_day=4, cutoff=1000.0)
    assert plan2["relink"] == []


def test_day_constant():
    assert DAY == 86400.0
