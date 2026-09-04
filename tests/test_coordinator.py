"""Tests for coordinator.py helpers.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt), both for
`coordinator.py`'s module-level `homeassistant` imports to resolve and, for the forecast-points
test below, to set mock entity states via the `hass` fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    NONE_PROGRAM,
)
from custom_components.solar_planner_scheduler.coordinator import (
    FAILED_TO_START_REPAIR_THRESHOLD,
    NIGHT_EXTENSION_HOURS,
    DeviceSchedule,
    SolarPlannerSchedulerCoordinator,
    _ceil_to_five_minutes,
    _day_buckets,
    _is_relevant_today,
    _migrate_legacy_state,
    _read_forecast_points,
    compute_locked,
)


async def _flush(coordinator) -> None:
    """Cancel the debounced async_request_refresh() every state-setter kicks off.

    These tests only care about the synchronous store write each setter makes before requesting a
    refresh, not the refresh itself — but a pending Debouncer call_later() timer left dangling
    fails the test harness's teardown check for lingering timers.
    """
    await coordinator.async_shutdown()


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


def test_todays_buckets_extend_past_midnight_by_night_extension_hours():
    now = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
    buckets = _day_buckets(now, day_offset=0)
    last_start = buckets[-1]["start"]
    assert last_start.date() == datetime(2026, 8, 31, tzinfo=timezone.utc).date()
    assert last_start < datetime(2026, 8, 30, 23, 55, tzinfo=timezone.utc) + timedelta(hours=NIGHT_EXTENSION_HOURS)


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


# --- compute_locked() -------------------------------------------------------------------------


def test_compute_locked_is_false_with_no_schedule():
    assert compute_locked(DeviceSchedule("d", None, None, None), datetime(2026, 8, 30, tzinfo=timezone.utc)) is False


def test_compute_locked_is_true_when_forced_regardless_of_timing():
    start = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    schedule = DeviceSchedule("d", start, end, 95, forced=True)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)  # far from the window, still locked
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_true_when_imminent():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES - 1)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_false_when_far_off_and_not_forced():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES + 1)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    assert compute_locked(schedule, now) is False


def test_compute_locked_is_true_in_progress():
    start = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = start + timedelta(minutes=10)
    assert compute_locked(schedule, now) is True


def test_compute_locked_stays_true_once_elapsed_the_same_day():
    start = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = datetime(2026, 8, 30, 23, 0, tzinfo=timezone.utc)
    assert compute_locked(schedule, now) is True


def test_compute_locked_stays_true_after_an_overnight_slot_elapses_on_the_end_day():
    """A slot crossing midnight (e.g. 23:30 -> 01:00) must stay locked until the day it *ended*
    changes, not the day it started — using start.date() here would drop lock the instant it
    elapses, defeating "keep showing what ran today".
    """
    start = datetime(2026, 8, 29, 23, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
    schedule = DeviceSchedule("d", start, end, 95)
    now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)  # elapsed, still the day it ended
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_false_once_the_calendar_day_has_changed():
    start = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    assert compute_locked(schedule, now) is False


# --- _is_relevant_today() -----------------------------------------------------------------------


def test_is_relevant_today_is_false_with_nothing_committed():
    assert _is_relevant_today(None, datetime(2026, 8, 30, tzinfo=timezone.utc)) is False


def test_is_relevant_today_is_true_for_a_slot_started_today():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    committed = {"start": now, "end": now + timedelta(minutes=30)}
    assert _is_relevant_today(committed, now) is True


def test_is_relevant_today_is_true_for_an_overnight_slot_still_running():
    """Started yesterday, still in progress: must still block a sibling's search."""
    committed = {
        "start": datetime(2026, 8, 29, 23, 30, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc),
    }
    now = datetime(2026, 8, 30, 0, 30, tzinfo=timezone.utc)
    assert _is_relevant_today(committed, now) is True


def test_is_relevant_today_is_false_for_a_stale_multi_day_old_commitment():
    committed = {
        "start": datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc),
    }
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    assert _is_relevant_today(committed, now) is False


# --- coordinator state / store helpers --------------------------------------------------------


def _coordinator(hass) -> SolarPlannerSchedulerCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    return SolarPlannerSchedulerCoordinator(hass, entry)


