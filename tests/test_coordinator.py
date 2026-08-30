"""Tests for coordinator.py helpers.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt), both for
`coordinator.py`'s module-level `homeassistant` imports to resolve and, for the forecast-points
test below, to set mock entity states via the `hass` fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_FORECAST_ENTITY,
    CONF_IDLE_POWER_THRESHOLD,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_POWER_SENSOR,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.coordinator import (
    DeviceSchedule,
    SolarPlannerSchedulerCoordinator,
    _ceil_to_five_minutes,
    _day_buckets,
    _read_forecast_points,
)


def test_ceil_to_five_minutes_rounds_up_to_the_next_mark():
    now = datetime(2026, 8, 30, 14, 23, 47, 123456, tzinfo=timezone.utc)
    assert _ceil_to_five_minutes(now) == datetime(2026, 8, 30, 14, 25, tzinfo=timezone.utc)


def test_ceil_to_five_minutes_leaves_an_exact_mark_untouched():
    now = datetime(2026, 8, 30, 14, 25, tzinfo=timezone.utc)
    assert _ceil_to_five_minutes(now) == now


def test_todays_buckets_start_on_the_next_five_minute_mark_not_now():
    now = datetime(2026, 8, 30, 14, 23, 47, 123456, tzinfo=timezone.utc)
    buckets = _day_buckets(now, day_offset=0)
    assert buckets[0]["start"] == datetime(2026, 8, 30, 14, 25, tzinfo=timezone.utc)
    assert all(b["start"].minute % 5 == 0 and b["start"].second == 0 for b in buckets)


async def test_read_forecast_points_handles_a_raw_datetime_period_start(hass):
    """Regression test for the coverage_pct-always-0% bug: Solcast stores `period_start` as a
    live `datetime` object in hass.states (only serialized to a string over WS/REST/JSON), and
    dt_util.parse_datetime() used to be called on it unconditionally, raising TypeError and
    silently emptying every forecast point.
    """
    period_start = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {"detailedForecast": [{"period_start": period_start, "pv_estimate": 1.5}]},
    )

    points = _read_forecast_points(hass, "sensor.forecast")

    assert points == [{"time": period_start, "w": 1500.0}]


def _coordinator(hass, previous_schedule=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    if previous_schedule is not None:
        coordinator.data = {"lave_linge": previous_schedule}
    return coordinator


def test_locked_today_slot_reuses_an_imminent_slot_without_recomputing(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now)

    assert slot == {"start": start, "end": end, "coverage_pct": 95}


def test_locked_today_slot_returns_none_when_the_target_is_far_off(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES + 1)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now)

    assert slot is None


def test_locked_today_slot_keeps_a_slot_in_progress_without_a_power_sensor(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now)

    assert slot == {"start": start, "end": end, "coverage_pct": 95}


def test_locked_today_slot_unlocks_when_a_power_sensor_shows_it_never_started(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "0")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power"}
    slot = coordinator._locked_today_slot("lave_linge", device, duration_min=30, now=now)

    assert slot is None


def test_locked_today_slot_stays_locked_when_the_power_sensor_shows_it_running(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1800")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_IDLE_POWER_THRESHOLD: 10}
    slot = coordinator._locked_today_slot("lave_linge", device, duration_min=30, now=now)

    assert slot == {"start": start, "end": end, "coverage_pct": 95}


def test_locked_today_slot_ignores_a_stale_slot_after_the_program_changed(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=60, now=now)

    assert slot is None


def test_locked_today_slot_returns_none_once_the_window_has_fully_elapsed(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=40)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now)

    assert slot is None
