"""Sensor platform — exposes this entry's shared base settings for the bundled Lovelace card.

Each device's own next-start time now lives on the datetime platform (datetime.py), merged with
what used to be a separate read-only sensor.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_CONSUMPTION_ENTITY,
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DOMAIN,
)
from .coordinator import SolarPlannerSchedulerCoordinator
from .scheduling import price_at


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BaseConfigSensor(coordinator, entry), CurrentPriceSensor(coordinator, entry)])


class BaseConfigSensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SensorEntity):
    """Read-only mirror of this entry's base settings and fixed loads, for the card."""

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:cog"

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_config"
        self._attr_name = "Solar Planner Scheduler config"

    @property
    def native_value(self) -> int | None:
        return self._entry.data.get(CONF_MAX_SIMULTANEOUS_POWER)

    @property
    def extra_state_attributes(self) -> dict:
        data = self._entry.data
        # Only scheduled devices get a cost from the coordinator's own results; a fixed load's
        # window never changes, so its cost is computed separately (coordinator.fixed_load_cost()).
        price_tracking_enabled = data.get(CONF_PRICE_TRACKING_ENABLED, False)
        fixed_loads = [
            {
                CONF_NAME: load[CONF_NAME],
                CONF_START_TIME: load[CONF_START_TIME],
                "power_profile": load[CONF_POWER_PROFILE],
                "estimated_cost": self.coordinator.fixed_load_cost(load[CONF_NAME]) if price_tracking_enabled else None,
                "currency": self.coordinator.hass.config.currency if price_tracking_enabled else None,
            }
            for load in self._entry.options.get(CONF_FIXED_LOADS, [])
        ]
        # slug uses HA's own slugify() — the same function entity_id generation uses.
        devices = [
            {
                "name": device[CONF_NAME],
                "slug": slugify(device[CONF_NAME]),
                "programs": [
                    {"name": program[CONF_NAME], "slug": slugify(f"{device[CONF_NAME]} {program[CONF_NAME]}")}
                    for program in device.get(CONF_PROGRAMS, [])
                ],
            }
            for device in self._entry.options.get(CONF_DEVICES, [])
        ]
        return {
            "forecast_entity": data.get(CONF_FORECAST_ENTITY),
            "forecast_tomorrow_entity": data.get(CONF_FORECAST_TOMORROW_ENTITY),
            "production_entity": data.get(CONF_PRODUCTION_ENTITY),
            "consumption_entity": data.get(CONF_CONSUMPTION_ENTITY),
            "fixed_loads": fixed_loads,
            "devices": devices,
        }


class CurrentPriceSensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SensorEntity):
    """The €/kWh price right now, for history and as the Energy dashboard's "current price"
    source. None (not the internal neutral price) when tracking is disabled.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_current_price"
        # Fully spelled out so entity_id is namespaced, not a collision-prone sensor.current_price.
        self._attr_name = "Solar Planner Scheduler current price"

    @property
    def native_unit_of_measurement(self) -> str:
        return f"{self.coordinator.hass.config.currency}/kWh"

    @property
    def native_value(self) -> float | None:
        # Reads entry.data directly: coordinator._tariff_bands() can't distinguish "disabled"
        # from "enabled but empty", and price_at([], ...) returns the internal neutral price.
        if not self._entry.data.get(CONF_PRICE_TRACKING_ENABLED, False):
            return None
        tariff_bands = self._entry.data.get(CONF_TARIFF_BANDS, [])
        return price_at(dt_util.now(), tariff_bands)
