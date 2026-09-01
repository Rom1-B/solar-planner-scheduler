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
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_IDLE_POWER_THRESHOLD,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_PROGRAMS,
    CONF_START_TIME,
    DEFAULT_IDLE_POWER_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    NONE_PROGRAM,
    WEEKDAYS,
)
from .scheduling import (
    DRAG_SNAP_MS,
    Placement,
    coverage_percent,
    find_best_placement,
    phase_segments,
)

_LOGGER = logging.getLogger(__name__)

# Store schema, per device name then per program name (each program of a device is independently
# schedulable — the device itself is only a mutual-exclusion group, see _async_update_data):
# {
#   "<device_name>": {
#     "<program_name>": {
#       "active": True,                       # whether this program is currently scheduled at all
#       "pending_forced_start": "2026-...",   # a just-set forced start not yet folded into "committed"
#       "committed": {
#           "start": "2026-...", "end": "2026-...", "coverage_pct": 95,
#           "forced": False,                  # user-forced vs auto-computed
#       },
#     },
#   },
# }
#
# Persisted in its own Store rather than kept only on the coordinator's `self.data` — `self.data`
# is wiped by anything that recreates the coordinator (a HA restart, or a structural options-flow
# change, since those still trigger a full entry reload), which would otherwise silently undo the
# anti-flip-flop lock and the current program selection the moment either happened. Writing here
# never goes through `hass.config_entries.async_update_entry`, so routine actions (picking a
# program, forcing a start time) never reload the entry or flicker every device's entities.
STORAGE_VERSION = 1


def _migrate_legacy_state(raw: dict) -> dict:
    """Convert the pre-2026-09-01 schema ({device: {selected, pending_forced_start, committed}})
    to the current one ({device: {program: {active, pending_forced_start, committed}}}).

    Detection: a legacy device entry always has a "selected" key at that level; the current
    schema never does (its keys are program names). Runs once, right after async_load() — the
    result is saved back immediately (see async_load_state), so this heuristic never needs to
    fire again in practice. Idempotent: a device already in the new shape passes through as-is.
    """
    migrated: dict[str, dict] = {}
    for device_name, device_state in raw.items():
        if "selected" not in device_state:
            migrated[device_name] = device_state
            continue
        selected = device_state.get("selected", NONE_PROGRAM)
        migrated[device_name] = {}
        if selected != NONE_PROGRAM:
            program_state: dict = {"active": True}
            if "pending_forced_start" in device_state:
                program_state["pending_forced_start"] = device_state["pending_forced_start"]
            if "committed" in device_state:
                committed = {**device_state["committed"]}
                committed.pop("program", None)
                program_state["committed"] = committed
            migrated[device_name][selected] = program_state
    return migrated


@dataclass
class DeviceSchedule:
    name: str
    start: datetime | None
    end: datetime | None
    coverage_pct: int | None
    forced: bool = False
    power_w: float | None = None
    profile: list | None = None


def compute_locked(schedule: DeviceSchedule, now: datetime) -> bool:
    """Whether the displayed start time is figée (won't be recalculated) or still recalculable.

    True if the user forced it, or the departure is imminent/under way. False once its window has
    fully elapsed *and* the calendar day has changed — not the instant it elapses, so the entity
    keeps showing "what ran today" for the rest of that day instead of blanking out immediately.
    """
    if schedule.start is None or schedule.end is None:
        return False
    if schedule.forced:
        return True
    if now >= schedule.end:
        return now.date() == schedule.start.date()
    if now >= schedule.start:
        return True
    return schedule.start - now <= timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)


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


