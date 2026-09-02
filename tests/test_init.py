"""Tests for __init__.py's async_setup_entry wiring."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import is_callback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import async_setup_entry
from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    DOMAIN,
)


async def test_the_per_minute_refresh_timer_is_a_real_hass_callback(hass):
    """Regression test: a bare lambda passed to async_track_time_interval made HA's job-type
    detection dispatch it to an executor thread instead of the event loop, so
    async_write_ha_state() inside async_update_listeners() raised on nearly every tick — caught
    and only logged there, never crashing, so should_run/locked entities silently stayed stale for
    minutes after every HA restart. See CLAUDE.local.md, "should_run silently stale...".
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_config_entry_first_refresh"
        ),
        patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups"),
        patch("homeassistant.helpers.event.async_track_time_interval") as mock_track,
    ):
        await async_setup_entry(hass, entry)

    callback_fn = mock_track.call_args.args[1]
    assert is_callback(callback_fn)
