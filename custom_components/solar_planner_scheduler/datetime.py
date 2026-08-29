"""Datetime platform — the manually pinned start time used when a device's manual mode is on."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICES, CONF_MANUAL_START, CONF_NAME, DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(ManualStartDateTime(coordinator, entry, name) for name in device_names)


class ManualStartDateTime(CoordinatorEntity[SolarPlannerSchedulerCoordinator], DateTimeEntity):
    """The start time the coordinator uses for this device while its manual mode switch is on."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_manual_start"
        self._attr_name = f"{device_name} manual start"

    def _find_device(self) -> dict | None:
        return next((d for d in self._entry.options.get(CONF_DEVICES, []) if d[CONF_NAME] == self._device_name), None)

    @property
    def native_value(self) -> datetime | None:
        device = self._find_device()
        stored = device.get(CONF_MANUAL_START) if device else None
        return dt_util.parse_datetime(stored) if stored else None

    async def async_set_value(self, value: datetime) -> None:
        devices = [
            {**d, CONF_MANUAL_START: value.isoformat()} if d[CONF_NAME] == self._device_name else d
            for d in self._entry.options.get(CONF_DEVICES, [])
        ]
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, CONF_DEVICES: devices})
        await self.coordinator.async_request_refresh()
