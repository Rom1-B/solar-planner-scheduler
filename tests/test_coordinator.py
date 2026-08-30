"""Tests for coordinator.py helpers.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt), both for
`coordinator.py`'s module-level `homeassistant` imports to resolve and, for the forecast-points
test below, to set mock entity states via the `hass` fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.solar_planner_scheduler.coordinator import (
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
