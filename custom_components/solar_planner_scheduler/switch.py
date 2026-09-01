"""Switch platform — activates or deactivates one program of a device.

Replaces the old exclusive select.<device>_program: several programs of the same device can now
be active at once (the device is only a mutual-exclusion group in scheduling.py/coordinator.py,
not a "one program at a time" picker).
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS, DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ProgramActiveSwitch(coordinator, entry, device[CONF_NAME], program[CONF_NAME])
        for device in entry.options.get(CONF_DEVICES, [])
        for program in device.get(CONF_PROGRAMS, [])
    )


class ProgramActiveSwitch(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SwitchEntity):
    """Whether this program is currently scheduled.

    The activation state lives in the coordinator's own internal store, not the config entry's
    options — writing it never reloads the whole integration (see coordinator.py's store docstring).
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str, program_name: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_name = device_name
        self._program_name = program_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_{program_name}_active"
        self._attr_name = f"{device_name} {program_name} active"

    def _find_program(self) -> dict | None:
        device = next((d for d in self._entry.options.get(CONF_DEVICES, []) if d[CONF_NAME] == self._device_name), None)
        if device is None:
            return None
        return next((p for p in device.get(CONF_PROGRAMS, []) if p[CONF_NAME] == self._program_name), None)

    @property
    def is_on(self) -> bool:
        program = self._find_program()
        return self.coordinator.is_program_active(self._device_name, self._program_name, program or {})

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_program_active(self._device_name, self._program_name, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_program_active(self._device_name, self._program_name, False)