# --- _tariff_bands() ----------------------------------------------------------------------------


_TARIFF_BANDS = [{"start": "00:00", "price": 0.20}, {"start": "22:00", "price": 0.15}]


def _coordinator_with_tariff_data(hass, extra_data: dict) -> SolarPlannerSchedulerCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000, **extra_data},
        options={},
    )
    entry.add_to_hass(hass)
    return SolarPlannerSchedulerCoordinator(hass, entry)


def test_tariff_bands_returns_empty_when_tracking_disabled(hass):
    """Same guard-rail logic as test_no_forecast_data_does_not_commit_a_guessed_now_slot: a
    disabled/absent state must return a neutral, empty result rather than exploit whatever bands
    happen to still be configured.
    """
    coordinator = _coordinator_with_tariff_data(
        hass, {CONF_PRICE_TRACKING_ENABLED: False, CONF_TARIFF_BANDS: _TARIFF_BANDS}
    )
    assert coordinator._tariff_bands() == []


def test_tariff_bands_returns_configured_bands_when_enabled(hass):
    coordinator = _coordinator_with_tariff_data(
        hass, {CONF_PRICE_TRACKING_ENABLED: True, CONF_TARIFF_BANDS: _TARIFF_BANDS}
    )
    assert coordinator._tariff_bands() == _TARIFF_BANDS


def _seed_committed(coordinator, device_name, program_name, schedule, forced=False):
    coordinator._state.setdefault(device_name, {})[program_name] = {
        **coordinator._state.get(device_name, {}).get(program_name, {}),
        "committed": {
            "start": schedule.start.isoformat(),
            "end": schedule.end.isoformat(),
            "coverage_pct": schedule.coverage_pct,
            "forced": forced,
        },
    }


async def test_set_program_active_does_not_touch_config_entry_options(hass):
    """The whole point of the rationalization: activating a program must never call
    hass.config_entries.async_update_entry (that's what used to reload the entire integration and
    flicker every device's entities)."""
    coordinator = _coordinator(hass)
    options_before = coordinator.entry.options

    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)

    assert coordinator.entry.options is options_before
    assert coordinator.is_program_active("lave_linge", "Eco", {}) is True


async def test_deactivating_a_program_clears_any_committed_slot(hass):
    coordinator = _coordinator(hass)
    now = dt_util.now()
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", now, now + timedelta(minutes=30), 95))

    await coordinator.async_set_program_active("lave_linge", "Eco", False)
    await _flush(coordinator)

    assert coordinator._get_committed("lave_linge", "Eco") is None


async def test_forget_program_drops_only_the_matching_programs_state(hass):
    coordinator = _coordinator(hass)
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await coordinator.async_set_program_active("lave_linge", "Intense", True)
    await _flush(coordinator)

    await coordinator.async_forget_program("lave_linge", "Intense")

    assert "Intense" not in coordinator._state["lave_linge"]
    assert coordinator.is_program_active("lave_linge", "Eco", {}) is True

    await coordinator.async_forget_program("lave_linge", "Eco")

    assert "Eco" not in coordinator._state["lave_linge"]


def test_is_program_active_defaults_to_true_when_auto_days_non_empty_and_never_toggled(hass):
    """A program declaring auto_days already means "run me on these days" — a Store reset (e.g.
    after a HA restart with nothing persisted yet) must not silently disable that until the user
    re-toggles it by hand."""
    coordinator = _coordinator(hass)
    program = {CONF_NAME: "Chauffe", CONF_AUTO_DAYS: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}

    assert coordinator.is_program_active("ballon", "Chauffe", program) is True


def test_is_program_active_defaults_to_false_with_empty_auto_days(hass):
    coordinator = _coordinator(hass)
    program = {CONF_NAME: "Eco", CONF_AUTO_DAYS: []}

    assert coordinator.is_program_active("lave_vaisselle", "Eco", program) is False


