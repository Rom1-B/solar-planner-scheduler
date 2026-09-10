"""Tests for coordinator.py helpers.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt), both for
`coordinator.py`'s module-level `homeassistant` imports to resolve and, for the forecast-points
test below, to set mock entity states via the `hass` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR,
    CONF_FORECAST_CONFIG_ENTRY_HELIOS,
    CONF_FORECAST_CONFIG_ENTRY_SOLCAST,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_PHASE_CALIBRATION_RUNS,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DEFAULT_IDLE_POWER_THRESHOLD,
    DEFAULT_PHASE_CALIBRATION_RUNS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    FORECAST_PROVIDER_AVERAGE,
    FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_MIN,
    FORECAST_PROVIDER_SOLCAST,
    FORECAST_PROVIDER_WEIGHTED,
    NONE_PROGRAM,
    WEEKDAYS,
)
from custom_components.solar_planner_scheduler.coordinator import (
    FAILED_TO_START_REPAIR_THRESHOLD,
    NIGHT_EXTENSION_HOURS,
    PHASE_CALIBRATION_MINUTES_TOLERANCE,
    PHASE_CALIBRATION_WATTS_TOLERANCE_FLOOR_W,
    RUN_CALIBRATION_MIN_SAMPLES,
    STANDBY_MARGIN_W,
    STANDBY_MIN_SAMPLES,
    STANDBY_SAMPLE_COUNT,
    DeviceSchedule,
    SolarPlannerSchedulerCoordinator,
    _ceil_to_five_minutes,
    _day_buckets,
    _is_relevant_today,
    _migrate_legacy_state,
    _phases_differ_significantly,
    compute_locked,
    strip_legacy_device_options,
    strip_legacy_entry_data_keys,
)
from tests.conftest import register_provider_entities


async def _flush(coordinator) -> None:
    """Cancel the debounced async_request_refresh() every state-setter kicks off.

    These tests only care about the synchronous store write each setter makes before requesting a
    refresh, not the refresh itself — but a pending Debouncer call_later() timer left dangling
    fails the test harness's teardown check for lingering timers.
    """
    await coordinator.async_shutdown()


def test_ceil_to_five_minutes_rounds_up_to_the_next_mark():
    now = datetime(2026, 8, 30, 14, 23, 47, 123456, tzinfo=UTC)
    assert _ceil_to_five_minutes(now) == datetime(2026, 8, 30, 14, 25, tzinfo=UTC)


def test_ceil_to_five_minutes_leaves_an_exact_mark_untouched():
    now = datetime(2026, 8, 30, 14, 25, tzinfo=UTC)
    assert _ceil_to_five_minutes(now) == now


def test_todays_buckets_start_on_the_next_five_minute_mark_not_now():
    now = datetime(2026, 8, 30, 14, 23, 47, 123456, tzinfo=UTC)
    buckets = _day_buckets(now, day_offset=0)
    assert buckets[0]["start"] == datetime(2026, 8, 30, 14, 25, tzinfo=UTC)
    assert all(b["start"].minute % 5 == 0 and b["start"].second == 0 for b in buckets)


def test_todays_buckets_extend_past_midnight_by_night_extension_hours():
    now = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
    buckets = _day_buckets(now, day_offset=0)
    last_start = buckets[-1]["start"]
    assert last_start.date() == datetime(2026, 8, 31, tzinfo=UTC).date()
    assert last_start < datetime(2026, 8, 30, 23, 55, tzinfo=UTC) + timedelta(hours=NIGHT_EXTENSION_HOURS)


def test_theoretical_forecast_points_carries_percentiles(hass):
    """theoretical_forecast_points() must expose w10/w90 (not just w), ISO-serialized, for the
    card's confidence band, not just whatever _async_update_data() last computed for scheduling.
    """
    coordinator = _coordinator(hass)
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    coordinator._theoretical_points = [{"time": point_time, "w": 1000.0, "w10": 700.0, "w90": 1300.0}]

    assert coordinator.theoretical_forecast_points() == [
        {"time": point_time.isoformat(), "w": 1000.0, "w10": 700.0, "w90": 1300.0}
    ]


async def test_theoretical_points_keeps_the_last_known_curve_when_a_cycle_returns_nothing(hass):
    """Reported live: right after an HA restart, the chart's forecast line briefly went blank
    because the very first coordinator cycle ran before the forecast provider had published any
    data yet. A transient empty result must not blank a chart that already had a real curve.
    """
    coordinator = _coordinator(hass)
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    coordinator._theoretical_points = [{"time": point_time, "w": 1000.0, "w10": 700.0, "w90": 1300.0}]

    # No CONF_FORECAST_CONFIG_ENTRY_* field set: resolve_forecast_sources() resolves nothing, so
    # this cycle's own points come back empty, same as a provider not being ready yet.
    await coordinator._resolve_active_points({}, dt_util.now())

    assert coordinator._theoretical_points == [{"time": point_time, "w": 1000.0, "w10": 700.0, "w90": 1300.0}]


# --- compute_locked() -------------------------------------------------------------------------


def test_compute_locked_is_false_with_no_schedule():
    assert compute_locked(DeviceSchedule("d", None, None, None), datetime(2026, 8, 30, tzinfo=UTC)) is False


def test_compute_locked_is_true_when_forced_regardless_of_timing():
    start = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    schedule = DeviceSchedule("d", start, end, 95, forced=True)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)  # far from the window, still locked
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_true_when_imminent():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES - 1)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_false_when_far_off_and_not_forced():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES + 1)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    assert compute_locked(schedule, now) is False


def test_compute_locked_is_true_in_progress():
    start = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = start + timedelta(minutes=10)
    assert compute_locked(schedule, now) is True


def test_compute_locked_stays_true_once_elapsed_the_same_day():
    start = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = datetime(2026, 8, 30, 23, 0, tzinfo=UTC)
    assert compute_locked(schedule, now) is True


def test_compute_locked_stays_true_after_an_overnight_slot_elapses_on_the_end_day():
    """A slot crossing midnight (e.g. 23:30 -> 01:00) must stay locked until the day it *ended*
    changes, not the day it started — using start.date() here would drop lock the instant it
    elapses, defeating "keep showing what ran today".
    """
    start = datetime(2026, 8, 29, 23, 30, tzinfo=UTC)
    end = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    schedule = DeviceSchedule("d", start, end, 95)
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)  # elapsed, still the day it ended
    assert compute_locked(schedule, now) is True


def test_compute_locked_is_false_once_the_calendar_day_has_changed():
    start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
    schedule = DeviceSchedule("d", start, start + timedelta(minutes=30), 95)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    assert compute_locked(schedule, now) is False


# --- _is_relevant_today() -----------------------------------------------------------------------


def test_is_relevant_today_is_false_with_nothing_committed():
    assert _is_relevant_today(None, datetime(2026, 8, 30, tzinfo=UTC)) is False


def test_is_relevant_today_is_true_for_a_slot_started_today():
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    committed = {"start": now, "end": now + timedelta(minutes=30)}
    assert _is_relevant_today(committed, now) is True


def test_is_relevant_today_is_true_for_an_overnight_slot_still_running():
    """Started yesterday, still in progress: must still block a sibling's search."""
    committed = {
        "start": datetime(2026, 8, 29, 23, 30, tzinfo=UTC),
        "end": datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
    }
    now = datetime(2026, 8, 30, 0, 30, tzinfo=UTC)
    assert _is_relevant_today(committed, now) is True


