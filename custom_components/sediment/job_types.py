"""Configuration and counters of one job."""

from __future__ import annotations

from dataclasses import dataclass, field

from .profiles import DAY, EntityMeta, Rule, Schedule


# ---------------------------------------------------------------- job configuration
@dataclass
class JobConfig:
    """Everything a job needs, captured on the event loop before it starts."""

    dry_run: bool
    now: float
    raw_days: float
    horizon_days: float
    coarse_horizon_days: float
    schedules: dict[str, Schedule]
    rules: list[Rule]
    flap_default: bool
    takeover: bool
    meta: dict[str, EntityMeta] = field(default_factory=dict)
    repack: bool = False
    # The takeover is applied: horizon deletion and cleanup do real work even in a dry run,
    # because they are what the recorder's own purge would otherwise have done.
    housekeeping: bool = False
    # Events, short-term statistics and runs older than this many days are cleaned up
    # (the larger of the raw window and the recorder's own keep_days). 0 = use raw_days.
    cleanup_days: float = 0.0
    # Hash of everything that decides how entities are assigned to profiles (rules text,
    # resolution, custom tiers, flapping default). A change re-thins; a flip of an entity's
    # resolved profile (metadata missing for a night) does not.
    profile_sig: str = ""

    @property
    def cleanup_cutoff(self) -> float:
        return self.now - max(self.raw_days, self.cleanup_days) * DAY

    @property
    def cutoff(self) -> float:
        """Rows with last_updated_ts at or after this are inside the raw window."""
        return self.now - self.raw_days * DAY


@dataclass
class JobStats:
    """Counters of one job. Plain numbers and dicts so they serialise to JSON."""

    mode: str = "run"
    phase: str = "start"
    entity_days: int = 0
    rows_seen: int = 0
    rows_deleted: int = 0
    attr_deleted: int = 0
    relinked: int = 0
    protected: int = 0
    dropped_rows: int = 0
    horizon_rows: int = 0
    events_deleted: int = 0
    short_term_deleted: int = 0
    runs_deleted: int = 0
    orphan_attr_deleted: int = 0
    days_simulated: int = 0
    errors: list[str] = field(default_factory=list)
    # dry run: [rows in, kept] by class and bucket, and by class and profile
    share_counts: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    raw_rows_by_class_profile: dict[str, float] = field(default_factory=dict)
    kept_by_profile_tier: dict[str, float] = field(default_factory=dict)
    top_entities: dict[str, list[float]] = field(default_factory=dict)
    pending_days: int = 0
    quarantined: int = 0
    oversize: list[str] = field(default_factory=list)
    stale_meta_deleted: int = 0
    stale_event_types_deleted: int = 0
    reprofiled_entities: int = 0
    housekeeping: bool = False
    coarse_horizon_rows: int = 0
    # real run: days older than the raw window with thinning left when the run ended
    unfinished_days: int | None = None


# lower = more conservative with the data
CONSERVATIVE = {"keep": 0, "default": 1, "coarse": 2, "drop": 3}


def parse_stored(value: str) -> tuple[str, bool | None]:
    profile, _, flap = value.partition("|")
    return profile, {"True": True, "False": False}.get(flap)


def day_key(day: int, profile: str) -> str:
    return f"{day}|{profile}"
