"""Tests for sensor.py's BaseConfigSensor and CurrentPriceSensor."""

from __future__ import annotations

from datetime import UTC, datetime

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FIXED_LOADS,
    CONF_FORECAST_CONFIG_ENTRY_HELIOS,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_W,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DOMAIN,
    FORECAST_PROVIDER_HELIOS,
)
from custom_components.solar_planner_scheduler.coordinator import SolarPlannerSchedulerCoordinator
from custom_components.solar_planner_scheduler.sensor import (
    AverageForecastPowerNowSensor,
    BaseConfigSensor,
    CurrentPriceSensor,
    MinForecastPowerNowSensor,
)
from tests.conftest import register_provider_entities

# A single band covering the whole day makes native_value deterministic regardless of the real
# current time, no need to freeze the clock.
_FLAT_TARIFF_BANDS = [{"start": "00:00", "price": 0.22}]


def _set_up_sensor(hass, extra_data: dict) -> CurrentPriceSensor:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000, **extra_data},
        options={"devices": []},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return CurrentPriceSensor(coordinator, entry)


async def test_current_price_sensor_returns_none_when_tracking_disabled(hass):
    sensor = _set_up_sensor(hass, {CONF_PRICE_TRACKING_ENABLED: False, CONF_TARIFF_BANDS: _FLAT_TARIFF_BANDS})
    assert sensor.native_value is None


async def test_current_price_sensor_returns_price_at_now_when_enabled(hass):
    sensor = _set_up_sensor(hass, {CONF_PRICE_TRACKING_ENABLED: True, CONF_TARIFF_BANDS: _FLAT_TARIFF_BANDS})
    assert sensor.native_value == 0.22


async def test_current_price_sensor_unit_is_currency_per_kwh(hass):
    """Proves the unit isn't hardcoded to EUR: hass.config.currency drives it."""
    hass.config.currency = "USD"
    sensor = _set_up_sensor(hass, {CONF_PRICE_TRACKING_ENABLED: True, CONF_TARIFF_BANDS: _FLAT_TARIFF_BANDS})
    assert sensor.native_unit_of_measurement == "USD/kWh"


_FIXED_LOAD = {
    CONF_NAME: "PAC",
    CONF_START_TIME: "12:00:00",
    CONF_POWER_PROFILE: [{CONF_MINUTES: 60, CONF_POWER_W: 2000.0}],
}


def _set_up_base_config_sensor(hass, extra_data: dict | None = None) -> tuple[BaseConfigSensor, SolarPlannerSchedulerCoordinator]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000, **(extra_data or {})},
        options={"devices": [], CONF_FIXED_LOADS: [_FIXED_LOAD]},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return BaseConfigSensor(coordinator, entry), coordinator


async def test_base_config_sensor_exposes_fixed_load_cost_when_tracking_enabled(hass):
    sensor, coordinator = _set_up_base_config_sensor(hass, {CONF_PRICE_TRACKING_ENABLED: True})
    coordinator._fixed_load_costs["PAC"] = 0.40

    fixed_load = sensor.extra_state_attributes["fixed_loads"][0]
    assert fixed_load["estimated_cost"] == 0.40
    assert fixed_load["currency"] == hass.config.currency


async def test_base_config_sensor_fixed_load_cost_is_none_when_tracking_disabled(hass):
    sensor, coordinator = _set_up_base_config_sensor(hass, {CONF_PRICE_TRACKING_ENABLED: False})
    coordinator._fixed_load_costs["PAC"] = 0.40  # present but must be ignored: tracking is off

    fixed_load = sensor.extra_state_attributes["fixed_loads"][0]
    assert fixed_load["estimated_cost"] is None
    assert fixed_load["currency"] is None


async def test_base_config_sensor_exposes_theoretical_forecast(hass):
    sensor, coordinator = _set_up_base_config_sensor(hass)
    coordinator._theoretical_points = [{"time": datetime(2026, 8, 30, 10, tzinfo=UTC), "w": 1000.0, "w10": 700.0, "w90": 1300.0}]

    assert sensor.extra_state_attributes["theoretical_forecast"] == [
        {"time": "2026-08-30T10:00:00+00:00", "w": 1000.0, "w10": 700.0, "w90": 1300.0}
    ]


async def test_base_config_sensor_exposes_forecast_history_entities(hass):
    entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "2026-08-30T10:00:00+00:00", "watts": 1000}]}}
    )
    sensor, _ = _set_up_base_config_sensor(hass, {CONF_FORECAST_CONFIG_ENTRY_HELIOS: entry_id})

    assert sensor.extra_state_attributes["forecast_history_entities"] == {
        FORECAST_PROVIDER_HELIOS: "sensor.helios_power_now"
    }


def _set_up_forecast_power_now_entry(hass) -> tuple[SolarPlannerSchedulerCoordinator, MockConfigEntry]:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAX_SIMULTANEOUS_POWER: 4000}, options={"devices": []})
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator, entry


async def test_average_forecast_power_now_sensor_reads_the_coordinators_computed_value(hass):
    coordinator, entry = _set_up_forecast_power_now_entry(hass)
    coordinator._average_power_now = 1234.5
    sensor = AverageForecastPowerNowSensor(coordinator, entry)

    assert sensor.native_value == 1234.5
    assert sensor.unique_id == f"{entry.entry_id}_forecast_average_power_now"


async def test_average_forecast_power_now_sensor_is_none_with_fewer_than_two_providers(hass):
    coordinator, entry = _set_up_forecast_power_now_entry(hass)
    sensor = AverageForecastPowerNowSensor(coordinator, entry)

    assert sensor.native_value is None


async def test_min_forecast_power_now_sensor_reads_the_coordinators_computed_value(hass):
    coordinator, entry = _set_up_forecast_power_now_entry(hass)
    coordinator._min_power_now = 456.0
    sensor = MinForecastPowerNowSensor(coordinator, entry)

    assert sensor.native_value == 456.0
    assert sensor.unique_id == f"{entry.entry_id}_forecast_min_power_now"
