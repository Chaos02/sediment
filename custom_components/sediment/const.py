"""Constants for Sediment."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sediment"
STORAGE_KEY: Final = "sediment"
STORAGE_VERSION: Final = 1

# --- config / options keys
CONF_HISTORY_DAYS: Final = "history_days"
CONF_HISTORY_CHOICE: Final = "history_choice"
CONF_RESOLUTION: Final = "resolution"
CONF_CUSTOM_TIERS: Final = "custom_tiers"
CONF_SIZE_CAP_GB: Final = "size_cap_gb"
CONF_RULES: Final = "profile_rules"
CONF_FLAP_COLLAPSE: Final = "flapping_collapse"
CONF_TAKEOVER: Final = "takeover"
CONF_RAW_DAYS: Final = "raw_days"
CONF_DRY_RUN: Final = "dry_run"
CONF_COARSE_HORIZON_DAYS: Final = "coarse_horizon_days"

# --- defaults
DEFAULT_HISTORY_DAYS: Final = 1830  # 5 years
DEFAULT_RESOLUTION: Final = "standard"
DEFAULT_RAW_DAYS: Final = 10
DEFAULT_COARSE_HORIZON_DAYS: Final = 365
DEFAULT_FLAP_COLLAPSE: Final = True
DEFAULT_DRY_RUN: Final = True
DEFAULT_TAKEOVER: Final = False

HISTORY_CHOICES: Final = ("1", "2", "5", "10", "custom")
RESOLUTIONS: Final = ("fine", "standard", "coarse", "custom")

# --- recorder compatibility (see recorder_bridge.check_compat)
SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({53})

# --- nightly job
JOB_HOUR: Final = 4
JOB_MINUTE: Final = 30
BACKLOG_LIMIT: Final = 1000  # back off when the recorder queue is longer than this
BACKOFF_SECONDS: Final = 5.0
BACKOFF_GIVE_UP_SECONDS: Final = 3600.0
BATCH_SECONDS: Final = 0.25  # recorder thread time per task; entity-day is the atomic unit
DRY_RUN_MAX_DAYS: Final = 10  # complete days simulated in a dry run
PARAM_REFRESH_DAYS: Final = 7

# --- services
SERVICE_RUN_NOW: Final = "run_now"
SERVICE_CLEAR_QUARANTINE: Final = "clear_quarantine"
SERVICE_IMPORT_HISTORY: Final = "import_history"
SERVICE_EXPORT_PROFILE_MAP: Final = "export_profile_map"
PROFILE_MAP_FILE: Final = "sediment/profile_map.json"
IMPORT_DIR: Final = "sediment"
ATTR_DRY_RUN: Final = "dry_run"

# --- repairs issue ids
ISSUE_TAKEOVER_BLOCKED: Final = "takeover_blocked"
ISSUE_PROTECTING: Final = "takeover_protecting"
ISSUE_DIALECT: Final = "unsupported_database"
ISSUE_SAFETY_NET: Final = "safety_net"
ISSUE_JOB_FAILING: Final = "job_failing"
ISSUE_INFERRED: Final = "retention_inferred"
ALL_ISSUES: Final = (
    ISSUE_TAKEOVER_BLOCKED,
    ISSUE_PROTECTING,
    ISSUE_DIALECT,
    ISSUE_SAFETY_NET,
    ISSUE_JOB_FAILING,
    ISSUE_INFERRED,
)
JOB_FAIL_ISSUE_AFTER: Final = 3

# --- sensors
SENSOR_DB_SIZE: Final = "db_size"
SENSOR_PROJECTED_SIZE: Final = "projected_size"

REPORT_FILE: Final = "sediment/last_report.json"
