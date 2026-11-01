"""Profiles, tier schedules and the resolution presets.

Pure Python, no Home Assistant imports (the manager passes entity metadata in).

Profiles (every managed entity has exactly one):

* ``keep``    never thinned, kept to the horizon
* ``default`` the codec with the chosen resolution tiers
* ``coarse``  wider buckets and a shorter horizon
* ``drop``    raw for the raw window only, then deleted
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json

from .codec import TierCfg, tier_for_bucket

PROFILES = ("keep", "default", "coarse", "drop")

DAY = 86400.0

# Resolution presets: (bucket seconds, tier ends at this age in days; None = horizon)
RESOLUTION_PRESETS: dict[str, tuple[tuple[float, float | None], ...]] = {
    # T1 15 min to 1 year, T2 1 h to 3 years, then 6 h
    "fine": ((900.0, 365.0), (3600.0, 1095.0), (21600.0, None)),
    # the reference tiers: 15 min to 6 months, 1 h to 2 years, then 6 h
    "standard": ((900.0, 182.0), (3600.0, 730.0), (21600.0, None)),
    # 1 h to 6 months, 6 h to 2 years, then 1 day
    "coarse": ((3600.0, 182.0), (21600.0, 730.0), (86400.0, None)),
}
# The `coarse` profile: 1 h to 90 days, then 6 h up to its own (shorter) horizon.
COARSE_PROFILE_TIERS: tuple[tuple[float, float | None], ...] = (
    (3600.0, 90.0),
    (21600.0, None),
)
BUCKET_LADDER = (900.0, 3600.0, 21600.0, 86400.0)


@dataclass(frozen=True)
class TierSpec:
    """One tier of a schedule: codec config and the age window it covers."""

    cfg: TierCfg
    start_age_days: float
    end_age_days: float  # exclusive; the last tier ends at the horizon


@dataclass(frozen=True)
class Schedule:
    """The tier ladder of one profile."""

    profile: str
    tiers: tuple[TierSpec, ...]
    horizon_days: float

    def target_index(self, age_days: float) -> int | None:
        """Index of the tier an entity-day of this age should be at.

        -1 means still raw, None means beyond the horizon (to be deleted).
        """
        if age_days >= self.horizon_days:
            return None
        idx = -1
        for i, t in enumerate(self.tiers):
            if age_days >= t.start_age_days:
                idx = i
        return idx

    def buckets(self) -> list[float]:
        return [t.cfg.bucket for t in self.tiers]


def build_schedule(
    profile: str,
    ladder: tuple[tuple[float, float | None], ...],
    raw_days: float,
    horizon_days: float,
) -> Schedule:
    """Turn (bucket, end age) pairs into tier specs clipped to the horizon.

    The first tier starts at the end of the raw window. A tier whose window is empty
    after clipping is dropped.
    """
    tiers: list[TierSpec] = []
    start = float(raw_days)
    for bucket, end in ladder:
        stop = horizon_days if end is None else min(float(end), horizon_days)
        if stop <= start:
            continue
        tiers.append(TierSpec(tier_for_bucket(bucket), start, stop))
        start = stop
    if tiers and tiers[-1].end_age_days < horizon_days:
        last = tiers[-1]
        tiers[-1] = TierSpec(last.cfg, last.start_age_days, horizon_days)
    return Schedule(profile, tuple(tiers), float(horizon_days))


def resolution_ladder(
    resolution: str, custom: list[dict] | None
) -> tuple[tuple[float, float | None], ...]:
    """Ladder for a preset name, or for custom tiers [{bucket_min, until_days}, ...]."""
    if resolution in RESOLUTION_PRESETS:
        return RESOLUTION_PRESETS[resolution]
    out: list[tuple[float, float | None]] = []
    rows = custom or []
    for i, row in enumerate(rows):
        bucket = max(60.0, float(row["bucket_min"]) * 60.0)
        until = None if i == len(rows) - 1 else float(row["until_days"])
        out.append((bucket, until))
    return tuple(out) if out else RESOLUTION_PRESETS["standard"]


def build_schedules(
    resolution: str,
    custom: list[dict] | None,
    raw_days: float,
    horizon_days: float,
    coarse_horizon_days: float,
) -> dict[str, Schedule]:
    """Schedules for `default` and `coarse` (keep and drop have none)."""
    ladder = resolution_ladder(resolution, custom)
    return {
        "default": build_schedule("default", ladder, raw_days, horizon_days),
        "coarse": build_schedule(
            "coarse",
            COARSE_PROFILE_TIERS,
            raw_days,
            min(horizon_days, coarse_horizon_days),
        ),
    }


def coarsen_oldest(resolution: str, custom: list[dict] | None) -> tuple[str, list[dict]]:
    """Return ("custom", tiers) with the oldest tier's bucket one ladder step wider.

    Used by the size cap prompt. Newer tiers are never touched.
    """
    ladder = resolution_ladder(resolution, custom)
    rows: list[dict] = []
    for bucket, end in ladder:
        rows.append(
            {
                "bucket_min": bucket / 60.0,
                "until_days": float(end) if end is not None else 0.0,
            }
        )
    oldest = rows[-1]["bucket_min"] * 60.0
    wider = next((b for b in BUCKET_LADDER if b > oldest), None)
    if wider is None:
        return "custom", rows
    rows[-1]["bucket_min"] = wider / 60.0
    return "custom", rows


def profile_signature(
    rules_text: str,
    resolution: str,
    custom_tiers: list[dict] | None,
    flap_collapse: bool,
    coarse_horizon_days: int,
) -> str:
    """Hash of everything that decides which profile an entity gets.

    Shared by the manager and the offline `tools/merge_history.py`, so an import made with
    the same settings carries the signature the integration computes.
    """
    blob = json.dumps(
        [rules_text.strip(), resolution, custom_tiers, flap_collapse, coarse_horizon_days],
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- rules
@dataclass(frozen=True)
class Rule:
    """One assignment rule: a selector, a profile, an optional flapping override."""

    kind: str  # entity | domain | device_class | integration
    pattern: str
    profile: str
    flap: bool | None = None


@dataclass(frozen=True)
class EntityMeta:
    """What a rule can match besides the entity id."""

    device_class: str | None = None
    integration: str | None = None
    state_class: str | None = None


def parse_rules(text: str) -> tuple[list[Rule], list[str]]:
    """Parse the rules text. One rule per line: `<selector> = <profile> [flap|noflap]`.

    Selectors: an entity id with optional `*` and `?`, `domain:<d>`, `device_class:<c>`,
    `integration:<i>`. Lines starting with `#` and empty lines are ignored.
    Returns (rules, error messages).
    """
    rules: list[Rule] = []
    errors: list[str] = []
    for no, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"line {no}: expected 'selector = profile'")
            continue
        sel, _, rest = line.partition("=")
        sel = sel.strip().lower()
        words = rest.split()
        if not sel or not words:
            errors.append(f"line {no}: expected 'selector = profile'")
            continue
        profile = words[0].lower()
        if profile not in PROFILES:
            errors.append(f"line {no}: unknown profile '{words[0]}'")
            continue
        flap: bool | None = None
        for extra in words[1:]:
            if extra.lower() == "flap":
                flap = True
            elif extra.lower() == "noflap":
                flap = False
            else:
                errors.append(f"line {no}: unknown option '{extra}'")
        kind, pattern = "entity", sel
        for prefix in ("domain", "device_class", "integration"):
            if sel.startswith(prefix + ":"):
                kind, pattern = prefix, sel[len(prefix) + 1 :].strip()
        if not pattern:
            errors.append(f"line {no}: empty selector")
            continue
        rules.append(Rule(kind, pattern, profile, flap))
    return rules, errors


def rule_hits(rule: Rule, entity_id: str, meta: EntityMeta | None) -> bool:
    """Does one rule match this entity?"""
    eid = entity_id.lower()
    if rule.kind == "entity":
        return fnmatch.fnmatchcase(eid, rule.pattern)
    if rule.kind == "domain":
        return eid.split(".", 1)[0] == rule.pattern
    if rule.kind == "device_class":
        return meta is not None and (meta.device_class or "").lower() == rule.pattern
    return meta is not None and (meta.integration or "").lower() == rule.pattern


def resolve(entity_id: str, meta: EntityMeta | None, rules: list[Rule]) -> tuple[str, bool | None]:
    """First matching rule wins. Returns (profile, flap override or None)."""
    for r in rules:
        if rule_hits(r, entity_id, meta):
            return r.profile, r.flap
    return "default", None


def profile_map_hash(signature: str, assignments: dict[str, tuple[str, bool | None]]) -> str:
    """Hash of a profile map: the signature and every entity's profile and flapping rule."""
    blob = json.dumps(
        [signature, sorted([eid, prof, flap] for eid, (prof, flap) in assignments.items())]
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]
