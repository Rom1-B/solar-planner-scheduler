"""Tests for select.py's ForecastSourceSelect."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITIES_HELIOS,
    CONF_FORECAST_ENTITIES_SOLCAST,
    CONF_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
    FORECAST_PROVIDER_AVERAGE,
    FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_MIN,
    FORECAST_PROVIDER_SOLCAST,
)
from custom_components.solar_planner_scheduler.coordinator import SolarPlannerSchedulerCoordinator
from custom_components.solar_planner_scheduler.select import ForecastSourceSelect


def _set_up_select(hass, extra_data: dict) -> tuple[ForecastSourceSelect, SolarPlannerSchedulerCoordinator]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAX_SIMULTANEOUS_POWER: 4000, **extra_data},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return ForecastSourceSelect(coordinator, entry), coordinator


async def test_options_lists_every_combiner_last_when_both_solcast_and_helios_resolved(hass):
    select, _ = _set_up_select(
        hass,
        {
            CONF_FORECAST_ENTITIES_SOLCAST: ["sensor.forecast_today"],
            CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now",
        },
    )
    assert select.options == ["Solcast", "Helios Forecast", "Average", "Min"]


async def test_options_excludes_every_combiner_when_only_one_provider_configured(hass):
    select, _ = _set_up_select(hass, {CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now"})
    assert select.options == ["Helios Forecast"]


async def test_options_is_empty_when_nothing_configured(hass):
    select, _ = _set_up_select(hass, {})
    assert select.options == []


async def test_current_option_falls_back_to_the_first_resolved_provider_when_never_chosen(hass):
    select, _ = _set_up_select(hass, {CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now"})
    assert select.current_option == "Helios Forecast"


async def test_current_option_falls_back_when_stored_average_is_no_longer_valid(hass):
    select, coordinator = _set_up_select(hass, {CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now"})
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_AVERAGE)
    await coordinator.async_shutdown()

    assert select.current_option == "Helios Forecast"


async def test_current_option_falls_back_when_stored_min_is_no_longer_valid(hass):
    select, coordinator = _set_up_select(hass, {CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now"})
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_MIN)
    await coordinator.async_shutdown()

    assert select.current_option == "Helios Forecast"


async def test_current_option_reflects_the_stored_choice(hass):
    select, coordinator = _set_up_select(
        hass,
        {
            CONF_FORECAST_ENTITIES_SOLCAST: ["sensor.forecast_today"],
            CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now",
        },
    )
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_HELIOS)
    await coordinator.async_shutdown()  # cancel the debounced refresh, same as _flush() elsewhere

    assert select.current_option == "Helios Forecast"


async def test_async_select_option_stores_the_matching_provider(hass):
    select, coordinator = _set_up_select(
        hass,
        {
            CONF_FORECAST_ENTITIES_SOLCAST: ["sensor.forecast_today"],
            CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now",
        },
    )
    await coordinator.async_load_state()

    await select.async_select_option("Solcast")
    await coordinator.async_shutdown()

    assert coordinator.active_forecast_source() == FORECAST_PROVIDER_SOLCAST


async def test_async_select_option_stores_the_average_provider(hass):
    select, coordinator = _set_up_select(
        hass,
        {
            CONF_FORECAST_ENTITIES_SOLCAST: ["sensor.forecast_today"],
            CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now",
        },
    )
    await coordinator.async_load_state()

    await select.async_select_option("Average")
    await coordinator.async_shutdown()

    assert coordinator.active_forecast_source() == FORECAST_PROVIDER_AVERAGE


async def test_async_select_option_stores_the_min_provider(hass):
    select, coordinator = _set_up_select(
        hass,
        {
            CONF_FORECAST_ENTITIES_SOLCAST: ["sensor.forecast_today"],
            CONF_FORECAST_ENTITIES_HELIOS: "sensor.helios_power_now",
        },
    )
    await coordinator.async_load_state()

    await select.async_select_option("Min")
    await coordinator.async_shutdown()

    assert coordinator.active_forecast_source() == FORECAST_PROVIDER_MIN
