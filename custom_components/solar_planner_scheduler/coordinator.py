"""DataUpdateCoordinator — recomputes the best slot for each configured device on an interval."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

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
    CONF_POWER_W,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
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
    instant_deficit_cost,
    phase_segments,
)

_LOGGER = logging.getLogger(__name__)

# Store schema, per device then per program (each program is independently schedulable; the
# device itself is only a mutual-exclusion group):
# {
#   "<device_name>": {
#     "<program_name>": {
#       "active": True,
#       "active_set_on": "2026-01-01",
#       "pending_forced_start": "2026-...",
#       "pending_power_detected_at": "2026-...",
#       "committed": {"start": "2026-...", "end": "2026-...", "coverage_pct": 95, "forced": False},
#     },
#   },
# }
#
# Kept in its own Store, not `self.data`: `self.data` is wiped on every coordinator recreation
# (HA restart, options change), which would drop the lock and selection. Writing here never
# reloads the entry, so picking a program or forcing a start doesn't flicker every entity.
STORAGE_VERSION = 1

# One failed_to_start unlock is normal (startup lag); this many in a row means it never started.
# At DEFAULT_UPDATE_INTERVAL_MINUTES (5 min) this is ~15 min of grace before raising a Repair.
FAILED_TO_START_REPAIR_THRESHOLD = 3

# How far today's search extends past midnight, e.g. to reach a cheap overnight tariff band.
NIGHT_EXTENSION_HOURS = 5

# How far from a program's planned start (before or after) a power reading is still trusted as
# belonging to that same run, for recalibrating the committed slot onto the real start time.
MANUAL_START_TOLERANCE_MINUTES = 30


def _migrate_legacy_state(raw: dict) -> dict:
    """Convert the legacy schema ({device: {selected, ...}}) to the current one
    ({device: {program: {active, ...}}}). A "selected" key marks a legacy entry; idempotent.
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
    estimated_cost: float | None = None


def compute_locked(schedule: DeviceSchedule, now: datetime) -> bool:
    """True if forced or the departure is imminent/under way. Stays true after the window elapses
    until the calendar day changes, so the entity keeps showing "what ran today".
    """
    if schedule.start is None or schedule.end is None:
        return False
    if schedule.forced:
        return True
    if now >= schedule.end:
        return now.date() == schedule.end.date()
    if now >= schedule.start:
        return True
    return schedule.start - now <= timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)


def _is_relevant_today(committed: dict | None, now: datetime) -> bool:
    """Whether a committed slot should still block a sibling program's search: started today, or
    still running (covers an overnight slot started yesterday, not just stale multi-day-old ones).
    """
    if committed is None:
        return False
    return committed["start"].date() == now.date() or now < committed["end"]


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
            # Solcast's in-memory period_start is a real datetime, only a string over the WS/REST API.
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
    """5-minute-grid buckets: today (day_offset=0) from the next 5-min mark to 23:55 plus
    NIGHT_EXTENSION_HOURS into tomorrow morning (covers an overnight cheap-tariff window), or a
    future day (day_offset>=1) from midnight to 23:55 — aligned to the card's drag grid.
    """
    if day_offset == 0:
        start = _ceil_to_five_minutes(now)
        day_end = now.replace(hour=23, minute=55, second=0, microsecond=0) + timedelta(hours=NIGHT_EXTENSION_HOURS)
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
    """One occurrence per fixed load, anchored on today."""
    windows = []
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for load in fixed_loads:
        # TimeSelector returns "HH:MM:SS"; seconds are ignored.
        hour, minute = (int(x) for x in load[CONF_START_TIME].split(":")[:2])
        start = day_start.replace(hour=hour, minute=minute)
        profile = load[CONF_POWER_PROFILE]
        end = start + timedelta(minutes=sum(p[CONF_MINUTES] for p in profile))
        windows.append({"name": load[CONF_NAME], "start": start, "end": end, "profile": profile})
    return windows


