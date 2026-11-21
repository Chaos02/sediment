"""The options of the config entry with defaults filled in."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    CONF_COARSE_HORIZON_DAYS,
    CONF_CUSTOM_TIERS,
    CONF_DRY_RUN,
    CONF_FLAP_COLLAPSE,
    CONF_HISTORY_DAYS,
    CONF_RAW_DAYS,
    CONF_RESOLUTION,
    CONF_RULES,
    CONF_SIZE_CAP_GB,
    CONF_TAKEOVER,
    DEFAULT_COARSE_HORIZON_DAYS,
    DEFAULT_DRY_RUN,
    DEFAULT_FLAP_COLLAPSE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_RAW_DAYS,
    DEFAULT_RESOLUTION,
    DEFAULT_TAKEOVER,
)
from .profiles import (
    Schedule,
    build_schedules,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Settings:
    """The options of the config entry with defaults filled in."""

    history_days: int = DEFAULT_HISTORY_DAYS
    resolution: str = DEFAULT_RESOLUTION
    custom_tiers: list[dict] | None = None
    size_cap_gb: float | None = None
    rules_text: str = ""
    flap_collapse: bool = DEFAULT_FLAP_COLLAPSE
    takeover: bool = DEFAULT_TAKEOVER
    raw_days: int = DEFAULT_RAW_DAYS
    dry_run: bool = DEFAULT_DRY_RUN
    coarse_horizon_days: int = DEFAULT_COARSE_HORIZON_DAYS

    @classmethod
    def from_options(cls, opts: dict[str, Any]) -> Settings:
        cap = opts.get(CONF_SIZE_CAP_GB)
        return cls(
            history_days=int(opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)),
            resolution=opts.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
            custom_tiers=opts.get(CONF_CUSTOM_TIERS),
            size_cap_gb=float(cap) if cap else None,
            rules_text=opts.get(CONF_RULES, ""),
            flap_collapse=bool(opts.get(CONF_FLAP_COLLAPSE, DEFAULT_FLAP_COLLAPSE)),
            takeover=bool(opts.get(CONF_TAKEOVER, DEFAULT_TAKEOVER)),
            raw_days=int(opts.get(CONF_RAW_DAYS, DEFAULT_RAW_DAYS)),
            dry_run=bool(opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)),
            coarse_horizon_days=int(
                opts.get(CONF_COARSE_HORIZON_DAYS, DEFAULT_COARSE_HORIZON_DAYS)
            ),
        )

    def schedules(self) -> dict[str, Schedule]:
        return build_schedules(
            self.resolution,
            self.custom_tiers,
            self.raw_days,
            self.history_days,
            self.coarse_horizon_days,
        )