def test_is_relevant_today_is_false_for_a_stale_multi_day_old_commitment():
    committed = {
        "start": datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
    }
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
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
    """The user turning a program off on purpose (e.g. "no wash today") must stick for the rest of
    that day, even though it declares auto_days — the default only applies when nothing was ever
    stored (or the stored False is stale, see the "ignores a stale false" test below)."""
    coordinator = _coordinator(hass)
    program = {CONF_NAME: "Chauffe", CONF_AUTO_DAYS: ["mon"]}
    await coordinator.async_set_program_active("ballon", "Chauffe", False)
    await _flush(coordinator)

    assert coordinator.is_program_active("ballon", "Chauffe", program) is False


async def test_is_program_active_ignores_a_stale_false_from_a_previous_day(hass):
    """The point of auto_days is to run every one of those days unattended — deactivating "just for
    today" must not silence every following auto_day too, only the day it was actually set on."""
    coordinator = _coordinator(hass)
    program = {CONF_NAME: "Chauffe", CONF_AUTO_DAYS: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    await coordinator.async_set_program_active("ballon", "Chauffe", False)
    await _flush(coordinator)
    coordinator._state["ballon"]["Chauffe"]["active_set_on"] = "2000-01-01"

    assert coordinator.is_program_active("ballon", "Chauffe", program) is True


async def test_is_program_active_stays_false_on_a_day_rollover_that_is_not_an_auto_day(hass):
    """Regression for a live incident (2026-09-09): Eco coton (auto_days=["fri"]) reactivated on
    an ordinary Tuesday-to-Wednesday rollover, because the stale-False reset only checked "does
    this program have any auto_days at all", not "is today actually one of them".
    """
    coordinator = _coordinator(hass)
    not_today = next(d for d in WEEKDAYS if d != WEEKDAYS[dt_util.now().weekday()])
    program = {CONF_NAME: "Eco coton", CONF_AUTO_DAYS: [not_today]}
    await coordinator.async_set_program_active("lave_linge", "Eco coton", False)
    await _flush(coordinator)
    coordinator._state["lave_linge"]["Eco coton"]["active_set_on"] = "2000-01-01"

    assert coordinator.is_program_active("lave_linge", "Eco coton", program) is False


async def test_is_program_active_reactivates_once_today_is_actually_the_auto_day(hass):
    coordinator = _coordinator(hass)
    today = WEEKDAYS[dt_util.now().weekday()]
    program = {CONF_NAME: "Eco coton", CONF_AUTO_DAYS: [today]}
    await coordinator.async_set_program_active("lave_linge", "Eco coton", False)
    await _flush(coordinator)
    coordinator._state["lave_linge"]["Eco coton"]["active_set_on"] = "2000-01-01"

    assert coordinator.is_program_active("lave_linge", "Eco coton", program) is True


async def test_set_forced_start_is_readable_before_a_refresh_folds_it_in(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert should_search is True
    assert dormant is False


def test_reusable_committed_searches_when_the_program_duration_changed(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
    start = now + timedelta(minutes=2)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 60, now, [])

    assert should_search is True


def test_reusable_committed_keeps_showing_an_elapsed_slot_on_the_same_day(hass):
    """A program is scheduled once per activation: once its window has passed, don't propose
    another slot the same day, but keep displaying what already ran instead of blanking out."""
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    start = datetime(2026, 8, 29, 23, 30, tzinfo=UTC)
    end = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    now = datetime(2026, 8, 30, 0, 30, tzinfo=UTC)  # in progress, day already rolled over

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 90, now, [])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "forced": False, "cost": None, "seen_running": False}
    assert should_search is False
    assert dormant is False


def test_reusable_committed_continues_the_recurring_schedule_on_an_auto_day(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 29, 13, 0, tzinfo=UTC)  # Saturday
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)  # Sunday
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed(
        "lave_linge", "Eco", {}, 30, now, ["fri", "sun"]
    )

    assert should_search is True
    assert dormant is False


def test_reusable_committed_stays_dormant_on_a_non_auto_day(hass):
    """On-demand programs (empty auto_days) don't keep proposing a new slot every day on their
    own — they stay dormant until toggled again."""
    coordinator = _coordinator(hass)
    start = datetime(2026, 8, 29, 13, 0, tzinfo=UTC)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert dormant is True
    assert should_search is False


def test_reusable_committed_keeps_a_forced_slot_locked_even_far_in_the_future(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
    start = now + timedelta(hours=6)  # nowhere near imminent
    _seed_committed(
        coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, start + timedelta(minutes=30), 80), forced=True
    )

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("lave_linge", "Eco", {}, 30, now, [])

    assert should_search is False
    assert forced is True


def test_reusable_committed_unlocks_when_a_power_sensor_shows_it_never_started(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
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
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
    start = now - timedelta(minutes=5)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1600")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1600}]}

    await coordinator._update_seen_running(
        "lave_linge",
        "Eco",
        {CONF_POWER_SENSOR: "sensor.lave_linge_power"},
        item,
        150,
        [],
        0.0,
        [],
        DEFAULT_IDLE_POWER_THRESHOLD,
        now,
    )

    committed = coordinator._get_committed("lave_linge", "Eco")
    assert committed["seen_running"] is True
    assert committed["start"] == now


async def test_update_seen_running_does_nothing_below_idle_threshold(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 8, 30, 9, 13, tzinfo=UTC)
    start = now - timedelta(minutes=5)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "0")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1600}]}

    await coordinator._update_seen_running(
        "lave_linge",
        "Eco",
        {CONF_POWER_SENSOR: "sensor.lave_linge_power"},
        item,
        150,
        [],
        0.0,
        [],
        DEFAULT_IDLE_POWER_THRESHOLD,
        now,
    )

    committed = coordinator._get_committed("lave_linge", "Eco")
    assert committed["seen_running"] is False
    assert committed["start"] == start


async def test_a_short_real_cycle_does_not_get_flagged_as_failed_to_start_later(hass):
    """End-to-end reproduction of the live incident: power spikes shortly after start, then drops
    back to idle well before the configured window elapses. A later cycle must still reuse the slot.
    """
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 4, 11, 50, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    device = {CONF_POWER_SENSOR: "sensor.pac_power"}
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}

    hass.states.async_set("sensor.pac_power", "1069")  # the real appliance just ramped up
    await coordinator._update_seen_running(
        "pac", "Eau chaude", device, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, start + timedelta(minutes=4)
    )

    hass.states.async_set("sensor.pac_power", "5")  # it finished its real, shorter cycle
    later = start + timedelta(minutes=33)
    await coordinator._update_seen_running(
        "pac", "Eau chaude", device, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, later
    )
    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed("pac", "Eau chaude", device, 150, later, [])

    assert should_search is False
    assert failed_to_start is False


