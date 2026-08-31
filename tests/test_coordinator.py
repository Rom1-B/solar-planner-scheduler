"""Tests for coordinator.py helpers.

Needs pytest-homeassistant-custom-component installed (see requirements-dev.txt), both for
`coordinator.py`'s module-level `homeassistant` imports to resolve and, for the forecast-points
test below, to set mock entity states via the `hass` fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.const import (
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FORECAST_ENTITY,
    CONF_IDLE_POWER_THRESHOLD,
    CONF_MANUAL,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.coordinator import (
    ALREADY_RAN,
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


def _seed_committed(coordinator, name, schedule, program="Eco"):
    """Populate the persisted-state dict directly, as if a previous cycle had committed to it —
    mirrors what _set_committed() stores (ISO strings), since _locked_today_slot reads from
    self._committed (the Store-backed dict), not self.data, so it survives a coordinator recreate.
    """
    coordinator._committed[name] = {
        "start": schedule.start.isoformat(),
        "end": schedule.end.isoformat(),
        "coverage_pct": schedule.coverage_pct,
        "program": program,
    }


def _coordinator(hass, previous_schedule=None, program="Eco"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    if previous_schedule is not None:
        _seed_committed(coordinator, "lave_linge", previous_schedule, program)
    return coordinator


def test_locked_today_slot_reuses_an_imminent_slot_without_recomputing(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "program": "Eco"}


def test_locked_today_slot_returns_none_when_the_target_is_far_off(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES + 1)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[])

    assert slot is None


def test_locked_today_slot_keeps_a_slot_in_progress_without_a_power_sensor(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[])

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "program": "Eco"}


def test_locked_today_slot_unlocks_when_a_power_sensor_shows_it_never_started(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "0")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power"}
    slot = coordinator._locked_today_slot(
        "lave_linge", device, duration_min=30, now=now, selected_program="Eco", auto_days=[]
    )

    assert slot is None


def test_locked_today_slot_stays_locked_when_the_power_sensor_shows_it_running(hass):
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))
    hass.states.async_set("sensor.lave_linge_power", "1800")

    device = {CONF_POWER_SENSOR: "sensor.lave_linge_power", CONF_IDLE_POWER_THRESHOLD: 10}
    slot = coordinator._locked_today_slot(
        "lave_linge", device, duration_min=30, now=now, selected_program="Eco", auto_days=[]
    )

    assert slot == {"start": start, "end": end, "coverage_pct": 95, "program": "Eco"}


def test_locked_today_slot_searches_immediately_when_a_different_program_is_selected(hass):
    """Picking a program is always an explicit user action: it must get a fresh proposal right
    away, regardless of auto_days — auto_days only governs whether an *unchanged* selection keeps
    getting rescheduled automatically once the calendar day rolls over (see the two tests below).
    """
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95), program="Eco")

    slot = coordinator._locked_today_slot(
        "lave_linge", {}, duration_min=30, now=now, selected_program="Intense", auto_days=[]
    )

    assert slot is None


def test_locked_today_slot_reschedules_once_the_program_duration_changes_after_a_run(hass):
    start = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    slot = coordinator._locked_today_slot(
        "lave_linge", {}, duration_min=60, now=now, selected_program="Eco", auto_days=[]
    )

    assert slot is None


def test_locked_today_slot_flags_already_ran_once_the_window_has_fully_elapsed(hass):
    """A device is scheduled once per selection: once its committed window has passed, don't
    propose another slot the same day just because a later window now scores well.
    """
    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)
    start = now - timedelta(minutes=40)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    slot = coordinator._locked_today_slot("lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[])

    assert slot is ALREADY_RAN


def test_locked_today_slot_continues_the_recurring_schedule_on_an_auto_day(hass):
    """A previous day's run for the same, still-selected program doesn't keep blocking today, as
    long as today is one of the program's auto_days — that's the recurring-schedule case (e.g. the
    washing machine's Friday/Sunday routine).
    """
    start = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)  # Saturday
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)  # Sunday
    slot = coordinator._locked_today_slot(
        "lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=["fri", "sun"]
    )

    assert slot is None


def test_locked_today_slot_stays_dormant_on_a_non_auto_day_for_the_same_selection(hass):
    """Without an auto_day match, an unchanged selection doesn't keep proposing a new slot every
    day on its own — this is the on-demand case (e.g. a dishwasher run whenever it's loaded): it
    stays dormant until the user picks the program again, rather than auto-repeating daily.
    """
    start = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    coordinator = _coordinator(hass, DeviceSchedule("lave_linge", start, end, 95))

    now = datetime(2026, 8, 30, 9, 13, tzinfo=timezone.utc)  # the next day, not an auto_day
    slot = coordinator._locked_today_slot(
        "lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[]
    )

    assert slot is ALREADY_RAN


def test_locking_stops_a_committed_slot_from_flip_flopping_across_refresh_cycles(hass):
    """Simulates the real bug observed live: sensor.lave_linge_next_start alternated between an
    imminent slot ("09:15") and a much-later one ("12:05") on consecutive coordinator refreshes,
    because find_best_placement was re-run from scratch every cycle and a slot near solar noon
    kept outscoring the imminent one. Reproduces the same "search disagrees each cycle" condition
    with a stubbed find_slot_for_day, and checks that once a slot is locked, the stub is never
    even called again — the disagreement can no longer reach the result.
    """
    near = {"start": datetime(2026, 8, 30, 9, 15, tzinfo=timezone.utc), "end": datetime(2026, 8, 30, 9, 45, tzinfo=timezone.utc), "coverage_pct": 95}
    far = {"start": datetime(2026, 8, 30, 12, 5, tzinfo=timezone.utc), "end": datetime(2026, 8, 30, 12, 35, tzinfo=timezone.utc), "coverage_pct": 180}
    # find_slot_for_day() itself never returns a "program" key (only _set_committed/_get_committed
    # attach one) — locked() below returns a committed slot instead once seeded, which does carry
    # one, so the searched-for reference must too for the equality checks to hold in later cycles.
    near_committed = {**near, "program": "Eco"}

    call_count = 0

    def flapping_search(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Old (buggy) behavior: a real search re-run every cycle would alternate depending on
        # which candidate the ever-shifting "now" horizon currently favors.
        return near if call_count % 2 == 1 else far

    coordinator = _coordinator(hass, previous_schedule=None)
    coordinator._find_slot_for_day = flapping_search

    device = {}
    duration_min = 30

    def locked(now):
        return coordinator._locked_today_slot(
            "lave_linge", device, duration_min, now, selected_program="Eco", auto_days=[]
        )

    # Cycle 1, far from "near"'s start: nothing to lock onto yet, falls through to the search.
    now = datetime(2026, 8, 30, 8, 30, tzinfo=timezone.utc)
    slot = locked(now) or coordinator._find_slot_for_day()
    assert slot == near
    _seed_committed(coordinator, "lave_linge", DeviceSchedule("lave_linge", slot["start"], slot["end"], slot["coverage_pct"]))
    assert call_count == 1

    # Cycle 2, now within DEFAULT_UPDATE_INTERVAL_MINUTES of "near"'s start: locks onto it — the
    # search (which would have flip-flopped to "far") is never even invoked.
    now = datetime(2026, 8, 30, 9, 5, tzinfo=timezone.utc)
    slot = locked(now) or coordinator._find_slot_for_day()
    assert slot == near_committed, "expected the imminent slot to stay locked instead of jumping to the later one"
    assert call_count == 1, "the search must not be re-run once locked"
    _seed_committed(coordinator, "lave_linge", DeviceSchedule("lave_linge", slot["start"], slot["end"], slot["coverage_pct"]))

    # Cycle 3, now inside the committed window: still locked, still no re-search.
    now = datetime(2026, 8, 30, 9, 20, tzinfo=timezone.utc)
    slot = locked(now) or coordinator._find_slot_for_day()
    assert slot == near_committed
    assert call_count == 1


async def test_committed_state_survives_a_coordinator_recreate(hass):
    """Regression test: self.data (where the lock/ALREADY_RAN state used to live) is wiped by
    anything that recreates the coordinator — a HA restart, or any options-flow change at all,
    since every one triggers a full entry reload via the update listener. Committed state must
    survive that, via the Store instead of self.data.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={},
    )
    entry.add_to_hass(hass)

    now = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
    start = now - timedelta(minutes=40)
    end = start + timedelta(minutes=30)

    coordinator_a = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator_a.async_load_state()
    await coordinator_a._set_committed("lave_linge", start, end, 95, "Eco")

    # A brand new coordinator instance, as async_setup_entry creates on every reload — self.data
    # starts empty, but async_load_state() must recover what was persisted above.
    coordinator_b = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator_b.async_load_state()
    assert coordinator_b.data is None  # confirms this genuinely isn't reading self.data

    slot = coordinator_b._locked_today_slot(
        "lave_linge", {}, duration_min=30, now=now, selected_program="Eco", auto_days=[]
    )

    assert slot is ALREADY_RAN


async def test_switching_a_device_to_manual_clears_any_stale_auto_commitment(hass):
    """Regression test for the reported bug: a device left in manual mode for a while can carry a
    stale auto commitment from days earlier (auto mode never got a chance to overwrite it). When
    the user later switches back to auto, that stale entry must not silently apply — the manual
    branch drops it unconditionally, so the very next auto refresh treats the selection as brand
    new and searches immediately, regardless of auto_days.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 4000},
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_vaisselle",
                    CONF_SELECTED_PROGRAM: "Eco",
                    CONF_MANUAL: True,
                    CONF_PROGRAMS: [{CONF_NAME: "Eco", CONF_POWER_W: 100, CONF_DURATION_MIN: 30, CONF_AUTO_DAYS: []}],
                }
            ]
        },
    )
    entry.add_to_hass(hass)
    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    _seed_committed(
        coordinator,
        "lave_vaisselle",
        DeviceSchedule("lave_vaisselle", datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc), datetime(2026, 8, 25, 13, 30, tzinfo=timezone.utc), 95),
        program="Eco",
    )

    await coordinator._async_update_data()

    assert coordinator._get_committed("lave_vaisselle") is None
