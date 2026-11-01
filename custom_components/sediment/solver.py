"""Size estimate: how large will the database be with a given length and resolution.

Pure Python. The model is the reference one: raw rows per day per entity class, times the
kept share of the tier a day falls into, times bytes per row, summed over the tiers of
each profile and extrapolated to the horizon. The kept shares start as the built-in
reference measurements and are replaced by this install's own once a dry run has measured them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from . import codec
from .profiles import DAY, Schedule

# Bytes (measured on a reference SQLite database, schema 53)
B_STATE_ROW = 171.0  # one `states` row with its five indexes
B_ATTR_T0 = 44.0  # attribute share per raw row
# attribute share per kept row at a tier, keyed by bucket seconds (churn grows per row)
B_ATTR_BY_BUCKET = ((900.0, 15.0), (3600.0, 21.0), (21600.0, 26.0))
B_STATS_ROW = 78.0  # one long term statistics row
REST_FALLBACK_BYTES = 442e6  # statistics, short term statistics, events and the rest

# Kept share of the raw rows by class at bucket 900 s, 1 h and 6 h (reference measurements).
PHASE0_SHARES: dict[str, dict[float, float]] = {
    "continuous": {900.0: 0.200, 3600.0: 0.065, 21600.0: 0.0146},
    "enum": {900.0: 0.103, 3600.0: 0.103, 21600.0: 0.103},
    "counter": {900.0: 0.142, 3600.0: 0.044, 21600.0: 0.0095},
    "fewlevel": {900.0: 0.752, 3600.0: 0.752, 21600.0: 0.752},
    "attribute": {900.0: 0.599, 3600.0: 0.424, 21600.0: 0.2908},
}
FLAT_CLASSES = ("enum", "fewlevel")
CLASSES = tuple(PHASE0_SHARES)


def guess_class(entity_id: str, state: str | None, state_class: str | None) -> str:
    """Cheap class guess from the current state, for entities without a sample."""
    domain = entity_id.split(".", 1)[0]
    cls = codec.classify(domain, [state or ""], state_class)
    return "continuous" if cls == "fewlevel" else cls


def kept_share(
    cls: str, bucket: float, measured: dict[str, dict[str, float]] | None = None
) -> float:
    """Kept share of raw rows for a class at a bucket size (log-log interpolation)."""
    pts = dict(PHASE0_SHARES.get(cls, PHASE0_SHARES["continuous"]))
    if measured and cls in measured:
        for b, s in measured[cls].items():
            if s and s > 0:
                pts[float(b)] = float(s)
    xs = sorted(pts)
    if cls in FLAT_CLASSES:
        return min(1.0, max(1e-4, sum(pts.values()) / len(pts)))
    lb = math.log(bucket)
    if bucket <= xs[0]:
        a, b = xs[0], xs[1]
    elif bucket >= xs[-1]:
        a, b = xs[-2], xs[-1]
    else:
        a = max(x for x in xs if x <= bucket)
        b = min(x for x in xs if x >= bucket)
        if a == b:
            return pts[a]
    la, lb_, sa, sb = math.log(a), math.log(b), math.log(pts[a]), math.log(pts[b])
    s = sa + (sb - sa) * (lb - la) / (lb_ - la)
    return min(1.0, max(1e-4, math.exp(s)))


def attr_bytes_per_row(bucket: float) -> float:
    """Attribute bytes per kept row at a tier."""
    for b, v in B_ATTR_BY_BUCKET:
        if bucket <= b:
            return v
    return B_ATTR_BY_BUCKET[-1][1]


@dataclass
class Inputs:
    """What the estimate starts from."""

    rows_per_day: dict[tuple[str, str], float]  # (class, profile) -> raw rows per day
    schedules: dict[str, Schedule]
    raw_days: float
    horizon_days: float
    db_size_bytes: float | None = None
    rows_in_db: float = 0.0
    stats_rows_per_day: float = 0.0
    measured_shares: dict[str, dict[str, float]] | None = None


@dataclass
class Estimate:
    """Projected database size at the horizon."""

    total_bytes: float = 0.0
    states_bytes: float = 0.0
    attr_bytes: float = 0.0
    rest_bytes: float = 0.0
    stats_growth_bytes: float = 0.0
    rows: float = 0.0
    rows_by_tier: dict[str, float] = field(default_factory=dict)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / 1e9

    def over_cap(self, cap_gb: float | None) -> bool:
        return bool(cap_gb) and self.total_gb > float(cap_gb)


def estimate(inp: Inputs) -> Estimate:
    """Project the database size. Newer tiers are never thinned to fit a cap."""
    est = Estimate()
    rows_states = 0.0
    rows_attr_bytes = 0.0
    tier_rows: dict[str, float] = {}

    def add(label: str, n: float, attr_b: float) -> None:
        nonlocal rows_states, rows_attr_bytes
        rows_states += n
        rows_attr_bytes += n * attr_b
        tier_rows[label] = tier_rows.get(label, 0.0) + n

    for (cls, profile), rate in inp.rows_per_day.items():
        if rate <= 0:
            continue
        if profile == "keep":
            add("keep", rate * inp.horizon_days, B_ATTR_T0)
            continue
        add("T0", rate * inp.raw_days, B_ATTR_T0)
        if profile == "drop":
            continue
        sched = inp.schedules.get(profile) or inp.schedules["default"]
        for i, tier in enumerate(sched.tiers):
            days = max(0.0, tier.end_age_days - tier.start_age_days)
            share = kept_share(cls, tier.cfg.bucket, inp.measured_shares)
            add(f"{profile}:T{i + 1}", rate * share * days, attr_bytes_per_row(tier.cfg.bucket))
    est.rows = rows_states
    est.rows_by_tier = tier_rows
    est.states_bytes = rows_states * B_STATE_ROW
    est.attr_bytes = rows_attr_bytes
    if inp.db_size_bytes:
        now_states = inp.rows_in_db * (B_STATE_ROW + B_ATTR_T0)
        est.rest_bytes = max(0.0, inp.db_size_bytes - now_states)
    else:
        est.rest_bytes = REST_FALLBACK_BYTES
    est.stats_growth_bytes = inp.stats_rows_per_day * B_STATS_ROW * inp.horizon_days
    est.total_bytes = est.states_bytes + est.attr_bytes + est.rest_bytes + est.stats_growth_bytes
    return est


def days_to_years(days: float) -> float:
    return days / 365.0


def measured_shares_from_counts(
    counts: dict[str, dict[str, list[float]]],
) -> dict[str, dict[str, float]]:
    """Turn dry run counters into kept shares.

    counts[cls][str(bucket)] = [raw rows in, kept rows out]; only buckets with enough
    input rows to mean something are returned.
    """
    out: dict[str, dict[str, float]] = {}
    for cls, by_bucket in counts.items():
        for bucket, (rows_in, kept) in by_bucket.items():
            if rows_in >= 1000:
                out.setdefault(cls, {})[bucket] = kept / rows_in
    return out


__all__ = [
    "CLASSES",
    "DAY",
    "Estimate",
    "Inputs",
    "attr_bytes_per_row",
    "estimate",
    "guess_class",
    "kept_share",
    "measured_shares_from_counts",
]