async def test_is_program_active_honors_an_explicit_false_over_the_auto_days_default(hass):
    """The user turning a program off on purpose (e.g. "no wash today") must stick, even though it
    declares auto_days — the default only applies when nothing was ever stored."""
    coordinator = _coordinator(hass)
    program = {CONF_NAME: "Chauffe", CONF_AUTO_DAYS: ["mon"]}
    await coordinator.async_set_program_active("ballon", "Chauffe", False)
    await _flush(coordinator)

    assert coordinator.is_program_active("ballon", "Chauffe", program) is False


async def test_set_forced_start_is_readable_before_a_refresh_folds_it_in(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc)

    await coordinator.async_set_forced_start("lave_linge", "Eco", start)
    await _flush(coordinator)

    assert coordinator._pending_forced_start("lave_linge", "Eco") == start


async def test_clear_forced_start_drops_both_pending_and_committed(hass):
    coordinator = _coordinator(hass)
    now = dt_util.now()
    _seed_committed(
        coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", now, now + timedelta(minutes=30), 95), forced=True
    )
    await coordinator.async_set_forced_start("lave_linge", "Eco", now + timedelta(hours=1))
    await _flush(coordinator)

    await coordinator.async_clear_forced_start("lave_linge", "Eco")
    await _flush(coordinator)

    assert coordinator._get_committed("lave_linge", "Eco") is None
    assert coordinator._pending_forced_start("lave_linge", "Eco") is None


# --- _reusable_committed() ---------------------------------------------------------------------


def test_reusable_committed_reuses_an_imminent_slot(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "forced": False, "cost": None, "seen_running": False}
    assert forced is False
    assert should_search is False
    assert dormant is False


def test_reusable_committed_searches_when_the_target_is_far_off(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES + 1)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert should_search is True
    assert dormant is False


def test_reusable_committed_searches_when_nothing_is_committed_yet(hass):
    """No committed entry (a program just activated, or never toggled before) always means a
    fresh search — there's nothing to compare against."""
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert should_search is True
    assert dormant is False


def test_reusable_committed_searches_when_the_program_duration_changed(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 60, now, [])

    assert should_search is True


def test_reusable_committed_keeps_showing_an_elapsed_slot_on_the_same_day(hass):
    """A program is scheduled once per activation: once its window has passed, don't propose
    another slot the same day, but keep displaying what already ran instead of blanking out."""
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=40)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "forced": False, "cost": None, "seen_running": False}
    assert should_search is False
    assert dormant is False


def test_reusable_committed_stays_in_progress_for_a_slot_crossing_midnight(hass):
    """A slot started yesterday (e.g. 23:30) and still running past midnight must not be treated
    as a day rollover mid-run — the in-progress check must win over the date comparison.
    """
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 29, 23, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    now = datetime(2026, 8, 30, 0, 30, tzinfo=timezone.utc)  # in progress, day already rolled over

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 90, now, [])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "forced": False, "cost": None, "seen_running": False}
    assert should_search is False
    assert dormant is False


def test_reusable_committed_continues_the_recurring_schedule_on_an_auto_day(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)  # Saturday
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)  # Sunday
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed(
        "lave_linge", "Eco", {}, 30, now, ["fri", "sun"]
    )

    assert should_search is True
    assert dormant is False


def test_reusable_committed_stays_dormant_on_a_non_auto_day(hass):
    """On-demand programs (empty auto_days) don't keep proposing a new slot every day on their
    own — they stay dormant until toggled again."""
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert dormant is True
    assert should_search is False


def test_reusable_committed_keeps_a_forced_slot_locked_even_far_in_the_future(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(hours=6)  # nowhere near imminent
    _seed_committed(
        coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 80), forced=True
    )

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert should_search is False
    assert forced is True


def test_reusable_committed_unlocks_when_a_power_sensor_shows_it_never_started(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "0")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power"}
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", device, 30, now, [])

    assert should_search is True
    assert failed_to_start is True


def test_reusable_committed_ignores_the_power_sensor_when_forced(hass):
    """An explicit forced start is authoritative — the failed-to-start safety net only applies to
    auto-computed slots."""
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95), forced=True)
    hass.states.async_set("sensor.lave_linge_power", "0")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power"}
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", device, 30, now, [])

    assert should_search is False
    assert failed_to_start is False
    assert forced is True


