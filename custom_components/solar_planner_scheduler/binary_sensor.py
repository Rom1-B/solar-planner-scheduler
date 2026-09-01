"""Binary sensor platform — on for the duration of one program's scheduled slot.

This is the intended trigger point for automations that actually act on a device (turn a switch
on/off) — it turns the "when should this run" question into a plain state trigger.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS, DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ShouldRunBinarySensor(coordinator, entry, device[CONF_NAME], program[CONF_NAME])
        for device in entry.options.get(CONF_DEVICES, [])
        for program in device.get(CONF_PROGRAMS, [])
    )


class ShouldRunBinarySensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], BinarySensorEntity):
    """On while `now` is within this program's currently scheduled [start, end) window."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str, program_name: str
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._program_name = program_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_{program_name}_should_run"
        self._attr_name = f"{device_name} {program_name} should run"

    @property
    def is_on(self) -> bool:
        schedule = self.coordinator.data.get((self._device_name, self._program_name))
        if not schedule or not schedule.start or not schedule.end:
            return False
        now = dt_util.now()
        return schedule.start <= now < schedule.end
