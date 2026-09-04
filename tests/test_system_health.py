"""Tests for system_health.py."""

from __future__ import annotations

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.coordinator import SolarPlannerSchedulerCoordinator
from custom_components.solar_planner_scheduler.system_health import system_health_info

DEVICES = [
    {
        "name": "Lave-linge",
        "programs": [
            {"name": "Eco", "auto_days": ["mon"]},
            {"name": "Intensif", "auto_days": []},
        ],
    }
]


def _set_up_coordinator(hass) -> SolarPlannerSchedulerCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={"devices": DEVICES},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def test_system_health_reports_not_set_up_with_no_coordinator(hass):
    assert await system_health_info(hass) == {"status": "not set up"}


async def test_system_health_counts_active_programs_and_success(hass):
    _set_up_coordinator(hass)

    info = await system_health_info(hass)

    # "Eco" defaults active (non-empty auto_days), "Intensif" defaults inactive (empty auto_days).
    assert info["active_programs"] == 1
    assert info["last_update_success"] is True
    assert info["open_repairs"] == 0


async def test_system_health_counts_open_repairs_from_the_real_issue_registry(hass):
    _set_up_coordinator(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "failed_to_start_lave_linge_eco",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="failed_to_start",
        translation_placeholders={"device": "Lave-linge", "program": "Eco"},
    )

    info = await system_health_info(hass)

    assert info["open_repairs"] == 1


async def test_system_health_omits_pv_forecast_status_with_no_pv_coordinator(hass):
    _set_up_coordinator(hass)

    info = await system_health_info(hass)

    assert "pv_forecast_last_update_success" not in info


async def test_system_health_reports_pv_forecast_status_when_present(hass):
    coordinator = _set_up_coordinator(hass)

    class _FakePvForecastCoordinator:
        last_update_success = True

    coordinator.pv_forecast_coordinator = _FakePvForecastCoordinator()

    info = await system_health_info(hass)

    assert info["pv_forecast_last_update_success"] is True