class SolarPlannerSchedulerCoordinator(DataUpdateCoordinator[dict[tuple[str, str], DeviceSchedule]]):
    """Polls the configured entities and recomputes each active program's best slot for today."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES))
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict[str, dict] = {}

    async def async_load_state(self) -> None:
        """Load persisted per-device-per-program state. Call once before the first refresh."""
        raw = await self._store.async_load() or {}
        self._state = _migrate_legacy_state(raw)
        if self._state != raw:
            await self._store.async_save(self._state)

    def _program_state(self, device_name: str, program_name: str) -> dict:
        return self._state.get(device_name, {}).get(program_name, {})

    def is_program_active(self, device_name: str, program_name: str, program: dict) -> bool:
        """Return the stored activation, or True if the program has non-empty auto_days and was
        never toggled.

        A program declaring auto_days already says "run me on these days" — requiring a manual
        toggle on top of that (e.g. after a Store reset) would defeat auto_days' own purpose. Only
        applies when nothing was ever stored; an explicit False is left as-is.
        """
        stored = self._program_state(device_name, program_name).get("active")
        if stored is not None:
            return stored
        return bool(program.get(CONF_AUTO_DAYS))

    async def async_set_program_active(self, device_name: str, program_name: str, active: bool) -> None:
        state = {**self._program_state(device_name, program_name), "active": active}
        if not active:
            state.pop("committed", None)
            state.pop("pending_forced_start", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)
        await self.async_request_refresh()

    async def async_forget_program(self, device_name: str, program_name: str) -> None:
        """Drop this program's stored state entirely.

        Called from config_flow's "remove program" step so no stale switch/datetime/binary_sensor
        keeps pointing at a program that no longer exists.
        """
        device_state = self._state.get(device_name)
        if device_state is None or program_name not in device_state:
            return
        del device_state[program_name]
        await self._store.async_save(self._state)

    async def async_set_forced_start(self, device_name: str, program_name: str, start: datetime) -> None:
        state = {**self._program_state(device_name, program_name), "pending_forced_start": start.isoformat()}
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)
        await self.async_request_refresh()

    async def async_clear_forced_start(self, device_name: str, program_name: str) -> None:
        state = {**self._program_state(device_name, program_name)}
        state.pop("pending_forced_start", None)
        state.pop("committed", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)
        await self.async_request_refresh()

    def _pending_forced_start(self, device_name: str, program_name: str) -> datetime | None:
        raw = self._program_state(device_name, program_name).get("pending_forced_start")
        return dt_util.parse_datetime(raw) if raw else None

    def _get_committed(self, device_name: str, program_name: str) -> dict | None:
        raw = self._program_state(device_name, program_name).get("committed")
        if raw is None:
            return None
        start = dt_util.parse_datetime(raw["start"])
        end = dt_util.parse_datetime(raw["end"])
        if start is None or end is None:
            return None
        return {
            "start": start,
            "end": end,
            "coverage_pct": raw["coverage_pct"],
            "forced": raw.get("forced", False),
        }

    async def _set_committed(
        self, device_name: str, program_name: str, start: datetime, end: datetime, coverage_pct: int | None, forced: bool
    ) -> None:
        state = {**self._program_state(device_name, program_name)}
        state["committed"] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "coverage_pct": coverage_pct,
            "forced": forced,
        }
        state.pop("pending_forced_start", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)

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

    def _reusable_committed(
        self, device_name: str, program_name: str, device: dict, duration_min: float, now: datetime, auto_days: list[str]
    ) -> tuple[dict | None, bool, bool, bool]:
        """Decide whether to reuse the already-committed slot instead of searching fresh.

        Returns (slot, forced, should_search, dormant):
        - should_search=True means the caller must run a fresh search (or apply a pending forced
          start, handled separately before this is even called).
        - dormant=True means show no schedule at all this cycle, without searching — the "not an
          auto_day" case.

        A changed program duration always forces should_search=True, regardless of auto_days. A
        committed day that has rolled over only searches again if today is one of auto_days;
        otherwise it goes dormant until the program is toggled again (activation is scoped per
        program, so there's no "different program selected" case to check here — a committed
        entry is already scoped to this exact program).

        Without the "close" half of this, every refresh re-runs the search over the whole
        remaining day; a slot later in the day (typically near solar noon) can keep looking
        marginally better than an imminent one, so the "best" start keeps sliding forward and
        never actually arrives.
        """
        committed = self._get_committed(device_name, program_name)
        if committed is None:
            return None, False, True, False
        locked_duration = (committed["end"] - committed["start"]).total_seconds() / 60
        if abs(locked_duration - duration_min) > 0.01:
            return None, False, True, False  # the program's own definition changed — fresh choice
        if committed["start"].date() != now.date():
            if WEEKDAYS[now.weekday()] in auto_days:
                return None, False, True, False  # an auto-day: keep the recurring schedule going
            return None, False, False, True  # not an auto-day: dormant until the selection changes
        if now >= committed["end"]:
            # Elapsed but still today: keep showing it (locked stays true) instead of blanking out,
            # so the entity still reflects "what ran today" until the calendar day rolls over.
            return committed, committed["forced"], False, False
        if now >= committed["start"]:
            if not committed["forced"] and self._failed_to_start(device, now):
                return None, False, True, False  # unlock: recompute instead of waiting forever
            return committed, committed["forced"], False, False
        if committed["forced"] or committed["start"] - now <= timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES):
            return committed, committed["forced"], False, False
        return None, False, True, False

    def _find_slot_for_day(
        self,
        item: dict,
        duration_min: float,
        points: list[dict],
        base_load: float,
        committed: list[dict],
        max_power: float,
        day_offset: int,
        blocked: list[dict] | None = None,
    ) -> dict | None:
        buckets = _day_buckets(dt_util.now(), day_offset)
        if not buckets:
            return None
        placement: Placement | None = find_best_placement(
            buckets, item, max_power, points, base_load, committed, blocked or ()
        )
        if placement is None:
            return None
        start = buckets[placement.index]["start"]
        end = start + timedelta(minutes=duration_min)
        return {"start": start, "end": end, "coverage_pct": placement.coverage_pct}

    @staticmethod
    def _compute_slot_from_start(
        item: dict, duration_min: float, start: datetime, points: list[dict], base_load: float, committed: list[dict]
    ) -> dict:
        end = start + timedelta(minutes=duration_min)
        item_segments = phase_segments({**item, "start": start, "end": end})
        other_segments = [seg for o in committed if o.get("start") and o.get("end") for seg in phase_segments(o)]
        coverage_pct = coverage_percent(item_segments, other_segments, points, base_load, start, end)
        return {"start": start, "end": end, "coverage_pct": coverage_pct}

    @staticmethod
    def _schedule_from_slot(name: str, slot: dict | None, item: dict | None, forced: bool) -> DeviceSchedule:
        if slot is None:
            return DeviceSchedule(name, None, None, None)
        return DeviceSchedule(
            name,
            slot["start"],
            slot["end"],
            slot["coverage_pct"],
            forced,
            power_w=item.get("power_w") if item else None,
            profile=item.get("profile") if item else None,
        )

    async def _async_update_data(self) -> dict[tuple[str, str], DeviceSchedule]:
        data = self.entry.data
        options = self.entry.options

        points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_ENTITY))
        tomorrow_points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_TOMORROW_ENTITY))
        points = sorted(points + tomorrow_points, key=lambda pt: pt["time"])

        now = dt_util.now()
        # No live "background consumption" estimate: production - surplus at a single instant
        # spikes whenever any large load happens to be running right at update time, and that
        # spike used to get stretched across the whole scheduling horizon. Only declared consumers
        # (fixed_loads, scheduled devices) are deducted.
        base_load = 0.0

        max_power = data.get(CONF_MAX_SIMULTANEOUS_POWER)
        fixed_loads = _fixed_load_windows(options.get(CONF_FIXED_LOADS, []), now)

        results: dict[tuple[str, str], DeviceSchedule] = {}
        committed = list(fixed_loads)
        for device in options.get(CONF_DEVICES, []):
            device_name = device[CONF_NAME]
            programs = device.get(CONF_PROGRAMS, [])
            # Each sibling program's currently-known slot for *today*, used as this device's hard
            # exclusion zone. Pre-seeded from the Store (not built up empty and only as programs
            # are visited this pass) so a program earlier in CONF_PROGRAMS order still avoids a
            # sibling that already has a slot committed from an earlier refresh cycle and isn't
            # being re-resolved this cycle (reused, dormant, or not yet visited) — the previous
            # accumulate-as-you-go approach missed exactly this case: activating program A while
            # program B (later in config order) was already committed let A's search ignore B
            # entirely, since B's slot only entered the exclusion list once B itself was visited.
            # Filtered to today: a stale multi-day-old commitment (e.g. a dormant on-demand
            # program that hasn't rolled over its Store entry) must not block a fresh search.
            device_slots: dict[str, dict | None] = {}
            for p in programs:
                existing = self._get_committed(device_name, p[CONF_NAME])
                device_slots[p[CONF_NAME]] = existing if existing and existing["start"].date() == now.date() else None

            for program in programs:
                program_name = program[CONF_NAME]
                key = (device_name, program_name)
                if not self.is_program_active(device_name, program_name, program):
                    device_slots[program_name] = None
                    results[key] = DeviceSchedule(device_name, None, None, None)
                    continue

                # Every program has phases (power_profile), the config UI has never produced anything else.
                profile = program[CONF_POWER_PROFILE]
                duration_min = program.get(CONF_DURATION_MIN)
                if duration_min is None:
                    duration_min = sum(phase[CONF_MINUTES] for phase in profile)
                item = {"profile": profile, "duration_min": duration_min}
                blocked = [
                    {"start": slot["start"], "end": slot["end"]}
                    for name, slot in device_slots.items()
                    if name != program_name and slot is not None
                ]

                def _finalize(slot: dict | None) -> None:
                    device_slots[program_name] = slot
                    if slot is not None:
                        committed.append({**item, "start": slot["start"], "end": slot["end"]})

                pending_start = self._pending_forced_start(device_name, program_name)
                if pending_start is not None:
                    slot = self._compute_slot_from_start(item, duration_min, pending_start, points, base_load, committed)
                    await self._set_committed(
                        device_name, program_name, slot["start"], slot["end"], slot["coverage_pct"], True
                    )
                    _finalize(slot)
                    results[key] = self._schedule_from_slot(device_name, slot, item, True)
                    continue

                auto_days = program.get(CONF_AUTO_DAYS, [])
                slot, forced, should_search, dormant = self._reusable_committed(
                    device_name, program_name, device, duration_min, now, auto_days
                )
                if dormant:
                    device_slots[program_name] = None
                    results[key] = DeviceSchedule(device_name, None, None, None)
                    continue
                if should_search:
                    if points:
                        slot = self._find_slot_for_day(
                            item, duration_min, points, base_load, committed, max_power, 0, blocked=blocked
                        )
                        forced = False
                        if slot is not None:
                            await self._set_committed(
                                device_name, program_name, slot["start"], slot["end"], slot["coverage_pct"], False
                            )
                    else:
                        # No forecast data yet: every candidate would tie at 0% coverage, and the
                        # search would silently keep the very first bucket ("now") — wait for real
                        # data on the next refresh instead of committing to a guessed slot.
                        slot = None

                _finalize(slot)
                results[key] = self._schedule_from_slot(device_name, slot, item, forced)
        return results