def test_reusable_committed_does_not_unlock_once_seen_running(hass):
    """Regression: a real appliance can finish its actual cycle faster than the configured
    power_profile. A later poll seeing low power must not undo an earlier confirmed run."""
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=20)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    coordinator._state["lave_linge"]["Eco"]["committed"]["seen_running"] = True
    hass.states.async_set("sensor.lave_linge_power", "0")  # idle now, but already confirmed running

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power"}
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", device, 150, now, [])

    assert should_search is False
    assert failed_to_start is False


async def test_update_seen_running_latches_once_power_exceeds_idle_threshold(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=5)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1600")

    await coordinator._update_seen_running("lave_linge", "Eco", {CONF_POWER_SENSOR: "sensor.lave_linge_power"}, now)

    assert coordinator._get_committed("lave_linge", "Eco")["seen_running"] is True


async def test_update_seen_running_does_nothing_below_idle_threshold(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=5)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "0")

    await coordinator._update_seen_running("lave_linge", "Eco", {CONF_POWER_SENSOR: "sensor.lave_linge_power"}, now)

    assert coordinator._get_committed("lave_linge", "Eco")["seen_running"] is False


async def test_a_short_real_cycle_does_not_get_flagged_as_failed_to_start_later(hass):
    """End-to-end reproduction of the live incident: power spikes shortly after start, then drops
    back to idle well before the configured window elapses. A later cycle must still reuse the slot.
    """
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 4, 11, 50, tzinfo=timezone.utc)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    device = {CONF_POWER_SENSOR: "sensor.pac_power"}

    hass.states.async_set("sensor.pac_power", "1069")  # the real appliance just ramped up
    await coordinator._update_seen_running("pac", "Eau chaude", device, start + timedelta(minutes=4))

    hass.states.async_set("sensor.pac_power", "5")  # it finished its real, shorter cycle
    later = start + timedelta(minutes=33)
    await coordinator._update_seen_running("pac", "Eau chaude", device, later)
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("pac", "Eau chaude", device, 150, later, [])

    assert should_search is False
    assert failed_to_start is False


# --- _note_failed_to_start() --------------------------------------------------------------------


def _has_issue(hass, coordinator) -> bool:
    issue_id = coordinator._failed_to_start_issue_id("lave_linge", "Eco")
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_note_failed_to_start_does_not_raise_an_issue_below_the_threshold(hass):
    """A single failed_to_start unlock is common (a device can take a few minutes to actually draw
    power) and shouldn't alarm anyone by itself."""
    coordinator = _coordinator(hass)

    await coordinator._note_failed_to_start("lave_linge", "Eco", True)

    assert coordinator._program_state("lave_linge", "Eco")["failed_start_streak"] == 1
    assert not _has_issue(hass, coordinator)


async def test_note_failed_to_start_raises_an_issue_past_the_threshold(hass):
    """Hit live 2026-09-02: the ballon d'eau chaude kept failing to start, silently, for hours —
    several *consecutive* failures should surface as a visible Repair, not just a log line."""
    coordinator = _coordinator(hass)

    for _ in range(FAILED_TO_START_REPAIR_THRESHOLD):
        await coordinator._note_failed_to_start("lave_linge", "Eco", True)

    assert _has_issue(hass, coordinator)


async def test_note_failed_to_start_clears_the_issue_once_a_cycle_succeeds(hass):
    coordinator = _coordinator(hass)

    for _ in range(FAILED_TO_START_REPAIR_THRESHOLD):
        await coordinator._note_failed_to_start("lave_linge", "Eco", True)
    assert _has_issue(hass, coordinator)

    await coordinator._note_failed_to_start("lave_linge", "Eco", False)

    assert not _has_issue(hass, coordinator)
    assert coordinator._program_state("lave_linge", "Eco")["failed_start_streak"] == 0


# --- _migrate_legacy_state() -------------------------------------------------------------------


