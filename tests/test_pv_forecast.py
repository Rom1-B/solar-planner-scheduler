"""Tests for pv_forecast.py's compute_pv_forecast(), pure, no `hass`, same philosophy as
test_scheduling.py. async_fetch_open_meteo() isn't tested here (network call), matching this
project's existing choice not to unit-test HTTP fetches.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from custom_components.solar_planner_scheduler.pv_forecast import compute_pv_forecast

LATITUDE = 49.0357
LONGITUDE = -0.3014
ELEVATION = 99


def _sunny_day_weather() -> dict:
    """24 hourly points, shortwave_radiation bell-shaped (0 at night, peak ~700 W/m^2 at midday)."""

    def bell(hour: int) -> float:
        if hour < 6 or hour > 20:
            return 0.0
        return max(0.0, 700 * math.sin((hour - 6) / 14 * math.pi))

    hours = list(range(24))
    sw = [bell(h) for h in hours]
    return {
        "hourly": {
            "time": [f"2026-09-04T{h:02d}:00" for h in hours],
            "shortwave_radiation": sw,
            "direct_normal_irradiance": [v * 0.6 for v in sw],
            "diffuse_radiation": [v * 0.4 for v in sw],
            "temperature_2m": [18.0] * 24,
            "wind_speed_10m": [3.0] * 24,
        }
    }


def test_night_hours_produce_zero_power():
    points = compute_pv_forecast(
        _sunny_day_weather(), LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=14
    )
    midnight = next(p for p in points if p["time"] == datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc))
    assert midnight["w"] == 0.0


def test_midday_produces_positive_power():
    points = compute_pv_forecast(
        _sunny_day_weather(), LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=14
    )
    midday = next(p for p in points if p["time"] == datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc))
    assert midday["w"] > 0.0


def test_loss_pct_derates_the_output_proportionally():
    weather = _sunny_day_weather()
    peak_with_loss = max(
        p["w"]
        for p in compute_pv_forecast(weather, LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=50)
    )
    peak_no_loss = max(
        p["w"]
        for p in compute_pv_forecast(weather, LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=0)
    )
    assert peak_with_loss == peak_no_loss * 0.5


def test_capacity_scales_output_linearly():
    weather = _sunny_day_weather()
    peak_4kwc = max(
        p["w"]
        for p in compute_pv_forecast(weather, LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=14)
    )
    peak_8kwc = max(
        p["w"]
        for p in compute_pv_forecast(weather, LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=8.0, azimuth=156, tilt=35, loss_pct=14)
    )
    assert peak_8kwc == peak_4kwc * 2


def test_returns_sorted_time_w_dicts():
    points = compute_pv_forecast(
        _sunny_day_weather(), LATITUDE, LONGITUDE, ELEVATION, capacity_kwc=4.0, azimuth=156, tilt=35, loss_pct=14
    )
    assert len(points) == 24
    assert all(set(p.keys()) == {"time", "w"} for p in points)
    assert all(isinstance(p["time"], datetime) for p in points)
    assert [p["time"] for p in points] == sorted(p["time"] for p in points)
