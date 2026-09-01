"""Select platform — chooses which program (if any) is currently active for a device."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS, DOMAIN, NONE_PROGRAM
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(ProgramSelectEntity(coordinator, entry, name) for name in device_names)


class ProgramSelectEntity(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SelectEntity):
    """Chooses which program (if any) solar_planner_scheduler should plan for this device.

    The current selection lives in the coordinator's own internal store, not the config entry's
    options — writing it never reloads the whole integration (see coordinator.py's store docstring).
    """

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
        programs = device.get(CONF_PROGRAMS, []) if device else []
        return self.coordinator.get_selected_program(self._device_name, programs)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_selected_program(self._device_name, option)