async def test_early_power_detection_recalibrates_the_committed_start(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    hass.states.async_set("sensor.pac_power", "1069")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}
    now = start - timedelta(minutes=30)

    await coordinator._update_seen_running(
        "pac", "Eau chaude", {CONF_POWER_SENSOR: "sensor.pac_power"}, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, now
    )

    committed = coordinator._get_committed("pac", "Eau chaude")
    assert committed["start"] == now
    assert committed["end"] == now + timedelta(minutes=150)
    assert committed["seen_running"] is True
    assert committed["forced"] is True


async def test_late_power_detection_within_tolerance_also_recalibrates(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    hass.states.async_set("sensor.pac_power", "1069")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}
    now = start + timedelta(minutes=10)

    await coordinator._update_seen_running(
        "pac", "Eau chaude", {CONF_POWER_SENSOR: "sensor.pac_power"}, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, now
    )

    committed = coordinator._get_committed("pac", "Eau chaude")
    assert committed["start"] == now
    assert committed["end"] == now + timedelta(minutes=150)
    assert committed["seen_running"] is True
    assert committed["forced"] is True


async def test_power_detection_well_after_the_planned_start_still_recalibrates(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    hass.states.async_set("sensor.pac_power", "1069")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}
    now = start + timedelta(minutes=50)

    await coordinator._update_seen_running(
        "pac", "Eau chaude", {CONF_POWER_SENSOR: "sensor.pac_power"}, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, now
    )

    committed = coordinator._get_committed("pac", "Eau chaude")
    assert committed["start"] == now
    assert committed["end"] == now + timedelta(minutes=150)


async def test_power_detection_well_before_the_window_does_nothing(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    hass.states.async_set("sensor.pac_power", "1069")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}
    now = start - timedelta(minutes=50)

    await coordinator._update_seen_running(
        "pac", "Eau chaude", {CONF_POWER_SENSOR: "sensor.pac_power"}, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, now
    )

    committed = coordinator._get_committed("pac", "Eau chaude")
    assert committed["start"] == start
    assert committed["end"] == end
    assert committed["seen_running"] is False


async def test_power_detection_in_the_imminent_window_recalibrates_without_forcing(hass):
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54), forced=False)
    hass.states.async_set("sensor.pac_power", "1069")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1069}]}
    now = start - timedelta(minutes=3)

    await coordinator._update_seen_running(
        "pac", "Eau chaude", {CONF_POWER_SENSOR: "sensor.pac_power"}, item, 150, [], 0.0, [], DEFAULT_IDLE_POWER_THRESHOLD, now
    )

    committed = coordinator._get_committed("pac", "Eau chaude")
    assert committed["start"] == now
    assert committed["seen_running"] is True
    assert committed["forced"] is False


async def test_update_seen_running_uses_the_pending_power_detected_at_timestamp(hass):
    """A precise minute recorded by async_check_power_detection() takes priority over the live
    power check at this cycle's own `now`, for better than DEFAULT_UPDATE_INTERVAL_MINUTES
    precision."""
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    detected_at = start - timedelta(minutes=12)
    coordinator._state["lave_linge"]["Eco"]["pending_power_detected_at"] = detected_at.isoformat()
    hass.states.async_set("sensor.lave_linge_power", "0")  # live reading no longer matters
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1600}]}
    now = start + timedelta(minutes=2)  # cycle runs well after the recorded detection

    await coordinator._update_seen_running(
        "lave_linge",
        "Eco",
        {CONF_POWER_SENSOR: "sensor.lave_linge_power"},
        item,
        150,
        [],
        0.0,
        [],
        DEFAULT_IDLE_POWER_THRESHOLD,
        now,
    )

    committed = coordinator._get_committed("lave_linge", "Eco")
    assert committed["start"] == detected_at
    assert committed["seen_running"] is True
    assert "pending_power_detected_at" not in coordinator._state["lave_linge"]["Eco"]


async def test_update_seen_running_ignores_a_stale_pending_power_detected_at(hass):
    """A pending_power_detected_at recorded outside the current window (e.g. against a
    since-replaced commitment) must not be blindly trusted — falls back to a live check."""
    coordinator = _coordinator(hass)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    coordinator._state["lave_linge"]["Eco"]["pending_power_detected_at"] = (start - timedelta(hours=5)).isoformat()
    hass.states.async_set("sensor.lave_linge_power", "1600")
    item = {"profile": [{CONF_MINUTES: 150, CONF_POWER_W: 1600}]}
    now = start + timedelta(minutes=2)

    await coordinator._update_seen_running(
        "lave_linge",
        "Eco",
        {CONF_POWER_SENSOR: "sensor.lave_linge_power"},
        item,
        150,
        [],
        0.0,
        [],
        DEFAULT_IDLE_POWER_THRESHOLD,
        now,
    )

    committed = coordinator._get_committed("lave_linge", "Eco")
    assert committed["start"] == now


# --- _idle_threshold_for() -----------------------------------------------------------------------


def test_idle_threshold_for_stays_at_the_floor_for_a_low_first_phase(hass):
    coordinator = _coordinator(hass)
    profile = [{CONF_MINUTES: 20, CONF_POWER_W: 16}]

    assert coordinator._idle_threshold_for(profile, "lave_linge") == DEFAULT_IDLE_POWER_THRESHOLD


def test_idle_threshold_for_scales_with_a_high_first_phase(hass):
    coordinator = _coordinator(hass)
    profile = [{CONF_MINUTES: 30, CONF_POWER_W: 1600}]

    assert coordinator._idle_threshold_for(profile, "ballon") == 800.0


def test_idle_threshold_for_ignores_a_learned_standby_below_the_min_sample_count(hass):
    coordinator = _coordinator(hass)
    coordinator._state["standby"] = {"lave_linge": [500.0] * (STANDBY_MIN_SAMPLES - 1)}

    assert coordinator._idle_threshold_for([], "lave_linge") == DEFAULT_IDLE_POWER_THRESHOLD


def test_idle_threshold_for_uses_the_learned_median_once_enough_samples_exist(hass):
    coordinator = _coordinator(hass)
    coordinator._state["standby"] = {"lave_linge": [20.0] * STANDBY_MIN_SAMPLES}

    assert coordinator._idle_threshold_for([], "lave_linge") == 20.0 + STANDBY_MARGIN_W


def test_idle_threshold_for_prefers_the_larger_of_learned_standby_and_profile_derived(hass):
    coordinator = _coordinator(hass)
    profile = [{CONF_MINUTES: 30, CONF_POWER_W: 1600}]
    coordinator._state["standby"] = {"ballon": [3.0] * STANDBY_MIN_SAMPLES}

    # profile-derived (800.0) still wins over a low learned standby (3.0 + margin)
    assert coordinator._idle_threshold_for(profile, "ballon") == 800.0


# --- _record_standby_sample() / _learned_standby() ----------------------------------------------


