"""Datetime platform — the computed (or user-forced) next start time for one program of a device.

Merges what used to be split across a read-only sensor (computed value + attributes) and a
separate writable datetime (the manual override): one entity, always showing the current start
time, editable at any moment to force it.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COVERAGE_PCT,
    ATTR_END,
    ATTR_LOCKED,
    ATTR_POWER_W,
    ATTR_PROFILE,
    CONF_DEVICES,
    CONF_NAME,
    CONF_PROGRAMS,
    DOMAIN,
)
from .coordinator import SolarPlannerSchedulerCoordinator, compute_locked


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        StartTimeDateTime(coordinator, entry, device[CONF_NAME], program[CONF_NAME])
        for device in entry.options.get(CONF_DEVICES, [])
        for program in device.get(CONF_PROGRAMS, [])
    )


class StartTimeDateTime(CoordinatorEntity[SolarPlannerSchedulerCoordinator], DateTimeEntity):
    """A program's next start time — computed automatically, or forced by setting a value."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str, program_name: str
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._program_name = program_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_{program_name}_start"
        self._attr_name = f"{device_name} {program_name} start"

    @property
    def native_value(self) -> datetime | None:
        schedule = self.coordinator.data.get((self._device_name, self._program_name)) if self.coordinator.data else None
        return schedule.start if schedule else None

    @property
    def extra_state_attributes(self) -> dict:
        schedule = self.coordinator.data.get((self._device_name, self._program_name)) if self.coordinator.data else None
        if not schedule or not schedule.start:
            return {}
        return {
            ATTR_COVERAGE_PCT: schedule.coverage_pct,
            ATTR_END: schedule.end.isoformat() if schedule.end else None,
            ATTR_POWER_W: schedule.power_w,
            ATTR_PROFILE: schedule.profile,
            ATTR_LOCKED: compute_locked(schedule, dt_util.now()),
        }

    async def async_set_value(self, value: datetime) -> None:
        await self.coordinator.async_set_forced_start(self._device_name, self._program_name, value)
