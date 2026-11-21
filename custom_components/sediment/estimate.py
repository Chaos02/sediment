"""Entity metadata, profile map and the size estimate: helpers shared with the flow."""

from __future__ import annotations

from dataclasses import replace
import logging
import os
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance

from . import solver
from .const import (
    DOMAIN,
)
from .profiles import (
    EntityMeta,
    parse_rules,
    resolve,
)
from .settings import Settings

_LOGGER = logging.getLogger(__name__)


ROWSTATS_TTL = 3600.0


# ---------------------------------------------------------------- helpers shared with the flow
def build_meta(hass: HomeAssistant) -> dict[str, EntityMeta]:
    """What rules can match: device class, integration, state class."""
    meta: dict[str, EntityMeta] = {}
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        meta[ent.entity_id] = EntityMeta(
            device_class=ent.device_class or ent.original_device_class,
            integration=ent.platform,
        )
    for st in hass.states.async_all():
        old = meta.get(st.entity_id, EntityMeta())
        meta[st.entity_id] = EntityMeta(
            device_class=st.attributes.get("device_class") or old.device_class,
            integration=old.integration,
            state_class=st.attributes.get("state_class"),
        )
    return meta


def sqlite_db_path(db_url: str) -> str | None:
    """File path of a SQLite recorder URL, else None."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix) or ":memory:" in db_url:
        return None
    return db_url[len(prefix) :].split("?", 1)[0] or None


def db_size_bytes(instance: Any) -> float | None:
    """Size of the recorder database file (with its WAL), SQLite only."""
    path = sqlite_db_path(getattr(instance, "db_url", "") or "")
    if not path or not os.path.exists(path):
        return None
    total = os.path.getsize(path)
    for suffix in ("-wal",):
        if os.path.exists(path + suffix):
            total += os.path.getsize(path + suffix)
    return float(total)


async def async_get_rowstats(hass: HomeAssistant, raw_days: float, force: bool = False) -> dict:
    """Rows per entity over the raw window; cached for an hour."""
    from .rowstats import collect_rowstats

    cache = hass.data.setdefault(DOMAIN, {}).setdefault("rowstats", {})
    now = time.time()
    if (
        not force
        and cache.get("data")
        and now - cache["ts"] < ROWSTATS_TTL
        and cache.get("raw_days") == raw_days
    ):
        return cache["data"]
    instance = get_instance(hass)
    data = await instance.async_add_executor_job(collect_rowstats, instance, now, raw_days)
    cache.update({"data": data, "ts": now, "raw_days": raw_days})
    return data


async def async_estimate_inputs(
    hass: HomeAssistant,
    settings: Settings,
    measured: dict[str, dict[str, float]] | None = None,
) -> solver.Inputs:
    """What the projection starts from: the recorder's real numbers and these settings."""
    from .rowstats import aggregate_by_class_profile

    rs = await async_get_rowstats(hass, settings.raw_days)
    meta = build_meta(hass)
    rules, _errors = parse_rules(settings.rules_text)
    states = hass.states

    def classify(eid: str) -> str:
        st = states.get(eid)
        m = meta.get(eid)
        return solver.guess_class(eid, st.state if st else None, m.state_class if m else None)

    def profile_of(eid: str) -> str:
        return resolve(eid, meta.get(eid), rules)[0]

    rows_per_day = aggregate_by_class_profile(rs, classify, profile_of)
    instance = get_instance(hass)
    return solver.Inputs(
        rows_per_day=rows_per_day,
        schedules=settings.schedules(),
        raw_days=settings.raw_days,
        horizon_days=settings.history_days,
        db_size_bytes=db_size_bytes(instance),
        rows_in_db=float(rs["rows"]),
        stats_rows_per_day=float(rs["stats_rows_per_day"]),
        measured_shares=measured,
    )


async def async_estimate(
    hass: HomeAssistant,
    settings: Settings,
    measured: dict[str, dict[str, float]] | None = None,
) -> solver.Estimate:
    """Projected database size for these settings, from the recorder's real numbers."""
    return solver.estimate(await async_estimate_inputs(hass, settings, measured))


async def async_estimate_variants(
    hass: HomeAssistant,
    settings: Settings,
    measured: dict[str, dict[str, float]] | None,
    variants: dict[str, Settings],
) -> dict[str, float]:
    """Projected size in GB for several settings at once. The row counts are read once."""
    base = await async_estimate_inputs(hass, settings, measured)
    return {
        key: solver.estimate(
            replace(base, schedules=variant.schedules(), horizon_days=variant.history_days)
        ).total_gb
        for key, variant in variants.items()
    }


def build_profile_map(
    settings: Settings,
    data: dict[str, Any],
    meta: dict[str, EntityMeta],
    entity_ids: list[str],
) -> dict[str, tuple[str, bool | None]]:
    """The profile each entity gets right now, as the nightly job resolves it.

    An entity whose metadata is missing keeps its stored profile, as in the job.
    """
    rules, _errors = parse_rules(settings.rules_text)
    stored = data.get("profile_map") or {}
    out: dict[str, tuple[str, bool | None]] = {}
    for eid in entity_ids:
        got = resolve(eid, meta.get(eid), rules)
        if eid not in meta and eid in stored:
            profile, _, flap = stored[eid].partition("|")
            got = (profile, {"True": True, "False": False}.get(flap))
        out[eid] = got
    return out