async def test_record_standby_sample_appends_and_caps_at_standby_sample_count(hass):
    coordinator = _coordinator(hass)
    coordinator._state["standby"] = {"lave_linge": [111.0] + [999.0] * (STANDBY_SAMPLE_COUNT - 1)}

    await coordinator._record_standby_sample("lave_linge", 4.0)

    samples = coordinator._standby_samples("lave_linge")
    assert len(samples) == STANDBY_SAMPLE_COUNT
    assert samples[-1] == 4.0
    assert 111.0 not in samples  # the oldest reading was pushed out, not just appended past the cap


async def test_learned_standby_is_none_below_the_min_sample_count(hass):
    coordinator = _coordinator(hass)

    for _ in range(STANDBY_MIN_SAMPLES - 1):
        await coordinator._record_standby_sample("lave_linge", 4.0)

    assert coordinator._learned_standby("lave_linge") is None


async def test_learned_standby_is_the_median_of_recorded_readings(hass):
    coordinator = _coordinator(hass)

    for power in (2.0, 3.0, 4.0):
        await coordinator._record_standby_sample("lave_linge", power)

    assert coordinator._learned_standby("lave_linge") == 3.0


# --- standby sample recording inside _async_update_data() ----------------------------------------


def _device_with_power_sensor(power_w=200, duration_min=60):
    return {
        CONF_DEVICES: [
            {
                CONF_NAME: "lave_linge",
                CONF_POWER_SENSOR: "sensor.lave_linge_power",
                CONF_PROGRAMS: [
                    {
                        CONF_NAME: "Eco",
                        CONF_POWER_PROFILE: [{CONF_MINUTES: duration_min, CONF_POWER_W: power_w}],
                        CONF_DURATION_MIN: duration_min,
                        CONF_AUTO_DAYS: [],
                    }
                ],
            }
        ]
    }


async def test_a_fresh_search_records_a_standby_sample_when_the_device_looks_idle(hass):
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast": {"detailedForecast": [{"period_start": dt_util.now(), "pv_estimate": 3.0}]}}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_with_power_sensor(),
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.lave_linge_power", "5")
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator._standby_samples("lave_linge") == [5.0]


async def test_a_fresh_search_does_not_record_standby_when_the_device_already_looks_running(hass):
    """The failed_to_start unlock is the one should_search case where the device might actually
    already be running (the whole point of that branch is "we're not sure it ever started") — a
    live reading at or above the idle threshold must never be trusted as a standby sample.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_with_power_sensor(power_w=200),
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.forecast", "3", {"detailedForecast": [{"period_start": dt_util.now(), "pv_estimate": 3.0}]}
    )
    hass.states.async_set("sensor.lave_linge_power", "200")  # at/above this program's idle_threshold
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator._standby_samples("lave_linge") == []


def test_reusable_committed_uses_the_passed_idle_threshold(hass):
    coordinator = _coordinator(hass)
    now = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    start = now - timedelta(minutes=10)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "pac", "Eau chaude", DeviceSchedule("pac", start, end, 54))
    hass.states.async_set("sensor.pac_power", "500")
    device = {CONF_POWER_SENSOR: "sensor.pac_power"}

    slot, forced, should_search, dormant, failed_to_start = coordinator._reusable_committed(
        "pac", "Eau chaude", device, 150, now, [], idle_threshold=800
    )

    assert failed_to_start is True


# --- async_check_power_detection() ---------------------------------------------------------------


def _device_options_with_sensor(
    name="lave_linge", power_sensor="sensor.lave_linge_power", power_w=1600, duration_min=150, auto_days=None
):
    return {
        CONF_DEVICES: [
            {
                CONF_NAME: name,
                CONF_POWER_SENSOR: power_sensor,
                CONF_PROGRAMS: [
                    {
                        CONF_NAME: "Eco",
                        CONF_POWER_PROFILE: [{CONF_MINUTES: duration_min, CONF_POWER_W: power_w}],
                        CONF_DURATION_MIN: duration_min,
                        CONF_AUTO_DAYS: auto_days or [],
                    }
                ],
            }
        ]
    }


def _active_coordinator_with_sensor(hass, **kwargs) -> SolarPlannerSchedulerCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options_with_sensor(**kwargs),
    )
    entry.add_to_hass(hass)
    return SolarPlannerSchedulerCoordinator(hass, entry)


async def test_check_power_detection_records_the_precise_minute(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1600")
    now = start - timedelta(minutes=12)

    await coordinator.async_check_power_detection(now)

    assert coordinator._state["lave_linge"]["Eco"]["pending_power_detected_at"] == now.isoformat()


async def test_check_power_detection_skips_when_already_seen_running(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    coordinator._state["lave_linge"]["Eco"]["committed"]["seen_running"] = True
    hass.states.async_set("sensor.lave_linge_power", "1600")

    await coordinator.async_check_power_detection(start)

    assert "pending_power_detected_at" not in coordinator._state["lave_linge"]["Eco"]


async def test_check_power_detection_does_not_overwrite_an_existing_pending_timestamp(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    first_detection = start - timedelta(minutes=10)
    coordinator._state["lave_linge"]["Eco"]["pending_power_detected_at"] = first_detection.isoformat()
    hass.states.async_set("sensor.lave_linge_power", "1600")

    await coordinator.async_check_power_detection(start - timedelta(minutes=5))

    assert coordinator._state["lave_linge"]["Eco"]["pending_power_detected_at"] == first_detection.isoformat()


async def test_check_power_detection_skips_when_program_inactive(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    # Deliberately not activated: switch.lave_linge_eco_active stays off.
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1600")

    await coordinator.async_check_power_detection(start)

    assert "pending_power_detected_at" not in coordinator._state["lave_linge"]["Eco"]


async def test_check_power_detection_skips_outside_the_tolerance_window(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1600")

    await coordinator.async_check_power_detection(start - timedelta(minutes=50))

    assert "pending_power_detected_at" not in coordinator._state["lave_linge"]["Eco"]


async def test_check_power_detection_skips_below_the_derived_threshold(hass):
    coordinator = _active_coordinator_with_sensor(hass, power_w=1600)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    await _flush(coordinator)
    start = datetime(2026, 9, 5, 12, 40, tzinfo=UTC)
    end = start + timedelta(minutes=150)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "500")  # below max(10, 1600 * 0.5) = 800

    await coordinator.async_check_power_detection(start)

    assert "pending_power_detected_at" not in coordinator._state["lave_linge"]["Eco"]


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
    schedule_start = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
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


# --- strip_legacy_device_options() ---------------------------------------------------------------


def test_strip_legacy_device_options_drops_pre_2026_08_31_keys():
    devices = [
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

    cleaned, changed = strip_legacy_device_options(devices)

    assert changed is True
    assert cleaned == [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_PROGRAMS: []}]


def test_strip_legacy_device_options_is_a_no_op_on_already_clean_devices():
    devices = [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_PROGRAMS: []}]

    cleaned, changed = strip_legacy_device_options(devices)

    assert changed is False
    assert cleaned == devices


# --- strip_legacy_entry_data_keys() ---------------------------------------------------------------


def test_strip_legacy_entry_data_keys_drops_production_and_consumption_entity():
    data = {
        CONF_MAX_SIMULTANEOUS_POWER: 4000,
        "production_entity": "sensor.elec_solar_power",
        "consumption_entity": "sensor.elec_0_power",
        "subscription_price_monthly": 12.5,
    }

    cleaned, changed = strip_legacy_entry_data_keys(data)

    assert changed is True
    assert cleaned == {CONF_MAX_SIMULTANEOUS_POWER: 4000}


def test_strip_legacy_entry_data_keys_is_a_no_op_on_already_clean_data():
    data = {CONF_MAX_SIMULTANEOUS_POWER: 4000}

    cleaned, changed = strip_legacy_entry_data_keys(data)

    assert changed is False
    assert cleaned == data


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


async def test_no_forecast_data_still_displays_an_already_committed_not_yet_imminent_slot(hass):
    """Reported live: right after an HA restart, a program that was already scheduled (its
    committed slot still hours away, so _reusable_committed() wants to re-search and chase a
    better window every cycle) briefly looked unscheduled on the card whenever the forecast
    integration hadn't come back up yet. should_search=True with empty points must keep showing
    the existing Store commitment for display, not blank it — the Store itself stays untouched
    either way, so the next real search (once forecast data loads) is unaffected.
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
    future_start = dt_util.now() + timedelta(hours=2)
    _seed_committed(
        coordinator,
        "lave_vaisselle",
        "Eco",
        DeviceSchedule("lave_vaisselle", future_start, future_start + timedelta(minutes=30), 80),
    )
    await _flush(coordinator)
    # sensor.forecast is deliberately never set: _read_forecast_points() returns [] for a missing state.

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start == future_start
    committed = coordinator._get_committed("lave_vaisselle", "Eco")
    assert committed["start"] == future_start


