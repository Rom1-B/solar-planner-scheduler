"""Sensor platform — exposes this entry's shared base settings for the bundled Lovelace card.

Each device's own next-start time now lives on the datetime platform (datetime.py), merged with
what used to be a separate read-only sensor.
"""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.energy.data import async_get_manager as async_get_energy_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_SAVINGS_ENABLED,
    CONF_SAVINGS_INVESTMENT,
    CONF_SAVINGS_RESALE_START_DATE,
    CONF_SAVINGS_START_DATE,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DOMAIN,
)
from .coordinator import SolarPlannerSchedulerCoordinator
from .forecast_providers import resolve_forecast_history_entities
from .savings import energy_sources, hourly_sum, payback, price_lookup, solar_gains
from .scheduling import price_at

# Hourly statistics are compiled a few seconds past the hour; this leaves a wide margin.
SAVINGS_REFRESH_MINUTE = 10


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: SolarPlannerSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BaseConfigSensor(coordinator, entry),
            CurrentPriceSensor(coordinator, entry),
            AverageForecastPowerNowSensor(coordinator, entry),
            MinForecastPowerNowSensor(coordinator, entry),
            SolarSavingsSensor(hass, entry),
        ]
    )


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
                "estimated_savings": self.coordinator.fixed_load_savings(load[CONF_NAME]) if price_tracking_enabled else None,
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
            "fixed_loads": fixed_loads,
            "devices": devices,
            "theoretical_forecast": self.coordinator.theoretical_forecast_points(),
            "forecast_history_entities": resolve_forecast_history_entities(self.coordinator.hass, self._entry.entry_id, data),
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


class _CombinedForecastPowerNowSensor(CoordinatorEntity[SolarPlannerSchedulerCoordinator], SensorEntity):
    """Base for AverageForecastPowerNowSensor/MinForecastPowerNowSensor: unlike
    theoretical_forecast_points() (a display-only snapshot, never recorded), these are always-on
    plain numeric sensors so the recorder builds real history for them, comparable in something like
    apexcharts-card against a raw provider's own "power now" sensor — combining every *configured*
    provider regardless of which one is actually selected in select.*_forecast_source. None (not 0)
    below 2 configured providers, where "Average"/"Min" are meaningless. Always created regardless of
    provider count (matching CurrentPriceSensor's pattern for a disabled feature): the entity_id
    must not appear/disappear as the user edits forecast provider settings.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _unique_id_suffix: str
    _name_suffix: str

    def __init__(self, coordinator: SolarPlannerSchedulerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_id_suffix}"
        self._attr_name = f"Solar Planner Scheduler {self._name_suffix}"


class AverageForecastPowerNowSensor(_CombinedForecastPowerNowSensor):
    _unique_id_suffix = "forecast_average_power_now"
    _name_suffix = "forecast average power now"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.average_forecast_power_now()


class MinForecastPowerNowSensor(_CombinedForecastPowerNowSensor):
    _unique_id_suffix = "forecast_min_power_now"
    _name_suffix = "forecast min power now"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.min_forecast_power_now()


class SolarSavingsSensor(SensorEntity):
    """Money gained from solar since the installation date (self-consumption savings plus resale),
    with payback attributes, all derived from the Energy dashboard's configuration.

    Recomputed in full every hour from the recorder's long-term statistics (see savings.py), so
    nothing is persisted here. None when disabled, or when the Energy dashboard has no solar source
    or no import price.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:piggy-bank-outline"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_savings"
        self._attr_name = "Solar Planner Scheduler savings"
        self._attr_native_unit_of_measurement = hass.config.currency
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_time_change(self.hass, self._scheduled_refresh, minute=SAVINGS_REFRESH_MINUTE, second=0)
        )
        self.hass.async_create_task(self.async_refresh_savings())

    @callback
    def _scheduled_refresh(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_refresh_savings())

    def _clear(self) -> None:
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    async def _statistics(self, start: datetime, ids: set[str], units: dict | None, types: set[str]) -> dict:
        if not ids:
            return {}
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, None, ids, "hour", units, types
        )
        return {
            statistic_id: [
                {**row, "start": dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))} for row in rows
            ]
            for statistic_id, rows in stats.items()
        }

    async def async_refresh_savings(self) -> None:
        data = self._entry.data
        if not data.get(CONF_SAVINGS_ENABLED):
            self._clear()
            return
        sources = energy_sources((await async_get_energy_manager(self.hass)).data)
        if not sources.solar or sources.import_price is None:
            self._clear()
            return
        start_date = date.fromisoformat(data[CONF_SAVINGS_START_DATE])
        start = dt_util.start_of_local_day(start_date)
        resale_start = data.get(CONF_SAVINGS_RESALE_START_DATE)
        energy = await self._statistics(start, sources.energy_ids(), {"energy": "kWh"}, {"change"})
        prices = await self._statistics(start, sources.price_entities(), None, {"mean"})

        def lookup(price):
            return price_lookup(price, prices.get(price.entity, []) if price and price.entity else [])

        savings, resale = solar_gains(
            hourly_sum(energy, sources.solar),
            hourly_sum(energy, sources.export),
            lookup(sources.import_price),
            lookup(sources.export_price),
            resale_from=dt_util.start_of_local_day(date.fromisoformat(resale_start)) if resale_start else None,
        )
        gains = savings + resale
        self._attr_native_value = round(gains, 2)
        self._attr_extra_state_attributes = {
            "self_consumption_savings": round(savings, 2),
            "resale_revenue": round(resale, 2),
            **payback(gains, float(data[CONF_SAVINGS_INVESTMENT]), start_date, dt_util.now().date()),
        }
        self.async_write_ha_state()
