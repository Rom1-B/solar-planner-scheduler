"""Sensor platform — exposes each configured device's computed next-start time."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_APPROXIMATE,
    ATTR_COVERAGE_PCT,
    ATTR_END,
    ATTR_PENDING_CHOICE,
    ATTR_POWER_W,
    ATTR_PROFILE,
    ATTR_TODAY_COVERAGE_PCT,
    ATTR_TOMORROW_COVERAGE_PCT,
    CONF_DEVICES,
    CONF_NAME,
    DOMAIN,
)
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities(NextStartSensor(coordinator, entry, name) for name in device_names)


class NextStartSensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SensorEntity):
    """Next best start time computed for a configured device."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry, device_name: str) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_next_start"
        self._attr_name = f"{device_name} next start"

    @property
    def native_value(self):
        schedule = self.coordinator.data.get(self._device_name)
        return schedule.start if schedule else None

    @property
    def extra_state_attributes(self) -> dict:
        schedule = self.coordinator.data.get(self._device_name)
        if not schedule:
            return {}
        attrs = {
            ATTR_COVERAGE_PCT: schedule.coverage_pct,
            ATTR_APPROXIMATE: schedule.approximate,
            ATTR_END: schedule.end.isoformat() if schedule.end else None,
            ATTR_POWER_W: schedule.power_w,
            ATTR_PROFILE: schedule.profile,
        }
        pending = schedule.tomorrow_coverage_pct is not None
        attrs[ATTR_PENDING_CHOICE] = pending
        if pending:
            attrs[ATTR_TODAY_COVERAGE_PCT] = schedule.today_coverage_pct
            attrs[ATTR_TOMORROW_COVERAGE_PCT] = schedule.tomorrow_coverage_pct
        return attrs