async def test_no_forecast_data_does_not_shift_an_already_forced_slot(hass):
    """A manually forced (dragged) slot must never be affected by the empty-forecast fallback
    above: _reusable_committed() already keeps should_search False for a forced slot regardless of
    timing, so this end-to-end path never even reaches the branch that fallback lives in.
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
    forced_start = dt_util.now() + timedelta(hours=6)
    _seed_committed(
        coordinator,
        "lave_vaisselle",
        "Eco",
        DeviceSchedule("lave_vaisselle", forced_start, forced_start + timedelta(minutes=30), 80),
        forced=True,
    )
    await _flush(coordinator)
    # sensor.forecast is deliberately never set: _read_forecast_points() returns [] for a missing state.

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start == forced_start
    assert results[("lave_vaisselle", "Eco")].forced is True
    committed = coordinator._get_committed("lave_vaisselle", "Eco")
    assert committed["start"] == forced_start


async def test_activating_a_program_searches_immediately_regardless_of_auto_days(hass):
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast": {"detailedForecast": [{"period_start": dt_util.now(), "pv_estimate": 3.0}]}}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)
    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_a_dormant_on_demand_program_deactivates_itself(hass):
    """Regression for a live incident (2026-09-09): lave-vaisselle's Eco program (empty auto_days)
    stayed "active" indefinitely after its one-off run elapsed into a new day, since nothing ever
    turned the switch back off once dormant, leaving it looking "armed" until turned off by hand.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    yesterday_start = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _seed_committed(
        coordinator,
        "lave_vaisselle",
        "Eco",
        DeviceSchedule("lave_vaisselle", yesterday_start, yesterday_start + timedelta(minutes=30), 80),
    )
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is None
    assert coordinator.is_program_active("lave_vaisselle", "Eco", {CONF_AUTO_DAYS: []}) is False


async def test_a_dormant_auto_days_program_stays_active_for_its_next_occurrence(hass):
    """The opposite case: a recurring program going dormant on a non-auto-day (not just any empty
    auto_days) must stay active, so it can reactivate itself on its own on its next real auto_day.
    """
    not_today = next(d for d in WEEKDAYS if d != WEEKDAYS[dt_util.now().weekday()])
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[not_today]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    yesterday_start = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _seed_committed(
        coordinator,
        "lave_vaisselle",
        "Eco",
        DeviceSchedule("lave_vaisselle", yesterday_start, yesterday_start + timedelta(minutes=30), 80),
    )
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is None  # dormant: today isn't not_today's auto_day
    assert coordinator.is_program_active("lave_vaisselle", "Eco", {CONF_AUTO_DAYS: [not_today]}) is True


def _helios_entry(hass, entity_id: str = "sensor.helios_power_now", watts: float = 3000.0, at=None) -> str:
    at = at or dt_util.now()
    return register_provider_entities(
        hass, "helios_forecast", {entity_id: {"forecast": [{"datetime": at.isoformat(), "watts": watts}]}}
    )


async def test_active_forecast_source_defaults_to_the_first_resolved_provider_when_never_chosen(hass):
    """Store empty (async_set_forecast_source() never called): _async_update_data() must use the
    only/first provider resolve_forecast_sources() finds, same as before this feature existed.
    """
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert coordinator.active_forecast_source() is None
    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_async_set_forecast_source_switches_without_touching_entry_data(hass):
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    data_before = dict(entry.data)

    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_HELIOS)
    await _flush(coordinator)

    assert coordinator.active_forecast_source() == FORECAST_PROVIDER_HELIOS
    assert dict(entry.data) == data_before


async def test_async_update_data_falls_back_when_the_stored_source_is_no_longer_resolved(hass):
    """A choice stored for a provider whose field has since been emptied falls back to whatever
    remains, rather than silently reading no forecast at all.
    """
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_SOLCAST)  # not configured at all
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_async_update_data_averages_every_resolved_provider_when_average_selected(hass):
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast_today": {"detailedForecast": [{"period_start": now, "pv_estimate": 1.0}]}}
    )
    helios_entry_id = _helios_entry(hass, watts=3000.0, at=now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id,
            CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id,
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_AVERAGE)
    await _flush(coordinator)

    await coordinator._async_update_data()

    # solcast: pv_estimate 1.0 kW -> 1000 W ; helios: 3000 W already -> mean 2000 W.
    assert coordinator._theoretical_points == [{"time": now, "w": 2000.0, "w10": 2000.0, "w90": 2000.0}]


async def test_async_update_data_takes_the_minimum_across_every_resolved_provider_when_min_selected(hass):
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast_today": {"detailedForecast": [{"period_start": now, "pv_estimate": 1.0}]}}
    )
    helios_entry_id = _helios_entry(hass, watts=3000.0, at=now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id,
            CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id,
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_MIN)
    await _flush(coordinator)

    await coordinator._async_update_data()

    # solcast: pv_estimate 1.0 kW -> 1000 W ; helios: 3000 W already -> min 1000 W.
    assert coordinator._theoretical_points == [{"time": now, "w": 1000.0, "w10": 1000.0, "w90": 1000.0}]


