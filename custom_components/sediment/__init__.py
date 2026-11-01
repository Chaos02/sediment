"""Sediment: thin old recorder states in place instead of deleting them."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import voluptuous as vol

from .const import (
    ATTR_DRY_RUN,
    DOMAIN,
    SERVICE_CLEAR_QUARANTINE,
    SERVICE_EXPORT_PROFILE_MAP,
    SERVICE_IMPORT_HISTORY,
    SERVICE_RUN_NOW,
)
from .manager import SedimentManager, async_release_for_removal

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

type SedimentConfigEntry = ConfigEntry[SedimentManager]

RUN_NOW_SCHEMA = vol.Schema({vol.Optional(ATTR_DRY_RUN): bool})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the services once."""

    async def run_now(call: ServiceCall) -> None:
        dry_run = call.data.get(ATTR_DRY_RUN)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_run(dry_run)

    async def clear_quarantine(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_clear_quarantine()

    async def import_history(call: ServiceCall) -> ServiceResponse:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError("Sediment is not set up")
        report = await entries[0].runtime_data.async_import_history(
            call.data["path"], call.data.get(ATTR_DRY_RUN, True)
        )
        return {k: v for k, v in report.items() if k not in ("periods", "gaps")} | {
            "periods": len(report.get("periods", [])),
            "gaps": len(report.get("gaps", [])),
        }

    hass.services.async_register(DOMAIN, SERVICE_RUN_NOW, run_now, schema=RUN_NOW_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        import_history,
        schema=vol.Schema(
            {vol.Required("path"): str, vol.Optional(ATTR_DRY_RUN, default=True): bool}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def export_profile_map(call: ServiceCall) -> ServiceResponse:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError("Sediment is not set up")
        return await entries[0].runtime_data.async_export_profile_map()

    hass.services.async_register(DOMAIN, SERVICE_CLEAR_QUARANTINE, clear_quarantine)
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_PROFILE_MAP,
        export_profile_map,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SedimentConfigEntry) -> bool:
    """Set up Sediment from a config entry."""
    manager = SedimentManager(hass, entry)
    try:
        await manager.async_setup()
        entry.runtime_data = manager
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # no timers or tasks of a manager that never came up
        await manager.async_abort()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SedimentConfigEntry) -> bool:
    """Unload: release the recorder's purge settings and stop the job."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_unload()
    return ok


async def _async_reload(hass: HomeAssistant, entry: SedimentConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: SedimentConfigEntry) -> None:
    """The integration is removed: hand purging back to Home Assistant and say so."""
    await async_release_for_removal(hass)
