"""DataUpdateCoordinator — recomputes the best slot for each configured device on an interval."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACCEPTED_DAY_TODAY,
    ACCEPTED_DAY_TOMORROW,
    CONF_ACCEPTED_DATE,
    CONF_ACCEPTED_DAY,
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_DURATION_TOLERANCE_PERCENT,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_HISTORY_LOOKBACK_DAYS,
    CONF_IDLE_POWER_THRESHOLD,
    CONF_MANUAL,
    CONF_MANUAL_START,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PROGRAMS,
    CONF_RUN_GAP_TOLERANCE_MINUTES,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    DEFAULT_DURATION_TOLERANCE_PERCENT,
    DEFAULT_HISTORY_LOOKBACK_DAYS,
    DEFAULT_IDLE_POWER_THRESHOLD,
    DEFAULT_RUN_GAP_TOLERANCE_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    NONE_PROGRAM,
    WEEKDAYS,
)
from .scheduling import (
    DRAG_SNAP_MS,
    GOOD_ENOUGH_COVERAGE_PCT,
    Placement,
    coverage_percent,
    detect_runs,
    find_best_placement,
    phase_segments,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel returned by _locked_today_slot(): the committed slot already ran (its window elapsed),
# or a calendar day has rolled over past it on a day not in the program's auto_days — don't search
# for a new one. Distinct from None ("nothing locked, go search fresh"), which _locked_today_slot
# returns instead once auto_days allows today again, or once the selected program itself changes
# (always an immediate fresh search, regardless of auto_days).
ALREADY_RAN = object()

# Committed-slot state (what _locked_today_slot reads) is persisted in its own Store rather than
# kept only on the coordinator's `self.data` — `self.data` is wiped by anything that recreates the
# coordinator (a HA restart, or any options-flow change at all, since every one of them triggers a
# full entry reload via the update listener), which used to silently undo the anti-flip-flop lock
# and the "already ran today" guard the moment either happened. A dedicated Store survives both.
# Keyed by name so other coordinator-internal state that needs to survive a reload can live here
# too later, without needing its own Store.
STORAGE_VERSION = 1


@dataclass
class DeviceSchedule:
    name: str
    start: datetime | None
    end: datetime | None
    coverage_pct: int | None
    approximate: bool = False
    today_coverage_pct: int | None = None
    tomorrow_coverage_pct: int | None = None
    power_w: float | None = None
    profile: list | None = None


def _read_forecast_points(hass: HomeAssistant, entity_id: str | None) -> list[dict]:
    if not entity_id:
        return []
    state = hass.states.get(entity_id)
    if state is None:
        return []
    detailed = state.attributes.get("detailedForecast")
    if not isinstance(detailed, list):
        return []
    points = []
    for p in detailed:
        try:
            # Solcast stores period_start as a real datetime object in its own in-memory attributes
            # (only serialized to an ISO string once it crosses the WS/REST API boundary) — parse_datetime()
            # requires a string, so a raw datetime here must be used as-is instead.
            period_start = p["period_start"]
            if isinstance(period_start, str):
                period_start = dt_util.parse_datetime(period_start)
            if period_start is None:
                continue
            points.append({"time": period_start, "w": float(p.get("pv_estimate", 0)) * 1000})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(points, key=lambda pt: pt["time"])


def _ceil_to_five_minutes(dt: datetime) -> datetime:
    """Round dt up to the next 5-minute clock mark (never into the past relative to dt)."""
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = (dt - day_start).total_seconds() / 60
    ceiled_minutes = math.ceil(minutes_since_midnight / 5) * 5
    return day_start + timedelta(minutes=ceiled_minutes)


def _day_buckets(now: datetime, day_offset: int) -> list[dict]:
    """5-minute-grid buckets for `now`'s day (day_offset=0, from now to 23:55) or a future day
    (day_offset>=1, the full day from midnight to 23:55).

    Today's buckets start at the next 5-minute mark, not at `now` itself, so an auto-scheduled
    start time always lands on a multiple of 5 (matching the card's drag-to-reschedule grid) —
    same as future days, which are naturally aligned by starting at midnight.
    """
    if day_offset == 0:
        start = _ceil_to_five_minutes(now)
        day_end = now.replace(hour=23, minute=55, second=0, microsecond=0)
    else:
        day_start = (now + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start
        day_end = day_start.replace(hour=23, minute=55, second=0, microsecond=0)
    buckets = []
    bucket_time = start
    while bucket_time < day_end:
        buckets.append({"start": bucket_time})
        bucket_time += timedelta(milliseconds=DRAG_SNAP_MS)
    return buckets


def _fixed_load_windows(fixed_loads: list[dict], now: datetime) -> list[dict]:
    """One occurrence per fixed load, anchored on today — mirrors the card's daily recurrence.

    Each window carries the whole multi-phase `profile`; phase_segments() (scheduling.py) already
    knows how to walk a profile into per-phase segments from a single start time, so a single
    window per fixed load is enough regardless of how many phases it has.
    """
    windows = []
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for load in fixed_loads:
        # TimeSelector returns "HH:MM:SS"; only hour/minute matter here, seconds are ignored.
        hour, minute = (int(x) for x in load[CONF_START_TIME].split(":")[:2])
        start = day_start.replace(hour=hour, minute=minute)
        profile = load[CONF_POWER_PROFILE]
        end = start + timedelta(minutes=sum(p[CONF_MINUTES] for p in profile))
        windows.append({"start": start, "end": end, "profile": profile})
    return windows


class SolarPlannerSchedulerCoordinator(DataUpdateCoordinator[dict[str, DeviceSchedule]]):
    """Polls the configured entities and recomputes each device's best slot for today."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES))
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._committed: dict[str, dict] = {}

    async def async_load_state(self) -> None:
        """Load persisted committed-slot state. Call once before the first refresh."""
        self._committed = await self._store.async_load() or {}

    def _get_committed(self, name: str) -> dict | None:
        raw = self._committed.get(name)
        if raw is None:
            return None
        start = dt_util.parse_datetime(raw["start"])
        end = dt_util.parse_datetime(raw["end"])
        if start is None or end is None:
            return None
        return {"start": start, "end": end, "coverage_pct": raw["coverage_pct"], "program": raw.get("program")}

    async def _set_committed(
        self, name: str, start: datetime, end: datetime, coverage_pct: int | None, program: str
    ) -> None:
        self._committed[name] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "coverage_pct": coverage_pct,
            "program": program,
        }
        await self._store.async_save(self._committed)

    async def _clear_committed(self, name: str) -> None:
        if self._committed.pop(name, None) is not None:
            await self._store.async_save(self._committed)

    async def _estimate_program_power(self, device: dict, program: dict) -> tuple[float | None, bool]:
        """Estimates a flat-power program's real power from power_sensor history.

        Returns (avg_w, approximate). (None, False) means no usable history was found — the
        caller falls back to the program's declared power_w.
        """
        power_sensor = device.get(CONF_POWER_SENSOR)
        if not power_sensor:
            return None, False

        # Deferred: recorder.history queries the DB and must run in an executor, not the event loop.
        # The exact call (state_changes_during_period) has been stable across many HA releases, but
        # verify it against the target HA version before relying on it — see CLAUDE.local.md.
        from homeassistant.components.recorder import get_instance, history

        end = dt_util.now()
        lookback_days = self.entry.data.get(CONF_HISTORY_LOOKBACK_DAYS, DEFAULT_HISTORY_LOOKBACK_DAYS)
        start = end - timedelta(days=lookback_days)
        states_by_entity = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period, self.hass, start, end, power_sensor
        )
        samples = []
        for state in states_by_entity.get(power_sensor, []):
            try:
                samples.append({"time": state.last_changed, "value": float(state.state)})
            except ValueError:
                continue

        idle_threshold = self.entry.data.get(CONF_IDLE_POWER_THRESHOLD, DEFAULT_IDLE_POWER_THRESHOLD)
        gap_tolerance_min = self.entry.data.get(CONF_RUN_GAP_TOLERANCE_MINUTES, DEFAULT_RUN_GAP_TOLERANCE_MINUTES)
        runs = detect_runs(samples, idle_threshold, gap_tolerance_min)
        if not runs:
            return None, False

        target_duration = program.get(CONF_DURATION_MIN)
        tolerance = self.entry.data.get(CONF_DURATION_TOLERANCE_PERCENT, DEFAULT_DURATION_TOLERANCE_PERCENT) / 100
        matching = [
            r for r in runs if target_duration and abs(r["duration_min"] - target_duration) / target_duration <= tolerance
        ]
        if matching:
            return sum(r["avg_w"] for r in matching) / len(matching), False
        return sum(r["avg_w"] for r in runs) / len(runs), True

    def _failed_to_start(self, device: dict, now: datetime) -> bool:
        """True only if a power sensor is configured and currently reads below the idle threshold.

        Without a power sensor there's no telemetry to tell whether the device was actually
        started, so we trust the committed window rather than force an endless unlock/relock loop.
        """
        power_sensor = device.get(CONF_POWER_SENSOR)
        if not power_sensor:
            return False
        state = self.hass.states.get(power_sensor)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        try:
            power = float(state.state)
        except ValueError:
            return False
        idle_threshold = self.entry.data.get(CONF_IDLE_POWER_THRESHOLD, DEFAULT_IDLE_POWER_THRESHOLD)
        return power < idle_threshold

    def _locked_today_slot(
        self, name: str, device: dict, duration_min: float, now: datetime, selected_program: str, auto_days: list[str]
    ):
        """Reuse the already-committed slot instead of re-searching from scratch, once it's close
        (within one update_interval — the same constant the coordinator itself polls on, so the
        lock can never be a cycle late) or already under way. Returns ALREADY_RAN once that slot's
        window has fully elapsed: a device is meant to run once per selection, not keep proposing
        a new slot for the rest of the day once it's already had its committed run.

        Reads from the persisted Store (via _get_committed), not self.data — self.data is wiped by
        anything that recreates the coordinator (a HA restart, or any options-flow change, since
        every one triggers a full entry reload), which would otherwise silently undo this the
        moment either happened.

        Without the "close" half of this, every refresh re-runs find_best_placement over the whole
        remaining day; a slot later in the day (typically near solar noon) can keep looking
        marginally better than an imminent one, so the "best" start keeps sliding forward and never
        actually arrives — the device's target time is perpetually a few minutes away but never
        reached. Without the ALREADY_RAN half, a device would get proposed a second slot the same
        day (or any day after) as soon as another good-enough window appears.

        Picking a program is always an explicit user action, so a program (or duration) change
        always forces a fresh search regardless of auto_days — auto_days only decides whether an
        *unchanged* selection keeps getting a new slot proposed automatically once the calendar day
        rolls over, without the user touching anything.
        """
        previous = self._get_committed(name)
        if previous is None:
            return None
        if previous.get("program") != selected_program:
            return None  # a different program was picked — always a fresh, immediate search
        locked_duration = (previous["end"] - previous["start"]).total_seconds() / 60
        if abs(locked_duration - duration_min) > 0.01:
            return None  # the program's own definition changed since committed — fresh choice
        if previous["start"].date() != now.date():
            if WEEKDAYS[now.weekday()] in auto_days:
                return None  # an auto-day: keep the recurring schedule going automatically
            return ALREADY_RAN  # not an auto-day — stay dormant until the selection changes again
        if now >= previous["end"]:
            return ALREADY_RAN
        if now >= previous["start"]:
            if self._failed_to_start(device, now):
                return None  # unlock: recompute a fresh slot instead of waiting forever
            return previous
        if previous["start"] - now <= timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES):
            return previous
        return None

    def _find_slot_for_day(
        self,
        item: dict,
        duration_min: float,
        points: list[dict],
        base_load: float,
        committed: list[dict],
        max_power: float,
        day_offset: int,
    ) -> dict | None:
        buckets = _day_buckets(dt_util.now(), day_offset)
        if not buckets:
            return None
        placement: Placement | None = find_best_placement(buckets, item, max_power, points, base_load, committed)
        if placement is None:
            return None
        start = buckets[placement.index]["start"]
        end = start + timedelta(minutes=duration_min)
        return {"start": start, "end": end, "coverage_pct": placement.coverage_pct}

    def _resolve_accepted_day(self, device: dict, now: datetime) -> str | None:
        """What to commit to unconditionally this cycle: "today", "tomorrow", or None (compare fresh).

        A "tomorrow" acceptance only applies to the calendar day it was made on — once that day has
        become today, it collapses to "today"; once it's fully passed it's stale and ignored, so an
        unattended device never stays locked onto a day that no longer exists.
        """
        accepted_day = device.get(CONF_ACCEPTED_DAY)
        accepted_date_str = device.get(CONF_ACCEPTED_DATE)
        if not accepted_day or not accepted_date_str:
            return None
        accepted_date = dt_util.parse_date(accepted_date_str)
        if accepted_date is None:
            return None
        target_date = accepted_date + timedelta(days=1 if accepted_day == ACCEPTED_DAY_TOMORROW else 0)
        if now.date() > target_date:
            return None
        if now.date() == target_date:
            return ACCEPTED_DAY_TODAY
        return accepted_day

    @staticmethod
    def _manual_slot(
        device: dict, item: dict, duration_min: float, points: list[dict], base_load: float, committed: list[dict]
    ) -> dict | None:
        manual_start_str = device.get(CONF_MANUAL_START)
        if not manual_start_str:
            return None
        start = dt_util.parse_datetime(manual_start_str)
        if start is None:
            return None
        end = start + timedelta(minutes=duration_min)
        item_segments = phase_segments({**item, "start": start, "end": end})
        other_segments = [seg for o in committed if o.get("start") and o.get("end") for seg in phase_segments(o)]
        coverage_pct = coverage_percent(item_segments, other_segments, points, base_load, start, end)
        return {"start": start, "end": end, "coverage_pct": coverage_pct}

    @staticmethod
    def _schedule_from_slot(name: str, slot: dict | None, approximate: bool, item: dict | None = None) -> DeviceSchedule:
        if slot is None:
            return DeviceSchedule(name, None, None, None, approximate)
        return DeviceSchedule(
            name,
            slot["start"],
            slot["end"],
            slot["coverage_pct"],
            approximate,
            power_w=item.get(CONF_POWER_W) if item else None,
            profile=item.get("profile") if item else None,
        )

    async def _async_update_data(self) -> dict[str, DeviceSchedule]:
        data = self.entry.data
        options = self.entry.options

        points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_ENTITY))
        tomorrow_points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_TOMORROW_ENTITY))
        points = sorted(points + tomorrow_points, key=lambda pt: pt["time"])

        now = dt_util.now()
        # No live "background consumption" estimate: production - surplus at a single instant
        # spikes whenever any large load happens to be running right at update time, and that
        # spike used to get stretched across the whole scheduling horizon (today + tomorrow).
        # Only declared consumers (fixed_loads, scheduled devices) are deducted from now on.
        base_load = 0.0

        max_power = data.get(CONF_MAX_SIMULTANEOUS_POWER)
        fixed_loads = _fixed_load_windows(options.get(CONF_FIXED_LOADS, []), now)

        forecast_tomorrow = data.get(CONF_FORECAST_TOMORROW_ENTITY)

        results: dict[str, DeviceSchedule] = {}
        committed = list(fixed_loads)
        for device in options.get(CONF_DEVICES, []):
            name = device[CONF_NAME]
            selected = device.get(CONF_SELECTED_PROGRAM)
            if not selected or selected == NONE_PROGRAM:
                await self._clear_committed(name)
                results[name] = DeviceSchedule(name, None, None, None)
                continue
            program = next((p for p in device.get(CONF_PROGRAMS, []) if p[CONF_NAME] == selected), None)
            if program is None:
                await self._clear_committed(name)
                results[name] = DeviceSchedule(name, None, None, None)
                continue

            profile = program.get(CONF_POWER_PROFILE)
            duration_min = program.get(CONF_DURATION_MIN)
            if duration_min is None and profile:
                duration_min = sum(phase["minutes"] for phase in profile)

            approximate = False
            if profile:
                # phase_segments() reads item["profile"], not item["power_profile"] — this key rename
                # is the whole point of going through a program instead of a flat power_w pair.
                item = {"profile": profile, "duration_min": duration_min}
            else:
                estimated_w, approximate = await self._estimate_program_power(device, program)
                power_w = estimated_w if estimated_w is not None else program.get(CONF_POWER_W)
                item = {"power_w": power_w, "duration_min": duration_min}

            def _append_committed(slot: dict | None) -> None:
                if slot is not None:
                    committed.append({**item, "start": slot["start"], "end": slot["end"]})

            if device.get(CONF_MANUAL, False):
                # Manual mode fully owns scheduling while active — drop any leftover auto
                # commitment so that switching back to auto is always treated as a fresh
                # selection (an immediate search), not as a stale prior-day auto commitment.
                await self._clear_committed(name)
                slot = self._manual_slot(device, item, duration_min, points, base_load, committed)
                _append_committed(slot)
                results[name] = self._schedule_from_slot(name, slot, approximate, item)
                continue

            accepted = self._resolve_accepted_day(device, now)
            if accepted == ACCEPTED_DAY_TOMORROW:
                # Without forecast data every candidate ties at 0% coverage, so find_best_placement
                # would silently keep the very first bucket — right after a HA restart, before the
                # forecast integration has populated its state, that's "now" at 0%. Only ever reuse
                # an already-committed slot in that case, never guess a fresh one.
                slot = self._find_slot_for_day(item, duration_min, points, base_load, committed, max_power, 1) if points else None
                _append_committed(slot)
                results[name] = self._schedule_from_slot(name, slot, approximate, item)
                continue

            auto_days = program.get(CONF_AUTO_DAYS, [])
            locked = self._locked_today_slot(name, device, duration_min, now, selected, auto_days)
            if locked is ALREADY_RAN:
                # Already had its committed run: stop here, don't even look at tomorrow — a device
                # is scheduled once per selection, not re-proposed as soon as another window scores
                # well. Runs again only once the program selection itself changes.
                results[name] = DeviceSchedule(name, None, None, None, approximate)
                continue
            if locked is not None:
                today_slot = locked
            elif points:
                today_slot = self._find_slot_for_day(item, duration_min, points, base_load, committed, max_power, 0)
                if today_slot is not None:
                    await self._set_committed(
                        name, today_slot["start"], today_slot["end"], today_slot["coverage_pct"], selected
                    )
            else:
                # No forecast data yet and nothing locked to fall back on — wait for real data on
                # the next refresh instead of committing to a guessed "now" slot at 0% coverage.
                today_slot = None

            if accepted == ACCEPTED_DAY_TODAY:
                _append_committed(today_slot)
                results[name] = self._schedule_from_slot(name, today_slot, approximate, item)
                continue

            today_good_enough = today_slot is not None and today_slot["coverage_pct"] >= GOOD_ENOUGH_COVERAGE_PCT
            if today_good_enough or not forecast_tomorrow:
                _append_committed(today_slot)
                results[name] = self._schedule_from_slot(name, today_slot, approximate, item)
                continue

            tomorrow_slot = (
                self._find_slot_for_day(item, duration_min, points, base_load, committed, max_power, 1)
                if points
                else None
            )
            tomorrow_is_better = tomorrow_slot is not None and (
                today_slot is None or tomorrow_slot["coverage_pct"] > today_slot["coverage_pct"]
            )
            if tomorrow_is_better:
                # Neither day is committed yet — the user decides via accept_today/accept_tomorrow.
                results[name] = DeviceSchedule(
                    name,
                    None,
                    None,
                    None,
                    approximate,
                    today_coverage_pct=today_slot["coverage_pct"] if today_slot else None,
                    tomorrow_coverage_pct=tomorrow_slot["coverage_pct"],
                )
                continue

            _append_committed(today_slot)
            results[name] = self._schedule_from_slot(name, today_slot, approximate, item)
        return results