def test_migrate_legacy_state_converts_a_selected_program_to_active():
    schedule_start = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    legacy = {
        "lave_linge": {
            "selected": "Eco",
            "pending_forced_start": "2026-08-30T10:00:00+00:00",
            "committed": {
                "start": schedule_start.isoformat(),
                "end": (schedule_start + timedelta(minutes=30)).isoformat(),
                "coverage_pct": 95,
                "program": "Eco",
                "forced": True,
            },
        }
    }

    migrated = _migrate_legacy_state(legacy)

    assert migrated == {
        "lave_linge": {
            "Eco": {
                "active": True,
                "pending_forced_start": "2026-08-30T10:00:00+00:00",
                "committed": {
                    "start": schedule_start.isoformat(),
                    "end": (schedule_start + timedelta(minutes=30)).isoformat(),
                    "coverage_pct": 95,
                    "forced": True,
                },
            }
        }
    }


def test_migrate_legacy_state_drops_a_none_selection():
    legacy = {"lave_vaisselle": {"selected": NONE_PROGRAM}}

    assert _migrate_legacy_state(legacy) == {"lave_vaisselle": {}}


def test_migrate_legacy_state_is_idempotent_on_the_current_schema():
    current = {"lave_linge": {"Eco": {"active": True}}}

    assert _migrate_legacy_state(current) == current


# --- _async_update_data() -----------------------------------------------------------------------


def _device_options(name="lave_vaisselle", auto_days=None, power_w=100, duration_min=30):
    return {
        CONF_DEVICES: [
            {
                CONF_NAME: name,
                CONF_PROGRAMS: [
                    {
                        CONF_NAME: "Eco",
                        CONF_POWER_PROFILE: [{"minutes": duration_min, "power_w": power_w}],
                        CONF_DURATION_MIN: duration_min,
                        CONF_AUTO_DAYS: auto_days or [],
                    }
                ],
            }
        ]
    }


async def test_no_forecast_data_does_not_commit_a_guessed_now_slot(hass):
    """async_config_entry_first_refresh() runs immediately on a HA restart, which can be before the
    forecast integration has populated its state, leaving `points` empty. Every candidate then ties
    at 0% coverage, so find_best_placement would silently keep the very first bucket ("now") —
    dangerous, since it used to get committed as if it were a real proposal.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)
    # sensor.forecast is deliberately never set: _read_forecast_points() returns [] for a missing state.

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is None
    assert coordinator._get_committed("lave_vaisselle", "Eco") is None


async def test_activating_a_program_searches_immediately_regardless_of_auto_days(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {"detailedForecast": [{"period_start": dt_util.now(), "pv_estimate": 3.0}]},
    )
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)
    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_a_pending_forced_start_is_applied_and_committed(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    forced_start = dt_util.now() + timedelta(hours=2)
    await coordinator.async_set_forced_start("lave_vaisselle", "Eco", forced_start)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start == forced_start
    assert results[("lave_vaisselle", "Eco")].forced is True
    committed = coordinator._get_committed("lave_vaisselle", "Eco")
    assert committed["forced"] is True
    assert coordinator._pending_forced_start("lave_vaisselle", "Eco") is None


async def test_two_active_programs_of_the_same_device_never_get_overlapping_slots(hass):
    """The scenario that motivated per-program activation: two programs of the same washing
    machine, both active the same day. Even though their combined power stays well under
    max_simultaneous_power (so the power-budget check alone would let them overlap), the device is
    a mutual-exclusion group — the second program must land on a distinct window.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_PROGRAMS: [
                        {
                            CONF_NAME: "Eco coton",
                            CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 100}],
                            CONF_DURATION_MIN: 30,
                            CONF_AUTO_DAYS: [],
                        },
                        {
                            CONF_NAME: "5 chemises",
                            CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 100}],
                            CONF_DURATION_MIN: 30,
                            CONF_AUTO_DAYS: [],
                        },
                    ],
                }
            ]
        },
    )
    entry.add_to_hass(hass)
    now = dt_util.now()
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {
            "detailedForecast": [
                {"period_start": now + timedelta(minutes=i * 5), "pv_estimate": 1.0} for i in range(24 * 12)
            ]
        },
    )
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco coton", True)
    await coordinator.async_set_program_active("lave_linge", "5 chemises", True)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    eco = results[("lave_linge", "Eco coton")]
    chemises = results[("lave_linge", "5 chemises")]
    assert eco.start is not None and chemises.start is not None
    assert eco.end <= chemises.start or chemises.end <= eco.start


