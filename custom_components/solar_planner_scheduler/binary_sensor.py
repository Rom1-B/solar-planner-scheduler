"""Binary sensor platform — on for the duration of a device's scheduled slot.

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

from .const import CONF_DEVICES, CONF_NAME, DOMAIN
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(ShouldRunBinarySensor(coordinator, entry, name) for name in device_names)


class ShouldRunBinarySensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], BinarySensorEntity):
    """On while `now` is within the device's currently scheduled [start, end) window."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_should_run"
        self._attr_name = f"{device_name} should run"

    @property
    def is_on(self) -> bool:
        schedule = self.coordinator.data.get(self._device_name)
        if not schedule or not schedule.start or not schedule.end:
            return False
        now = dt_util.now()
        return schedule.start <= now < schedule.end
