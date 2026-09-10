"""Tests for __init__.py's async_setup_entry wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from homeassistant.core import is_callback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import PLATFORMS, _card_version, async_setup_entry
from custom_components.solar_planner_scheduler.const import (
    CONF_DEVICES,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_SENSOR,
    CONF_PROGRAMS,
    DOMAIN,
)


def test_select_platform_is_registered():
    assert "select" in PLATFORMS


def test_card_version_changes_with_the_file_content(tmp_path):
    card_path = tmp_path / "solar-planner-card.js"
    card_path.write_text("console.log('v1');")
    v1 = _card_version(card_path)

    card_path.write_text("console.log('v2');")
    v2 = _card_version(card_path)

    assert v1 != v2


def test_card_version_is_stable_for_unchanged_content(tmp_path):
    card_path = tmp_path / "solar-planner-card.js"
    card_path.write_text("console.log('same');")

    assert _card_version(card_path) == _card_version(card_path)


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


async def test_the_per_minute_timer_also_schedules_a_power_detection_check(hass):
    """The same 1-minute timer that refreshes should_run/locked also schedules
    async_check_power_detection(), so a manually-started program gets caught within about a minute
    instead of waiting for the next full DEFAULT_UPDATE_INTERVAL_MINUTES cycle."""
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
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_check_power_detection"
        ) as mock_check,
    ):
        await async_setup_entry(hass, entry)
        callback_fn = mock_track.call_args.args[1]
        now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        callback_fn(now)
        await hass.async_block_till_done()

    mock_check.assert_called_once_with(now)


async def test_the_per_minute_timer_also_schedules_a_run_progress_check(hass):
    """Same 1-minute timer also schedules async_track_run_progress(), so a program's power trace
    is sampled at minute resolution and its phases get recalibrated right when should_run flips
    back to False, not up to DEFAULT_UPDATE_INTERVAL_MINUTES late."""
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
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_track_run_progress"
        ) as mock_track_run,
    ):
        await async_setup_entry(hass, entry)
        callback_fn = mock_track.call_args.args[1]
        now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        callback_fn(now)
        await hass.async_block_till_done()

    mock_track_run.assert_called_once_with(now)


async def test_async_setup_entry_strips_legacy_device_option_keys(hass):
    """See CLAUDE.local.md, "Per-program activation..." — accepted_date/accepted_day/manual/
    manual_start/selected_program are leftover from the pre-2026-08-31 mechanic, unread by any
    code since then; a real installed entry still carried them, never cleaned up until now."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_POWER_SENSOR: "sensor.lave_linge_power",
                    "accepted_date": "2026-08-29",
                    "accepted_day": "tomorrow",
                    "manual": True,
                    "manual_start": "2026-08-30T15:05:00+00:00",
                    "selected_program": "None",
                    CONF_PROGRAMS: [],
                }
            ]
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_config_entry_first_refresh"
        ),
        patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups"),
        patch("homeassistant.helpers.event.async_track_time_interval"),
    ):
        await async_setup_entry(hass, entry)

    assert entry.options[CONF_DEVICES] == [
        {CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_PROGRAMS: []}
    ]


async def test_async_setup_entry_does_not_touch_already_clean_options(hass):
    clean_devices = [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_PROGRAMS: []}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={CONF_DEVICES: clean_devices},
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_config_entry_first_refresh"
        ),
        patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups"),
        patch("homeassistant.helpers.event.async_track_time_interval"),
        patch("homeassistant.config_entries.ConfigEntries.async_update_entry") as mock_update,
    ):
        await async_setup_entry(hass, entry)

    mock_update.assert_not_called()


async def test_async_setup_entry_strips_legacy_production_and_consumption_entity(hass):
    """production_entity/consumption_entity moved from entry.data to the card's own config
    (2026-09-10): a real installed entry still carries the old keys, never cleaned up until now."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_ENTITY: "sensor.forecast",
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
            "production_entity": "sensor.elec_solar_power",
            "consumption_entity": "sensor.elec_0_power",
        },
        options={},
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.solar_planner_scheduler.coordinator."
            "SolarPlannerSchedulerCoordinator.async_config_entry_first_refresh"
        ),
        patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups"),
        patch("homeassistant.helpers.event.async_track_time_interval"),
    ):
        await async_setup_entry(hass, entry)

    assert entry.data == {CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000}