async def test_async_update_data_falls_back_from_average_when_only_one_provider_resolves(hass):
    """"Average" stored as the active choice but only one provider is still configured: falls back
    to that one provider, same repli mechanism as any other stale choice, no exception.
    """
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_AVERAGE)
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_average_and_min_power_now_are_computed_regardless_of_the_active_selection(hass):
    """average_forecast_power_now()/min_forecast_power_now() back always-on comparison sensors, so
    they must reflect every *configured* provider even when a single real one (not "Average"/"Min")
    is the active select choice.
    """
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast_today": {"detailedForecast": [{"period_start": now, "pv_estimate": 1.0}]}}
    )
    helios_entry_id = _helios_entry(hass, watts=3000.0, at=now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id,
            CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id,
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_SOLCAST)
    await _flush(coordinator)

    await coordinator._async_update_data()

    # solcast: pv_estimate 1.0 kW -> 1000 W ; helios: 3000 W already -> mean 2000 W, min 1000 W.
    assert coordinator.average_forecast_power_now() == 2000.0
    assert coordinator.min_forecast_power_now() == 1000.0


async def test_average_and_min_power_now_are_none_with_only_one_provider_configured(hass):
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator.average_forecast_power_now() is None
    assert coordinator.min_forecast_power_now() is None


async def test_async_update_data_weights_helios_and_solcast_by_helios_reliability_when_selected(hass):
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast_today": {"detailedForecast": [{"period_start": now, "pv_estimate": 1.0}]}}
    )
    helios_entry_id = _helios_entry(hass, watts=3000.0, at=now)
    helios_entry = hass.config_entries.async_get_entry(helios_entry_id)
    register_provider_entities(hass, "helios_forecast", {"sensor.helios_reliability": {"per_day": [9.2]}}, entry=helios_entry)
    hass.states.async_set("sensor.helios_reliability", "25", {"per_day": [9.2]})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id,
            CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id,
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_WEIGHTED)
    await _flush(coordinator)

    await coordinator._async_update_data()

    # solcast: pv_estimate 1.0 kW -> 1000 W ; helios: 3000 W ; weight 25% helios -> 0.25*3000 + 0.75*1000 = 1500 W.
    assert coordinator._theoretical_points == [{"time": now, "w": 1500.0, "w10": 1500.0, "w90": 1500.0}]
    assert coordinator.weighted_forecast_power_now() == 1500.0


async def test_weighted_power_now_equals_helios_exactly_at_full_reliability(hass):
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass, "solcast_solar", {"sensor.forecast_today": {"detailedForecast": [{"period_start": now, "pv_estimate": 1.0}]}}
    )
    helios_entry_id = _helios_entry(hass, watts=3000.0, at=now)
    helios_entry = hass.config_entries.async_get_entry(helios_entry_id)
    register_provider_entities(hass, "helios_forecast", {"sensor.helios_reliability": {"per_day": [9.2]}}, entry=helios_entry)
    hass.states.async_set("sensor.helios_reliability", "100", {"per_day": [9.2]})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id,
            CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id,
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator.weighted_forecast_power_now() == 3000.0


async def test_weighted_power_now_is_none_when_helios_or_solcast_is_missing(hass):
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator.weighted_forecast_power_now() is None


async def test_weighted_not_offered_as_active_source_with_only_one_provider(hass):
    """"weighted" stored as the active choice but Solcast+Helios aren't both configured: falls back
    to the resolved provider, same repli mechanism as a stale "average"/"min" choice.
    """
    helios_entry_id = _helios_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(auto_days=[]),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_set_forecast_source(FORECAST_PROVIDER_WEIGHTED)
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_vaisselle", "Eco")].start is not None


async def test_async_update_data_merges_points_from_every_discovered_solcast_entity(hass):
    """Solcast's own entity list (today + tomorrow, say) must have every entity's points merged
    and sorted, not just the first one discovered.
    """
    today = dt_util.now()
    tomorrow = today + timedelta(days=1)
    solcast_entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.forecast_today": {"detailedForecast": [{"period_start": today, "pv_estimate": 1.0}]},
            "sensor.forecast_tomorrow": {"detailedForecast": [{"period_start": tomorrow, "pv_estimate": 2.0}]},
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator._async_update_data()

    times = [pt["time"] for pt in coordinator._theoretical_points]
    assert times == sorted(times)
    assert today in times and tomorrow in times


async def test_async_update_data_reads_forecast_solar_via_the_energy_platform_hook(hass, monkeypatch):
    """forecast_solar as the active provider: _async_update_data() must go through the
    config_entry_id dispatch in _read_provider_points(), not treat it as an entity_id.
    """
    point_time = dt_util.now()

    class _FakePlatform:
        @staticmethod
        async def async_get_solar_forecast(hass, config_entry_id):
            return {"wh_hours": {point_time.isoformat(): 2500}}

    class _FakeIntegration:
        async def async_get_platform(self, name):
            return _FakePlatform()

    async def _fake_async_get_integration(hass, domain):
        return _FakeIntegration()

    monkeypatch.setattr(
        "custom_components.solar_planner_scheduler.forecast_providers.async_get_integration", _fake_async_get_integration
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR: "entry123", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()

    await coordinator._async_update_data()

    assert coordinator._theoretical_points == [{"time": point_time, "w": 2500.0, "w10": 2500.0, "w90": 2500.0}]


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
    assert committed["seen_running"] is False


async def test_a_pending_forced_start_marks_seen_running_when_already_drawing_power(hass):
    """Live incident: forcing a start while the device is already running (e.g. re-forcing 10:58
    after the machine already started) must not reset seen_running to False — otherwise the very
    next cycle's still-live power reading looks like a fresh detection and recalibrates straight
    past the forced time, chasing "now" every time the user re-forces it.
    """
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    hass.states.async_set("sensor.lave_linge_power", "1600")
    forced_start = dt_util.now() - timedelta(minutes=12)
    await coordinator.async_set_forced_start("lave_linge", "Eco", forced_start)
    await _flush(coordinator)

    results = await coordinator._async_update_data()

    assert results[("lave_linge", "Eco")].start == forced_start
    committed = coordinator._get_committed("lave_linge", "Eco")
    assert committed["seen_running"] is True


async def test_a_pending_forced_start_records_a_standby_sample_when_the_device_looks_idle(hass):
    """An on-demand program driven only by forced starts (never an accepted auto-search proposal)
    would otherwise never learn its standby power at all.
    """
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    hass.states.async_set("sensor.lave_linge_power", "5")
    await coordinator.async_set_forced_start("lave_linge", "Eco", dt_util.now() + timedelta(hours=2))
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator._standby_samples("lave_linge") == [5.0]


async def test_a_pending_forced_start_does_not_record_standby_when_the_device_already_looks_running(hass):
    coordinator = _active_coordinator_with_sensor(hass)
    await coordinator.async_load_state()
    await coordinator.async_set_program_active("lave_linge", "Eco", True)
    hass.states.async_set("sensor.lave_linge_power", "1600")
    await coordinator.async_set_forced_start("lave_linge", "Eco", dt_util.now() - timedelta(minutes=12))
    await _flush(coordinator)

    await coordinator._async_update_data()

    assert coordinator._standby_samples("lave_linge") == []


async def test_two_active_programs_of_the_same_device_never_get_overlapping_slots(hass):
    """The scenario that motivated per-program activation: two programs of the same washing
    machine, both active the same day. Even though their combined power stays well under
    max_simultaneous_power (so the power-budget check alone would let them overlap), the device is
    a mutual-exclusion group — the second program must land on a distinct window.
    """
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.forecast": {
                "detailedForecast": [
                    {"period_start": now + timedelta(minutes=i * 5), "pv_estimate": 1.0} for i in range(24 * 12)
                ]
            }
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
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
    now = dt_util.now()
    solcast_entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.forecast": {
                "detailedForecast": [
                    {"period_start": now + timedelta(minutes=i * 5), "pv_estimate": 1.0} for i in range(24 * 12)
                ]
            }
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_MAX_SIMULTANEOUS_POWER: 4000},
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


