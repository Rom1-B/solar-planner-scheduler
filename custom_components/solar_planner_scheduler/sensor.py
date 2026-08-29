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
    CONF_CONSUMPTION_ENTITY,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_W,
    CONF_PRODUCTION_ENTITY,
    CONF_START_TIME,
    CONF_SURPLUS_ENTITY,
    DOMAIN,
)
from .coordinator import SolarPlannerSchedulerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_names = [d[CONF_NAME] for d in entry.options.get(CONF_DEVICES, [])]
    async_add_entities([BaseConfigSensor(entry), *(NextStartSensor(coordinator, entry, name) for name in device_names)])


class BaseConfigSensor(SensorEntity):
    """Read-only mirror of this entry's base settings and fixed loads.

    Lets the bundled Lovelace card read which entities to use (forecast/surplus/production/
    consumption/max power) and which fixed loads exist directly from here, instead of requiring
    the same values to be re-entered in the card's own YAML config — one source of truth instead
    of two.
    """

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:cog"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_config"
        self._attr_name = "Solar Planner Scheduler config"

    @property
    def native_value(self) -> int | None:
        return self._entry.data.get(CONF_MAX_SIMULTANEOUS_POWER)

    @property
    def extra_state_attributes(self) -> dict:
        data = self._entry.data
        fixed_loads = [
            {
                CONF_NAME: load[CONF_NAME],
                CONF_START_TIME: load[CONF_START_TIME],
                # The integration only stores fixed loads as flat power_w/duration_min (no
                # multi-phase editor for them yet); wrapped as a single-phase profile here since
                # that's the shape the card's rendering already expects.
                "power_profile": [{CONF_MINUTES: load[CONF_DURATION_MIN], CONF_POWER_W: load[CONF_POWER_W]}],
            }
            for load in self._entry.options.get(CONF_FIXED_LOADS, [])
        ]
        return {
            "forecast_entity": data.get(CONF_FORECAST_ENTITY),
            "forecast_tomorrow_entity": data.get(CONF_FORECAST_TOMORROW_ENTITY),
            "surplus_entity": data.get(CONF_SURPLUS_ENTITY),
            "production_entity": data.get(CONF_PRODUCTION_ENTITY),
            "consumption_entity": data.get(CONF_CONSUMPTION_ENTITY),
            "fixed_loads": fixed_loads,
        }


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
