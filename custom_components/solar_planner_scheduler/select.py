"""Select platform — chooses which program (if any) is currently active for a device."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS, CONF_SELECTED_PROGRAM, DOMAIN, NONE_PROGRAM
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(ProgramSelectEntity(coordinator, entry, name) for name in device_names)


class ProgramSelectEntity(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SelectEntity):
    """Chooses which program (if any) solar_planner_scheduler should plan for this device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_program"
        self._attr_name = f"{device_name} program"

    def _find_device(self) -> dict | None:
        return next((d for d in self._entry.options.get(CONF_DEVICES, []) if d[CONF_NAME] == self._device_name), None)

    @property
    def options(self) -> list[str]:
        device = self._find_device()
        program_names = [p[CONF_NAME] for p in device.get(CONF_PROGRAMS, [])] if device else []
        return [*program_names, NONE_PROGRAM]

    @property
    def current_option(self) -> str | None:
        device = self._find_device()
        return device.get(CONF_SELECTED_PROGRAM, NONE_PROGRAM) if device else NONE_PROGRAM

    async def async_select_option(self, option: str) -> None:
        devices = [
            {**d, CONF_SELECTED_PROGRAM: option} if d[CONF_NAME] == self._device_name else d
            for d in self._entry.options.get(CONF_DEVICES, [])
        ]
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, CONF_DEVICES: devices})
        await self.coordinator.async_request_refresh()