# --- _phases_differ_significantly() -------------------------------------------------------------


def test_phases_differ_significantly_true_on_length_mismatch():
    old = [{"minutes": 30, "power_w": 1600}]
    new = [{"minutes": 30, "power_w": 1600}, {"minutes": 10, "power_w": 0}]
    assert _phases_differ_significantly(old, new)


def test_phases_differ_significantly_true_when_minutes_exceed_tolerance():
    old = [{"minutes": 30, "power_w": 1600}]
    new = [{"minutes": 30 + PHASE_CALIBRATION_MINUTES_TOLERANCE + 1, "power_w": 1600}]
    assert _phases_differ_significantly(old, new)


def test_phases_differ_significantly_true_when_watts_exceed_the_relative_tolerance():
    old = [{"minutes": 30, "power_w": 1600}]
    new = [{"minutes": 30, "power_w": 1600 + 200}]  # > 10% of 1600 (160)
    assert _phases_differ_significantly(old, new)


def test_phases_differ_significantly_uses_the_watts_floor_for_low_power_phases():
    old = [{"minutes": 30, "power_w": 50}]  # 10% of 50W (5) is below the floor
    within = [{"minutes": 30, "power_w": 50 + PHASE_CALIBRATION_WATTS_TOLERANCE_FLOOR_W}]
    beyond = [{"minutes": 30, "power_w": 50 + PHASE_CALIBRATION_WATTS_TOLERANCE_FLOOR_W + 1}]
    assert not _phases_differ_significantly(old, within)
    assert _phases_differ_significantly(old, beyond)


def test_phases_differ_significantly_false_within_tolerance():
    old = [{"minutes": 30, "power_w": 1600}]
    new = [{"minutes": 30 + PHASE_CALIBRATION_MINUTES_TOLERANCE, "power_w": 1600 + 50}]  # 50W < 10% of 1600
    assert not _phases_differ_significantly(old, new)


# --- _apply_phase_calibration() ------------------------------------------------------------------


def test_apply_phase_calibration_rewrites_only_the_matching_program(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_PROGRAMS: [
                        {
                            CONF_NAME: "Eco",
                            CONF_POWER_PROFILE: [{CONF_MINUTES: 30, CONF_POWER_W: 1600}],
                            CONF_DURATION_MIN: 30,
                            CONF_AUTO_DAYS: [],
                        },
                        {
                            CONF_NAME: "Intense",
                            CONF_POWER_PROFILE: [{CONF_MINUTES: 60, CONF_POWER_W: 2000}],
                            CONF_DURATION_MIN: 60,
                            CONF_AUTO_DAYS: [],
                        },
                    ],
                }
            ]
        },
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)

    coordinator._apply_phase_calibration("lave_linge", "Eco", [{"minutes": 18, "power_w": 1650}])

    programs = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS]
    assert programs[0][CONF_POWER_PROFILE] == [{"minutes": 18, "power_w": 1650}]
    assert programs[0][CONF_DURATION_MIN] == 18
    assert programs[1][CONF_POWER_PROFILE] == [{CONF_MINUTES: 60, CONF_POWER_W: 2000}]  # untouched


# --- async_track_run_progress() / _finalize_run_calibration() -----------------------------------


def _device_with_profile(profile, power_sensor="sensor.lave_linge_power", calibration_runs=None):
    program = {
        CONF_NAME: "Eco",
        CONF_POWER_PROFILE: profile,
        CONF_DURATION_MIN: sum(p[CONF_MINUTES] for p in profile),
        CONF_AUTO_DAYS: [],
    }
    if calibration_runs is not None:
        program[CONF_PHASE_CALIBRATION_RUNS] = calibration_runs
    return {
        CONF_DEVICES: [
            {
                CONF_NAME: "lave_linge",
                CONF_POWER_SENSOR: power_sensor,
                CONF_PROGRAMS: [program],
            }
        ]
    }


def _profile_coordinator(hass, profile, power_sensor="sensor.lave_linge_power", calibration_runs=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_with_profile(profile, power_sensor, calibration_runs),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    coordinator._state.setdefault("lave_linge", {})["Eco"] = {"active": True}
    return coordinator


async def test_async_track_run_progress_records_a_sample_while_in_progress(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    now = datetime(2026, 9, 8, 12, 10, tzinfo=UTC)
    schedule = DeviceSchedule("lave_linge", now - timedelta(minutes=5), now + timedelta(minutes=25), 80)
    _seed_committed(coordinator, "lave_linge", "Eco", schedule)
    hass.states.async_set("sensor.lave_linge_power", "1590")

    await coordinator.async_track_run_progress(now)

    assert coordinator._program_state("lave_linge", "Eco")["run_trace"] == [{"t": now.isoformat(), "w": 1590.0}]


async def test_async_track_run_progress_does_nothing_when_calibration_runs_is_zero(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}], calibration_runs=0)
    now = datetime(2026, 9, 8, 12, 10, tzinfo=UTC)
    schedule = DeviceSchedule("lave_linge", now - timedelta(minutes=5), now + timedelta(minutes=25), 80)
    _seed_committed(coordinator, "lave_linge", "Eco", schedule)
    hass.states.async_set("sensor.lave_linge_power", "1590")

    await coordinator.async_track_run_progress(now)

    assert coordinator._program_state("lave_linge", "Eco").get("run_trace", []) == []


async def test_async_track_run_progress_skips_finalization_when_calibration_runs_is_zero(hass):
    original_profile = [{CONF_MINUTES: 30, CONF_POWER_W: 1600}]
    coordinator = _profile_coordinator(hass, original_profile, calibration_runs=0)
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    # A trace accumulated before the field was set to 0 (e.g. mid-run), left over in the Store.
    trace = [{"t": (start + timedelta(minutes=i)).isoformat(), "w": 1650.0} for i in range(18)]
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace

    await coordinator.async_track_run_progress(end)

    assert coordinator._program_state("lave_linge", "Eco").get("phases_calibrated") is not True
    assert coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0][CONF_POWER_PROFILE] == original_profile


