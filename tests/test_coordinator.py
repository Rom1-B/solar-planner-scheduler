"""Pure-function tests for coordinator.py helpers that don't need a running Home Assistant core.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt) purely so
`coordinator.py`'s module-level `homeassistant` imports resolve; no `hass` fixture is used here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.solar_planner_scheduler.coordinator import _ceil_to_five_minutes, _day_buckets


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
