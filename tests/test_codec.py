"""Unit and property tests for the codec (the invariants measured on a reference database)."""

from __future__ import annotations

import random

import pytest

from custom_components.sediment import codec
from custom_components.sediment.codec import DEFAULT_TIERS, TierCfg

DAY = 86400.0
T0 = 1_700_000_000.0 - (1_700_000_000.0 % DAY)  # a UTC midnight


def make_continuous(seed: int, n: int = 3000) -> tuple[list[float], list[float | None], list[str]]:
    """One day of a noisy sensor with a plateau, a ramp, spikes and a persistent step."""
    rng = random.Random(seed)
    q = rng.choice([0.01, 0.1, 0.5])
    t, v = [], []
    ts = T0 + rng.random() * 20
    level = rng.uniform(10, 50)
    for i in range(n):
        ts += rng.uniform(5, 50)
        if ts >= T0 + DAY:
            break
        phase = i / n
        base = level + (8 * phase if 0.3 < phase < 0.5 else 0.0)
        if phase > 0.7:
            base += 12  # persistent step
        x = base + rng.gauss(0, q * rng.choice([0.2, 1.0, 3.0]))
        if rng.random() < 0.004:
            x += rng.choice([-1, 1]) * rng.uniform(15, 30)  # spike
        t.append(ts)
        v.append(round(round(x / q) * q, 6))
    s = [repr(x) for x in v]
    return t, v, s


def make_counter(seed: int) -> tuple[list[float], list[float | None], list[str]]:
    rng = random.Random(seed)
    t, v = [], []
    ts, total = T0, 100.0
    for _ in range(1500):
        ts += rng.uniform(10, 120)
        if ts >= T0 + DAY:
            break
        total += rng.choice([0, 0, 0, 0.5, 1.5])
        if rng.random() < 0.002:
            total = rng.uniform(0, 5)  # reset
        t.append(ts)
        v.append(round(total, 3))
    return t, v, [repr(x) for x in v]


# ---------------------------------------------------------------- classification
def test_classify_continuous_and_enum():
    assert codec.classify("sensor", ["1.0", "2.5", "3.1", "4.0", "5.5"], None) == "continuous"
    assert codec.classify("binary_sensor", ["on", "off"], None) == "enum"
    assert codec.classify("sensor", ["idle", "busy", "idle"], None) == "enum"
    assert codec.classify("sensor", ["unavailable"], None) == "enum"
    assert codec.classify("climate", ["heat", "off"], None) == "attribute"


# ---------------------------------------------------------------- continuous
@pytest.mark.parametrize("seed", range(3))
def test_continuous_invariants(seed):
    t, v, s = make_continuous(seed)
    p = codec.series_params(v)
    tend = t[-1]
    chain = codec.run_chain("continuous", t, v, s, p, DEFAULT_TIERS, tend)
    n = len(t)
    previous = set(range(n))
    for k, (kept, E, active) in enumerate(chain):
        assert kept == sorted(set(kept))
        assert kept[0] == 0 and kept[-1] == n - 1
        assert set(kept) <= previous, "tiers must be subsets of the previous tier"
        previous = set(kept)
        if k == 0:
            r = codec.interp_errors(t, v, kept, E, DEFAULT_TIERS[0].bucket, active)
            # exact extremes in active buckets, 2E in flat buckets (restated invariant)
            assert r["ext"]["exact"] == r["ext"]["buckets"]
            assert r["over2_flat"] == 0
    # the series' global extremes survive every tier
    lo, hi = min(v), max(v)
    for kept, _E, _a in chain:
        vals = {v[i] for i in kept}
        assert lo in vals and hi in vals


def test_spike_and_step_survive():
    t = [T0 + 30 * i for i in range(2800)]
    v = [20.0 + (0.01 if i % 2 else 0.0) for i in range(len(t))]
    spike = 1000
    v[spike] = 45.0
    for i in range(1800, len(t)):
        v[i] += 10.0  # persistent step at 1800
    p = codec.series_params(v)
    kept, _E, _a = codec.thin_continuous(t, v, p, DEFAULT_TIERS[0], t[-1])
    assert spike in kept
    assert 1799 in kept and 1800 in kept


# ---------------------------------------------------------------- gaps
def test_gap_edges_kept_and_short_gaps_dropped():
    t = [T0 + 30 * i for i in range(400)]
    v: list[float | None] = [20.0 + (i % 3) * 0.01 for i in range(400)]
    s = [repr(x) for x in v]
    # long gap: rows 100..119 unavailable (20 * 30 s = 600 s)
    for i in range(100, 120):
        v[i] = None
        s[i] = "unavailable"
    # short gap: rows 200..203 (4 * 30 s = 120 s)
    for i in range(200, 204):
        v[i] = None
        s[i] = "unavailable"
    kept, _E, _a = codec.thin_continuous(t, v, codec.series_params(v), DEFAULT_TIERS[0], t[-1])
    assert {99, 100, 120} <= set(kept)
    _segs, mand = codec.split_segments(t, v, t[-1])
    assert 100 in mand and 99 in mand and 120 in mand
    assert 200 not in mand and 204 not in mand


# ---------------------------------------------------------------- counter
@pytest.mark.parametrize("seed", range(2))
def test_counter_deltas_exact(seed):
    t, v, s = make_counter(seed)
    chain = codec.run_chain("counter", t, v, s, {}, DEFAULT_TIERS, t[-1])
    for (kept, _E, _a), cfg in zip(chain, DEFAULT_TIERS, strict=True):
        _total, bad = codec.counter_exact(t, v, kept, cfg.bucket)
        assert bad == 0, f"counter bucket end values must stay exact (seed {seed})"


def test_counter_reset_kept():
    t = [T0 + 60 * i for i in range(60)]
    v = [float(i) for i in range(30)] + [float(i) for i in range(30)]
    kept = codec.thin_counter(t, v, DEFAULT_TIERS[0], t[-1])
    assert 29 in kept and 30 in kept


# ---------------------------------------------------------------- enum
def test_enum_keeps_every_transition():
    rng = random.Random(1)
    s = ["off"]
    for _ in range(500):
        s.append(s[-1] if rng.random() < 0.7 else rng.choice(["on", "off", "unavailable"]))
    t = [T0 + 20 * i for i in range(len(s))]
    kept = set(codec.thin_enum(t, s))
    trans = [0] + [i for i in range(1, len(s)) if s[i] != s[i - 1]]
    assert set(trans) <= kept
    assert len(s) - 1 in kept
    # attribute-only rows (same state) are dropped
    assert len(kept) <= len(trans) + 1


# ---------------------------------------------------------------- chain over classes
def test_attribute_chain_unions_components():
    t = [T0 + 60 * i for i in range(300)]
    state = ["heat"] * 300
    temp: list[float | None] = [20.0 + (0.1 if i % 2 else 0.0) for i in range(300)]
    temp[150] = 25.0
    comps = [
        ("state", "enum", None, state),
        ("temperature", "continuous", temp, [repr(x) for x in temp]),
    ]
    params = [None, codec.series_params(temp)]
    out = codec.thin_attribute_chain(
        t, state, comps, params, [TierCfg(900.0, 1.0, 0.01, 21600.0)], t[-1]
    )
    assert 150 in out[0]
    assert 0 in out[0] and 299 in out[0]
