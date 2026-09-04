"""Tests for sensor.py's CurrentPriceSensor and ComputedForecastSensor."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_TARIFF_BANDS,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.coordinator import SolarPlannerSchedulerCoordinator
from custom_components.solar_planner_scheduler.sensor import ComputedForecastSensor, CurrentPriceSensor

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


class _FakePvForecastCoordinator:
    """CoordinatorEntity.__init__() only assigns `coordinator`/`coordinator_context`, so a bare
    object with the `.data` shape ComputedForecastSensor actually reads is enough here.
    """

    def __init__(self, data):
        self.data = data


def _entry_for_computed_forecast(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={"devices": []},
    )
    entry.add_to_hass(hass)
    return entry


async def test_computed_forecast_sensor_native_value_interpolates_at_now(hass):
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    coordinator = _FakePvForecastCoordinator(
        [
            {"time": datetime(2026, 9, 4, 11, 0, tzinfo=timezone.utc), "w": 1000.0},
            {"time": datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc), "w": 2000.0},
        ]
    )
    sensor = ComputedForecastSensor(coordinator, _entry_for_computed_forecast(hass))

    with patch("custom_components.solar_planner_scheduler.sensor.dt_util.now", return_value=now):
        assert sensor.native_value == 1500.0


async def test_computed_forecast_sensor_native_value_is_none_with_no_data(hass):
    sensor = ComputedForecastSensor(_FakePvForecastCoordinator([]), _entry_for_computed_forecast(hass))
    assert sensor.native_value is None


async def test_computed_forecast_sensor_exposes_detailed_forecast_in_solcast_shape(hass):
    coordinator = _FakePvForecastCoordinator(
        [{"time": datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc), "w": 2000.0}]
    )
    sensor = ComputedForecastSensor(coordinator, _entry_for_computed_forecast(hass))

    assert sensor.extra_state_attributes == {
        "detailedForecast": [{"period_start": "2026-09-04T12:00:00+00:00", "pv_estimate": 2.0}]
    }