async def test_activating_a_program_avoids_a_sibling_committed_in_an_earlier_cycle(hass):
    """Regression: hit live on 2026-09-01. Activating "5 chemises" alone first (its own
    `_async_update_data()` cycle, "Eco coton" still inactive) commits it to a slot. Activating
    "Eco coton" afterward, in a *separate* later cycle, must still avoid that already-committed
    slot — the accumulate-as-you-go `blocked` list used to start empty every cycle and only grow
    as each program was visited that same pass, so "Eco coton" (first in CONF_PROGRAMS order)
    never saw "5 chemises"'s pre-existing commitment and could land right on top of it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_PROGRAMS: [
                        {
                            CONF_NAME: "Eco coton",
                            CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 100}],
                            CONF_DURATION_MIN: 30,
                            CONF_AUTO_DAYS: [],
                        },
                        {
                            CONF_NAME: "5 chemises",
                            CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 100}],
                            CONF_DURATION_MIN: 30,
                            CONF_AUTO_DAYS: [],
                        },
                    ],
                }
            ]
        },
    )
    entry.add_to_hass(hass)
    now = dt_util.now()
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {
            "detailedForecast": [
                {"period_start": now + timedelta(minutes=i * 5), "pv_estimate": 1.0} for i in range(24 * 12)
            ]
        },
    )
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    # Cycle 1: only "5 chemises" active — commits it to a slot.
    await coordinator.async_set_program_active("lave_linge", "5 chemises", True)
    await _flush(coordinator)
    await coordinator._async_update_data()

    # Cycle 2: "Eco coton" activated afterward, in a separate refresh.
    await coordinator.async_set_program_active("lave_linge", "Eco coton", True)
    await _flush(coordinator)
    results = await coordinator._async_update_data()

    eco = results[("lave_linge", "Eco coton")]
    chemises = results[("lave_linge", "5 chemises")]
    assert eco.start is not None and chemises.start is not None
    assert eco.end <= chemises.start or chemises.end <= eco.start


# --- fixed load cost --------------------------------------------------------------------------


def _fixed_load(name="PAC", start_time="12:00:00", minutes=60, power_w=2000.0):
    return {CONF_NAME: name, CONF_START_TIME: start_time, CONF_POWER_PROFILE: [{CONF_MINUTES: minutes, CONF_POWER_W: power_w}]}


async def test_fixed_load_cost_is_computed_when_tariff_tracking_is_enabled(hass):
    """No forecast state is ever set, so solar coverage is 0 and the full 2 kWh draw is billed at
    the flat 0.20 EUR/kWh tariff band: exactly 0.40 EUR, easy to verify by hand.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_ENTITY: "sensor.forecast",
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
            CONF_PRICE_TRACKING_ENABLED: True,
            CONF_TARIFF_BANDS: [{"start": "00:00", "price": 0.20}],
        },
        options={CONF_DEVICES: [], CONF_FIXED_LOADS: [_fixed_load()]},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator._async_update_data()

    assert coordinator.fixed_load_cost("PAC") == pytest.approx(0.40)


async def test_fixed_load_cost_is_none_when_tariff_tracking_is_disabled(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={CONF_DEVICES: [], CONF_FIXED_LOADS: [_fixed_load()]},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator._async_update_data()

    assert coordinator.fixed_load_cost("PAC") is None


async def test_fixed_load_cost_accounts_for_other_concurrent_loads(hass):
    """A second fixed load overlapping the same window shares the (zero) solar coverage, so each
    load's own deficit, and therefore cost, is unaffected by the other's presence here (no solar
    to split): this only proves the "other" load doesn't get excluded from the walk by mistake,
    since a wrongly-excluded self would double count, not a shared-solar scenario.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_ENTITY: "sensor.forecast",
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
            CONF_PRICE_TRACKING_ENABLED: True,
            CONF_TARIFF_BANDS: [{"start": "00:00", "price": 0.20}],
        },
        options={
            CONF_DEVICES: [],
            CONF_FIXED_LOADS: [_fixed_load(name="PAC"), _fixed_load(name="Ballon", power_w=1000.0)],
        },
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator._async_update_data()

    assert coordinator.fixed_load_cost("PAC") == pytest.approx(0.40)
    assert coordinator.fixed_load_cost("Ballon") == pytest.approx(0.20)
