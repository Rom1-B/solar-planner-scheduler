"""DataUpdateCoordinator — recomputes the best slot for each configured device on an interval."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

if TYPE_CHECKING:
    from .pv_forecast import PvForecastCoordinator

from .const import (
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_IDLE_POWER_THRESHOLD,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DEFAULT_IDLE_POWER_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    FORECAST_SOURCE_COMPUTED,
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
#       "pending_forced_start": "2026-...",
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
FAILED_TO_START_REPAIR_THRESHOLD = 2

# How far today's search extends past midnight, e.g. to reach a cheap overnight tariff band.
NIGHT_EXTENSION_HOURS = 5


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
        windows.append({"start": start, "end": end, "profile": profile})
    return windows


class SolarPlannerSchedulerCoordinator(DataUpdateCoordinator[dict[tuple[str, str], DeviceSchedule]]):
    """Polls the configured entities and recomputes each active program's best slot for today."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES))
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict[str, dict] = {}
        # Set from __init__.py right after construction, only when PV params are configured,
        # independent of forecast_source, so it can run for comparison even while an external
        # entity (e.g. Solcast) is the one actually driving scheduling.
        self.pv_forecast_coordinator: "PvForecastCoordinator | None" = None

    def diagnostics_snapshot(self) -> dict:
        """Everything diagnostics.py exposes: the persisted Store plus the last update's outcome."""
        snapshot = {
            "store": self._state,
            "last_update_success": self.last_update_success,
            "last_exception": repr(self.last_exception) if self.last_exception else None,
        }
        if self.pv_forecast_coordinator is not None:
            snapshot["pv_forecast_last_update_success"] = self.pv_forecast_coordinator.last_update_success
            snapshot["pv_forecast_last_exception"] = (
                repr(self.pv_forecast_coordinator.last_exception) if self.pv_forecast_coordinator.last_exception else None
            )
        return snapshot

    async def async_load_state(self) -> None:
        """Load persisted per-device-per-program state. Call once before the first refresh."""
        raw = await self._store.async_load() or {}
        self._state = _migrate_legacy_state(raw)
        if self._state != raw:
            await self._store.async_save(self._state)

    def _program_state(self, device_name: str, program_name: str) -> dict:
        return self._state.get(device_name, {}).get(program_name, {})

    def is_program_active(self, device_name: str, program_name: str, program: dict) -> bool:
        """Stored activation, or True if never stored and auto_days is non-empty."""
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
    ) -> None:
        state = {**self._program_state(device_name, program_name)}
        state["committed"] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "coverage_pct": coverage_pct,
            "forced": forced,
            "cost": cost,
        }
        state.pop("pending_forced_start", None)
        self._state.setdefault(device_name, {})[program_name] = state
        await self._store.async_save(self._state)

    def _failed_to_start(self, device: dict, now: datetime) -> bool:
        """True only if a power sensor is configured and reads below the idle threshold."""
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
        self, device_name: str, program_name: str, device: dict, duration_min: float, now: datetime, auto_days: list[str]
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
                if not committed["forced"] and self._failed_to_start(device, now):
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

        if data.get(CONF_FORECAST_SOURCE) == FORECAST_SOURCE_COMPUTED:
            points = list(self.pv_forecast_coordinator.data or []) if self.pv_forecast_coordinator else []
            tomorrow_points = []
        else:
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
                slot, forced, should_search, dormant, failed_to_start = self._reusable_committed(
                    device_name, program_name, device, duration_min, now, auto_days
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
        return results