async def test_async_track_run_progress_finalizes_and_rewrites_config_on_a_significant_change(hass):
    """A real cycle that finished in 17min instead of the declared 30, running a bit hotter
    (1650W instead of 1600W): both differences exceed tolerance, so the profile gets rewritten.
    """
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    trace = [{"t": (start + timedelta(minutes=i)).isoformat(), "w": 1650.0} for i in range(18)]
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace

    await coordinator.async_track_run_progress(end)

    assert coordinator._program_state("lave_linge", "Eco")["phases_calibrated"] is True
    updated = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert updated[CONF_POWER_PROFILE] == [{"minutes": 17, "power_w": 1650}]
    assert updated[CONF_DURATION_MIN] == 17


async def test_async_track_run_progress_skips_calibration_when_the_trace_is_too_short(hass):
    original_profile = [{CONF_MINUTES: 30, CONF_POWER_W: 1600}]
    coordinator = _profile_coordinator(hass, original_profile)
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    trace = [{"t": start.isoformat(), "w": 1650.0}] * (RUN_CALIBRATION_MIN_SAMPLES - 1)
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace

    await coordinator.async_track_run_progress(end)

    assert coordinator._program_state("lave_linge", "Eco")["phases_calibrated"] is True
    assert coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0][CONF_POWER_PROFILE] == original_profile


async def test_async_track_run_progress_does_not_recalibrate_twice_for_the_same_run(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    trace = [{"t": (start + timedelta(minutes=i)).isoformat(), "w": 1650.0} for i in range(18)]
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace
    await coordinator.async_track_run_progress(end)
    calibrated_profile = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0][CONF_POWER_PROFILE]
    # Even if a stray trace shows up afterward (e.g. leftover from a race), phases_calibrated
    # already being True must block a second rewrite.
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = [{"t": end.isoformat(), "w": 9999.0}] * 20

    await coordinator.async_track_run_progress(end + timedelta(minutes=1))

    assert coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0][CONF_POWER_PROFILE] == calibrated_profile


async def _run_and_finalize(coordinator, start, minutes, watts, extra_samples=0):
    """Simulates one full run: seeds a committed slot of `minutes` (plus extra_samples, so the
    trace's actual span matches `minutes` exactly — resegment_power_trace() measures the span
    between the first and last sample, so a trace needs minutes+1 samples to span `minutes`),
    then finalizes it. Mirrors what a real new _set_committed() call does between runs
    (phases_calibrated reset to False; phase_history is deliberately left untouched).
    """
    end = start + timedelta(minutes=minutes)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    coordinator._state["lave_linge"]["Eco"]["phases_calibrated"] = False
    sample_count = minutes + 1 + extra_samples
    trace = [{"t": (start + timedelta(minutes=i)).isoformat(), "w": watts} for i in range(sample_count)]
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace
    await coordinator.async_track_run_progress(end)


async def test_async_track_run_progress_accumulates_history_and_uses_the_max_below_three_runs(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    await _run_and_finalize(coordinator, start, 10, 1000.0)
    await _run_and_finalize(coordinator, start, 15, 1200.0)

    history = coordinator._program_state("lave_linge", "Eco")["phase_history"]
    assert history == [[{"minutes": 10, "power_w": 1000}], [{"minutes": 15, "power_w": 1200}]]
    updated = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert updated[CONF_POWER_PROFILE] == [{"minutes": 15, "power_w": 1200}]


async def test_async_track_run_progress_uses_the_second_highest_once_three_runs_exist(hass):
    """The single highest run (20min/1400W) gets excluded from the aggregate once a 3rd run
    lands — one atypical launch no longer dictates the declared profile by itself.
    """
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    await _run_and_finalize(coordinator, start, 10, 1000.0)
    await _run_and_finalize(coordinator, start, 20, 1400.0)
    await _run_and_finalize(coordinator, start, 15, 1200.0)

    updated = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert updated[CONF_POWER_PROFILE] == [{"minutes": 15, "power_w": 1200}]


async def test_async_track_run_progress_caps_phase_history_at_the_max_run_count(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    for minutes in range(10, 10 + DEFAULT_PHASE_CALIBRATION_RUNS + 1):  # one more run than the cap allows
        await _run_and_finalize(coordinator, start, minutes, 1000.0)

    history = coordinator._program_state("lave_linge", "Eco")["phase_history"]
    assert len(history) == DEFAULT_PHASE_CALIBRATION_RUNS
    assert history[0] == [{"minutes": 11, "power_w": 1000}]  # the first run (10min) was pushed out
    assert history[-1] == [{"minutes": 10 + DEFAULT_PHASE_CALIBRATION_RUNS, "power_w": 1000}]


async def test_async_track_run_progress_uses_a_custom_calibration_runs_cap(hass):
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 1600}], calibration_runs=2)
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    await _run_and_finalize(coordinator, start, 10, 1000.0)
    await _run_and_finalize(coordinator, start, 15, 1200.0)
    await _run_and_finalize(coordinator, start, 20, 1400.0)

    history = coordinator._program_state("lave_linge", "Eco")["phase_history"]
    assert history == [[{"minutes": 15, "power_w": 1200}], [{"minutes": 20, "power_w": 1400}]]


async def test_async_track_run_progress_bootstraps_a_multi_phase_profile_from_a_placeholder(hass):
    """A program declared with a single placeholder phase (entered without knowing the real
    behavior yet) gets its true phase count discovered from the very first run, not just a
    duration correction applied to that same single declared phase.
    """
    coordinator = _profile_coordinator(hass, [{CONF_MINUTES: 30, CONF_POWER_W: 10}])
    start = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=60)
    _seed_committed(coordinator, "lave_linge", "Eco", DeviceSchedule("lave_linge", start, end, 80))
    trace = [
        {"t": (start + timedelta(minutes=i)).isoformat(), "w": w}
        for i, w in enumerate([1000.0] * 10 + [5.0] * 51)
    ]
    coordinator._state["lave_linge"]["Eco"]["run_trace"] = trace

    await coordinator.async_track_run_progress(end)

    updated = coordinator.entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert updated[CONF_POWER_PROFILE] == [{"minutes": 10, "power_w": 1000}, {"minutes": 50, "power_w": 5}]


async def test_the_per_minute_passes_degrade_gracefully_without_a_power_sensor(hass):
    """Robustness check ahead of a HACS default-store submission: this project has only ever run
    against one real install, which always has power_sensor set. A device configured without one
    (_device_options()'s own default) must not crash either per-minute pass; detection/calibration
    simply never has anything to latch onto.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options=_device_options(),
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    start = dt_util.now() - timedelta(minutes=5)
    end = start + timedelta(minutes=30)
    _seed_committed(coordinator, "lave_vaisselle", "Eco", DeviceSchedule("lave_vaisselle", start, end, 80))
    await coordinator.async_set_program_active("lave_vaisselle", "Eco", True)
    await _flush(coordinator)

    await coordinator.async_check_power_detection(dt_util.now())
    await coordinator.async_track_run_progress(dt_util.now())
    await coordinator.async_track_run_progress(end)

    committed = coordinator._get_committed("lave_vaisselle", "Eco")
    assert committed["seen_running"] is False
    state = coordinator._program_state("lave_vaisselle", "Eco")
    assert state.get("phases_calibrated") is True
    assert state.get("run_trace", []) == []