class SolarPlannerSchedulerCoordinator(DataUpdateCoordinator[dict[tuple[str, str], DeviceSchedule]]):
    """Polls the configured entities and recomputes each active program's best slot for today."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES))
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict[str, dict] = {}
        # Recomputed every _async_update_data() cycle, never persisted — same "always derived,
        # never stale-cached across restarts" choice as `results` itself.
        self._fixed_load_costs: dict[str, float] = {}

    def fixed_load_cost(self, name: str) -> float | None:
        """€ cost of a fixed load's daily window, or None if tariff tracking is off."""
        return self._fixed_load_costs.get(name)

    def diagnostics_snapshot(self) -> dict:
        """Everything diagnostics.py exposes: the persisted Store plus the last update's outcome."""
        return {
            "store": self._state,
            "last_update_success": self.last_update_success,
            "last_exception": repr(self.last_exception) if self.last_exception else None,
        }

    async def async_load_state(self) -> None:
        """Load persisted per-device-per-program state. Call once before the first refresh."""
        raw = await self._store.async_load() or {}
        self._state = _migrate_legacy_state(raw)
        if self._state != raw:
            await self._store.async_save(self._state)

    def _program_state(self, device_name: str, program_name: str) -> dict:
        return self._state.get(device_name, {}).get(program_name, {})

    def is_program_active(self, device_name: str, program_name: str, program: dict) -> bool:
        """Stored activation, or True if never stored and auto_days is non-empty.

        A manual deactivation only holds for the day it was set: an auto_days program is meant to
        run every one of those days, so switching it off "just for today" must not silence every
        following auto_day too — once the calendar day rolls over, a stale False is ignored.
        """
        state = self._program_state(device_name, program_name)
        stored = state.get("active")
        if stored is None:
            return bool(program.get(CONF_AUTO_DAYS))
        if not stored and program.get(CONF_AUTO_DAYS) and state.get("active_set_on") != dt_util.now().date().isoformat():
            return True
        return stored

    async def async_set_program_active(self, device_name: str, program_name: str, active: bool) -> None:
        state = {
            **self._program_state(device_name, program_name),
            "active": active,
            "active_set_on": dt_util.now().date().isoformat(),
        }
        if not active:
            state.pop("committed", None)
            state.pop("pending_forced_start", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)
        await self.async_request_refresh()

    async def async_forget_program(self, device_name: str, program_name: str) -> None:
        """Drop this program's stored state entirely (called when it's removed from config)."""
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
            "cost": raw.get("cost"),
            "seen_running": raw.get("seen_running", False),
        }

    async def _set_committed(
        self,
        device_name: str,
        program_name: str,
        start: datetime,
        end: datetime,
        coverage_pct: int | None,
        forced: bool,
        cost: float | None = None,
        seen_running: bool = False,
    ) -> None:
        state = {**self._program_state(device_name, program_name)}
        state["committed"] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "coverage_pct": coverage_pct,
            "forced": forced,
            "cost": cost,
            "seen_running": seen_running,
        }
        state.pop("pending_forced_start", None)
        state.pop("pending_power_detected_at", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)

    def _current_power(self, power_sensor: str | None) -> float | None:
        if not power_sensor:
            return None
        state = self.hass.states.get(power_sensor)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _idle_threshold_for(self, profile: list[dict]) -> float:
        """max(configured floor, half the program's first phase power).

        A slow-starting program (low first phase) stays at the floor, unchanged from before; a
        program that jumps straight to a peak gets a stricter, more accurate threshold instead of
        the same flat floor for everyone.
        """
        floor = self.entry.data.get(CONF_IDLE_POWER_THRESHOLD, DEFAULT_IDLE_POWER_THRESHOLD)
        if not profile:
            return floor
        return max(floor, profile[0][CONF_POWER_W] * 0.5)

    def _failed_to_start(self, device: dict, committed: dict, now: datetime, idle_threshold: float) -> bool:
        """True only if power has never reached the idle threshold since this slot's committed start.

        Checking the instant reading alone breaks once the real appliance finishes its actual cycle
        faster than the configured power_profile: a later poll would see it back at idle and wrongly
        conclude it never ran. `committed["seen_running"]` (set by `_update_seen_running`) latches the
        first observed run so a later dip is never mistaken for a failed start.
        """
        if committed.get("seen_running"):
            return False
        power = self._current_power(device.get(CONF_POWER_SENSOR))
        if power is None:
            return False
        return power < idle_threshold

    async def _update_seen_running(
        self,
        device_name: str,
        program_name: str,
        device: dict,
        item: dict,
        duration_min: float,
        points: list[dict],
        base_load: float,
        committed: list[dict],
        idle_threshold: float,
        now: datetime,
    ) -> None:
        """Latch committed["seen_running"] the first time power reaches idle threshold, and
        recalibrate the committed start/end to that real detection time.

        A program started by hand rarely begins at the exact planned minute; a stale start/end
        otherwise keeps other auto-scheduled programs' power-budget checks blind to the real
        overlap, and the gantt keeps showing a time that never actually happened. Detection is
        trusted from MANUAL_START_TOLERANCE_MINUTES before the planned start onward, up to the
        planned end: earlier than that, a power reading is assumed unrelated to this program.

        `async_check_power_detection()`, a lighter per-minute pass, may have already recorded the
        precise minute a crossing happened in `pending_power_detected_at`; when present and still
        within the trusted window, it's used instead of this cycle's own `now`, for better than
        DEFAULT_UPDATE_INTERVAL_MINUTES precision without running the full cycle more often. Falls
        back to a live check at `now` otherwise (the very first cycle to see it, or a missed beat).

        Recalibrating sets forced=True for a genuinely early detection (more than
        DEFAULT_UPDATE_INTERVAL_MINUTES before the planned start) or once in progress (now >=
        start): in both cases "was this auto or manual" stops mattering, only "it's running, leave
        it alone" does. Within DEFAULT_UPDATE_INTERVAL_MINUTES right before the planned start,
        forced is left as it already was: the existing imminent-window logic in
        `_reusable_committed()` and `compute_locked()` already freezes the slot there regardless of
        forced, so marking it forced would change nothing but the stored value's honesty.
        """
        existing = self._program_state(device_name, program_name)
        committed_raw = existing.get("committed")
        if committed_raw is None or committed_raw.get("seen_running"):
            return
        start = dt_util.parse_datetime(committed_raw["start"])
        end = dt_util.parse_datetime(committed_raw["end"])
        if start is None or end is None:
            return
        window_start = start - timedelta(minutes=MANUAL_START_TOLERANCE_MINUTES)

        detected_raw = existing.get("pending_power_detected_at")
        detected_at = dt_util.parse_datetime(detected_raw) if detected_raw else None
        if detected_at is not None and window_start <= detected_at < end:
            effective_now = detected_at
        else:
            if not (window_start <= now < end):
                return
            power = self._current_power(device.get(CONF_POWER_SENSOR))
            if power is None or power < idle_threshold:
                return
            effective_now = now

        imminent_start = start - timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)
        forced = True if effective_now < imminent_start or effective_now >= start else committed_raw.get("forced", False)
        slot = self._compute_slot_from_start(item, duration_min, effective_now, points, base_load, committed)
        await self._set_committed(
            device_name,
            program_name,
            slot["start"],
            slot["end"],
            slot["coverage_pct"],
            forced,
            slot["cost"],
            seen_running=True,
        )

    async def async_check_power_detection(self, now: datetime) -> None:
        """Lightweight per-minute pass: record the exact minute a program's power first crosses its
        idle threshold, without the full per-cycle work (search, tariffs, Repair tracking).

        Only ever writes one Store field, `pending_power_detected_at`, consumed by
        `_update_seen_running()` on the next normal coordinator cycle to do the actual
        recalibration — this keeps DEFAULT_UPDATE_INTERVAL_MINUTES free to control search/tariff/
        Repair cadence while still catching a real start within about a minute, not up to a full
        cycle late.
        """
        for device in self.entry.options.get(CONF_DEVICES, []):
            device_name = device[CONF_NAME]
            for program in device.get(CONF_PROGRAMS, []):
                program_name = program[CONF_NAME]
                if not self.is_program_active(device_name, program_name, program):
                    continue
                existing = self._program_state(device_name, program_name)
                committed_raw = existing.get("committed")
                if (
                    committed_raw is None
                    or committed_raw.get("seen_running")
                    or existing.get("pending_power_detected_at")
                ):
                    continue
                start = dt_util.parse_datetime(committed_raw["start"])
                end = dt_util.parse_datetime(committed_raw["end"])
                if start is None or end is None:
                    continue
                window_start = start - timedelta(minutes=MANUAL_START_TOLERANCE_MINUTES)
                if not (window_start <= now < end):
                    continue
                power = self._current_power(device.get(CONF_POWER_SENSOR))
                if power is None:
                    continue
                idle_threshold = self._idle_threshold_for(program.get(CONF_POWER_PROFILE, []))
                if power < idle_threshold:
                    continue
                self._state.setdefault(device_name, {})[program_name] = {
                    **existing,
                    "pending_power_detected_at": now.isoformat(),
                }
                await self._store.async_save(self._state)

    @staticmethod
    def _failed_to_start_issue_id(device_name: str, program_name: str) -> str:
        return f"failed_to_start_{slugify(f'{device_name} {program_name}')}"

    async def _note_failed_to_start(self, device_name: str, program_name: str, failed: bool) -> None:
        """Track consecutive failed_to_start unlocks and raise a Repair past the threshold; clears
        on any cycle that doesn't fail.
        """
        existing = self._program_state(device_name, program_name)
        streak = existing.get("failed_start_streak", 0)
        issue_id = self._failed_to_start_issue_id(device_name, program_name)
        if failed:
            streak += 1
            self._state.setdefault(device_name, {})[program_name] = {**existing, "failed_start_streak": streak}
            await self._store.async_save(self._state)
            if streak >= FAILED_TO_START_REPAIR_THRESHOLD:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="failed_to_start",
                    translation_placeholders={"device": device_name, "program": program_name},
                )
        elif streak:
            self._state.setdefault(device_name, {})[program_name] = {**existing, "failed_start_streak": 0}
            await self._store.async_save(self._state)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _reusable_committed(
        self,
        device_name: str,
        program_name: str,
        device: dict,
        duration_min: float,
        now: datetime,
        auto_days: list[str],
        idle_threshold: float = DEFAULT_IDLE_POWER_THRESHOLD,
    ) -> tuple[dict | None, bool, bool, bool, bool]:
        """Decide whether to reuse the already-committed slot instead of searching fresh.

        Returns (slot, forced, should_search, dormant, failed_to_start). should_search=True means
        run a fresh search; dormant=True means no schedule this cycle (not an auto_day);
        failed_to_start=True only for the "power sensor never showed it running" unlock, to track
        a repair-worthy streak.

        A changed duration always forces should_search. A rolled-over day re-searches only if
        today is an auto_day, else goes dormant — but only once the slot has actually elapsed, so
        an overnight slot crossing midnight (see NIGHT_EXTENSION_HOURS) isn't cut off mid-run.
        Reusing an imminent/in-progress slot instead of re-searching every cycle avoids the "best
        start" sliding forward and never arriving.
        """
        committed = self._get_committed(device_name, program_name)
        if committed is None:
            return None, False, True, False, False
        locked_duration = (committed["end"] - committed["start"]).total_seconds() / 60
        if abs(locked_duration - duration_min) > 0.01:
            return None, False, True, False, False  # the program's own definition changed — fresh choice
        if now >= committed["start"]:
            if now < committed["end"]:
                # In progress — even overnight, this is never a day rollover.
                if not committed["forced"] and self._failed_to_start(device, committed, now, idle_threshold):
                    return None, False, True, False, True  # unlock: recompute instead of waiting forever
                return committed, committed["forced"], False, False, False
            if committed["start"].date() != now.date():
                if WEEKDAYS[now.weekday()] in auto_days:
                    return None, False, True, False, False  # an auto-day: keep the recurring schedule going
                return None, False, False, True, False  # not an auto-day: dormant until the selection changes
            # Elapsed but still the day it started: keep showing it until the calendar day rolls over.
            return committed, committed["forced"], False, False, False
        if committed["forced"] or committed["start"] - now <= timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES):
            return committed, committed["forced"], False, False, False
        return None, False, True, False, False

    def _tariff_bands(self) -> list[dict]:
        """Tariff bands, or [] (neutral price) when tracking is disabled."""
        if not self.entry.data.get(CONF_PRICE_TRACKING_ENABLED, False):
            return []
        return self.entry.data.get(CONF_TARIFF_BANDS, [])

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
            buckets, item, max_power, points, base_load, committed, blocked or (), self._tariff_bands()
        )
        if placement is None:
            return None
        start = buckets[placement.index]["start"]
        end = start + timedelta(minutes=duration_min)
        return {"start": start, "end": end, "coverage_pct": placement.coverage_pct, "cost": placement.cost}

    def _compute_slot_from_start(
        self, item: dict, duration_min: float, start: datetime, points: list[dict], base_load: float, committed: list[dict]
    ) -> dict:
        end = start + timedelta(minutes=duration_min)
        item_segments = phase_segments({**item, "start": start, "end": end})
        other_segments = [seg for o in committed if o.get("start") and o.get("end") for seg in phase_segments(o)]
        coverage_pct = coverage_percent(item_segments, other_segments, points, base_load, start, end)
        cost = instant_deficit_cost(item_segments, other_segments, points, base_load, start, end, self._tariff_bands())
        return {"start": start, "end": end, "coverage_pct": coverage_pct, "cost": cost}

    def _schedule_from_slot(self, name: str, slot: dict | None, item: dict | None, forced: bool) -> DeviceSchedule:
        if slot is None:
            return DeviceSchedule(name, None, None, None)
        price_tracking_enabled = self.entry.data.get(CONF_PRICE_TRACKING_ENABLED, False)
        return DeviceSchedule(
            name,
            slot["start"],
            slot["end"],
            slot["coverage_pct"],
            forced,
            power_w=item.get("power_w") if item else None,
            profile=item.get("profile") if item else None,
            estimated_cost=slot.get("cost") if price_tracking_enabled else None,
        )

    async def _async_update_data(self) -> dict[tuple[str, str], DeviceSchedule]:
        data = self.entry.data
        options = self.entry.options

        points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_ENTITY))
        tomorrow_points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_TOMORROW_ENTITY))
        points = sorted(points + tomorrow_points, key=lambda pt: pt["time"])

        now = dt_util.now()
        # No live background-consumption estimate: only declared consumers are deducted.
        base_load = 0.0

        max_power = data.get(CONF_MAX_SIMULTANEOUS_POWER)
        fixed_loads = _fixed_load_windows(options.get(CONF_FIXED_LOADS, []), now)

        results: dict[tuple[str, str], DeviceSchedule] = {}
        committed = list(fixed_loads)
        for device in options.get(CONF_DEVICES, []):
            device_name = device[CONF_NAME]
            programs = device.get(CONF_PROGRAMS, [])
            # Sibling programs' today-relevant slots, pre-seeded from the Store so an earlier-order
            # program still avoids a sibling committed in an earlier cycle, not just this pass.
            device_slots: dict[str, dict | None] = {}
            for p in programs:
                existing = self._get_committed(device_name, p[CONF_NAME])
                device_slots[p[CONF_NAME]] = existing if _is_relevant_today(existing, now) else None

            for program in programs:
                program_name = program[CONF_NAME]
                key = (device_name, program_name)
                if not self.is_program_active(device_name, program_name, program):
                    await self._note_failed_to_start(device_name, program_name, False)
                    device_slots[program_name] = None
                    results[key] = DeviceSchedule(device_name, None, None, None)
                    continue

                profile = program[CONF_POWER_PROFILE]
                duration_min = program.get(CONF_DURATION_MIN)
                if duration_min is None:
                    duration_min = sum(phase[CONF_MINUTES] for phase in profile)
                item = {"profile": profile, "duration_min": duration_min}
                idle_threshold = self._idle_threshold_for(profile)
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
                        device_name, program_name, slot["start"], slot["end"], slot["coverage_pct"], True, slot["cost"]
                    )
                    await self._note_failed_to_start(device_name, program_name, False)
                    _finalize(slot)
                    results[key] = self._schedule_from_slot(device_name, slot, item, True)
                    continue

                auto_days = program.get(CONF_AUTO_DAYS, [])
                await self._update_seen_running(
                    device_name, program_name, device, item, duration_min, points, base_load, committed, idle_threshold, now
                )
                slot, forced, should_search, dormant, failed_to_start = self._reusable_committed(
                    device_name, program_name, device, duration_min, now, auto_days, idle_threshold
                )
                await self._note_failed_to_start(device_name, program_name, failed_to_start)
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
                                device_name,
                                program_name,
                                slot["start"],
                                slot["end"],
                                slot["coverage_pct"],
                                False,
                                slot["cost"],
                            )
                    else:
                        # No forecast data yet: wait for it instead of committing a guessed slot.
                        slot = None

                _finalize(slot)
                results[key] = self._schedule_from_slot(device_name, slot, item, forced)

        # Fixed loads are never scheduled (their window is fixed), but still draw from the grid
        # when solar can't cover them, same as a scheduled device. Computed once `committed` holds
        # every other device/fixed-load's final window, so "others" reflects the whole picture.
        self._fixed_load_costs = {}
        if data.get(CONF_PRICE_TRACKING_ENABLED, False):
            tariff_bands = self._tariff_bands()
            for load in fixed_loads:
                item_segments = phase_segments(load)
                other_segments = [
                    seg
                    for o in committed
                    if o is not load and o.get("start") and o.get("end")
                    for seg in phase_segments(o)
                ]
                self._fixed_load_costs[load["name"]] = instant_deficit_cost(
                    item_segments, other_segments, points, base_load, load["start"], load["end"], tariff_bands
                )

        return results
