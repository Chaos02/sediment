"""Profiles, schedules, rules, the size estimate and the relink plan."""

from __future__ import annotations

from custom_components.sediment import solver
from custom_components.sediment.job_thin import _plan
from custom_components.sediment.profiles import (
    EntityMeta,
    build_schedules,
    parse_rules,
    resolve,
)


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


def test_custom_tiers():
    custom = [
        {"bucket_min": 5, "until_days": 30},
        {"bucket_min": 30, "until_days": 365},
        {"bucket_min": 120, "until_days": 0},
    ]
    d = build_schedules("custom", custom, 10, 1000, 365)["default"]
    assert [t.cfg.bucket for t in d.tiers] == [300.0, 1800.0, 7200.0]
    assert d.tiers[1].end_age_days == 365 and d.tiers[2].end_age_days == 1000


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


def test_over_cap():
    est = solver.estimate(_phase0_inputs())
    assert est.over_cap(10)
    assert not est.over_cap(100)
    assert not est.over_cap(None)


# ---------------------------------------------------------------- relink plan
def _seq(n, base=100):
    return [(base + i, "1", 1000.0 + i, base + i - 1 if i else None, None) for i in range(n)]


def test_plan_relinks_only_pointers_to_deleted_rows():
    seq = _seq(6)
    plan = _plan(seq, kept={0, 2, 5}, off=0, n_day=6, cutoff=1e9)
    # row 2 pointed at 1 (deleted) and now points at 0; row 5 pointed at 4 and now at 2
    assert dict(plan["relink"]) == {102: 100, 105: 102}
    assert plan["delete_ids"] == [101, 103, 104]
