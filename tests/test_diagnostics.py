"""Tests for diagnostics.py."""

from __future__ import annotations

from datetime import datetime, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.coordinator import SolarPlannerSchedulerCoordinator
from custom_components.solar_planner_scheduler.diagnostics import async_get_config_entry_diagnostics


def _set_up_coordinator(hass) -> SolarPlannerSchedulerCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={"devices": []},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def test_diagnostics_includes_entry_config_and_store_state(hass):
    coordinator = _set_up_coordinator(hass)
    start = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    coordinator._state["lave_linge"] = {
        "Eco": {
            "active": True,
            "committed": {
                "start": start.isoformat(),
                "end": (start.replace(hour=11)).isoformat(),
                "coverage_pct": 95,
                "forced": False,
            },
        }
    }

    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.entry)

    assert diagnostics["entry_data"] == {CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000}
    assert diagnostics["entry_options"] == {"devices": []}
    assert diagnostics["store"]["lave_linge"]["Eco"]["active"] is True
    assert diagnostics["store"]["lave_linge"]["Eco"]["committed"]["coverage_pct"] == 95
    assert diagnostics["last_update_success"] is True
    assert diagnostics["last_exception"] is None


async def test_diagnostics_reports_the_last_exception_when_present(hass):
    coordinator = _set_up_coordinator(hass)
    coordinator.last_update_success = False
    coordinator.last_exception = ValueError("boom")

    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.entry)

    assert diagnostics["last_update_success"] is False
    assert "boom" in diagnostics["last_exception"]
