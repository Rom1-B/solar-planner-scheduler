"""Switch platform — toggles a device between automatic scheduling and a manually pinned start."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, CONF_MANUAL, CONF_NAME, DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(ManualModeSwitch(coordinator, entry, name) for name in device_names)


class ManualModeSwitch(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SwitchEntity):
    """Off (default): the coordinator searches for the best slot. On: it uses manual_start instead."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_manual_mode"
        self._attr_name = f"{device_name} manual mode"

    def _find_device(self) -> dict | None:
        return next((d for d in self._entry.options.get(CONF_DEVICES, []) if d[CONF_NAME] == self._device_name), None)

    @property
    def is_on(self) -> bool:
        device = self._find_device()
        return bool(device.get(CONF_MANUAL, False)) if device else False

    async def _async_set_manual(self, manual: bool) -> None:
        devices = [
            {**d, CONF_MANUAL: manual} if d[CONF_NAME] == self._device_name else d
            for d in self._entry.options.get(CONF_DEVICES, [])
        ]
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, CONF_DEVICES: devices})
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_manual(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_manual(False)
