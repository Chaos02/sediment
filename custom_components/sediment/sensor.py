"""Sensors: current database size and the projected size at the horizon."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SENSOR_DB_SIZE, SENSOR_PROJECTED_SIZE
from .manager import SedimentManager


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the two size sensors."""
    manager: SedimentManager = entry.runtime_data
    async_add_entities(
        [
            SedimentSizeSensor(manager, entry.entry_id, SENSOR_DB_SIZE),
            SedimentSizeSensor(manager, entry.entry_id, SENSOR_PROJECTED_SIZE),
        ]
    )


class SedimentSizeSensor(SensorEntity):
    """A size in GB read from the manager."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: SedimentManager, entry_id: str, kind: str) -> None:
        self._manager = manager
        self._kind = kind
        self._attr_unique_id = f"{entry_id}_{kind}"
        self._attr_translation_key = kind
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Sediment",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.async_add_listener(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        if self._kind == SENSOR_DB_SIZE:
            return self._manager.db_size_gb
        return self._manager.projected_gb
