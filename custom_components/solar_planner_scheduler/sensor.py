"""Sensor platform — exposes this entry's shared base settings for the bundled Lovelace card.

Each device's own next-start time now lives on the datetime platform (datetime.py), merged with
what used to be a separate read-only sensor.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_START_TIME,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([BaseConfigSensor(entry)])


class BaseConfigSensor(SensorEntity):
    """Read-only mirror of this entry's base settings and fixed loads.

    Lets the bundled Lovelace card read which entities to use (forecast/production/consumption/
    max power) and which fixed loads exist directly from here, instead of requiring the same
    values to be re-entered in the card's own YAML config — one source of truth instead of two.
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
                "power_profile": load[CONF_POWER_PROFILE],
            }
            for load in self._entry.options.get(CONF_FIXED_LOADS, [])
        ]
        # Lets the card discover each device's programs, and the entity_id prefix shared by their
        # switch/datetime/binary_sensor entities, from here instead of requiring them to be
        # re-listed in the card's own YAML config. `slug` is computed with HA's own slugify() —
        # the exact function entity_id generation itself uses — rather than left for the card to
        # approximate from the display name.
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
