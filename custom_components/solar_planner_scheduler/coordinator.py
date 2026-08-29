"""DataUpdateCoordinator — recomputes the best slot for each configured device on an interval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONSUMPTION_ENTITY,  # noqa: F401 - reserved for a future baseLoad refinement, unused for now
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_W,
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    CONF_SURPLUS_ENTITY,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    NONE_PROGRAM,
)
from .scheduling import DRAG_SNAP_MS, Placement, find_best_placement, interpolate

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceSchedule:
    name: str
    start: datetime | None
    end: datetime | None
    coverage_pct: int | None


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
            period_start = dt_util.parse_datetime(p["period_start"])
            if period_start is None:
                continue
            points.append({"time": period_start, "w": float(p.get("pv_estimate", 0)) * 1000})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(points, key=lambda pt: pt["time"])


def _read_float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _fixed_load_windows(fixed_loads: list[dict], now: datetime) -> list[dict]:
    """One occurrence per fixed load, anchored on today — mirrors the card's daily recurrence."""
    windows = []
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for load in fixed_loads:
        hour, minute = (int(x) for x in load[CONF_START_TIME].split(":"))
        start = day_start.replace(hour=hour, minute=minute)
        end = start + timedelta(minutes=load[CONF_DURATION_MIN])
        windows.append({"start": start, "end": end, "power_w": load[CONF_POWER_W]})
    return windows


class SolarPlannerSchedulerCoordinator(DataUpdateCoordinator[dict[str, DeviceSchedule]]):
    """Polls the configured entities and recomputes each device's best slot for today."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES))
        self.entry = entry

    async def _async_update_data(self) -> dict[str, DeviceSchedule]:
        data = self.entry.data
        options = self.entry.options

        points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_ENTITY))
        tomorrow_points = _read_forecast_points(self.hass, data.get(CONF_FORECAST_TOMORROW_ENTITY))
        points = sorted(points + tomorrow_points, key=lambda pt: pt["time"])

        now = dt_util.now()
        surplus = _read_float_state(self.hass, data.get(CONF_SURPLUS_ENTITY)) or 0.0
        production = _read_float_state(self.hass, data.get(CONF_PRODUCTION_ENTITY))
        if production is None:
            production = interpolate(points, now)
        base_load = max(0.0, production - surplus)

        max_power = data.get(CONF_MAX_SIMULTANEOUS_POWER)
        fixed_loads = _fixed_load_windows(options.get(CONF_FIXED_LOADS, []), now)

        day_end = now.replace(hour=23, minute=55, second=0, microsecond=0)
        buckets = []
        bucket_time = now
        while bucket_time < day_end:
            buckets.append({"start": bucket_time})
            bucket_time = bucket_time + timedelta(milliseconds=DRAG_SNAP_MS)

        results: dict[str, DeviceSchedule] = {}
        committed = list(fixed_loads)
        for device in options.get(CONF_DEVICES, []):
            name = device[CONF_NAME]
            selected = device.get(CONF_SELECTED_PROGRAM)
            if not selected or selected == NONE_PROGRAM:
                results[name] = DeviceSchedule(name, None, None, None)
                continue
            program = next((p for p in device.get(CONF_PROGRAMS, []) if p[CONF_NAME] == selected), None)
            if program is None:
                results[name] = DeviceSchedule(name, None, None, None)
                continue

            profile = program.get(CONF_POWER_PROFILE)
            duration_min = program.get(CONF_DURATION_MIN)
            if duration_min is None and profile:
                duration_min = sum(phase["minutes"] for phase in profile)
            # phase_segments() reads item["profile"], not item["power_profile"] — this key rename is
            # the whole point of going through a program instead of a flat power_w/duration_min pair.
            item = {"profile": profile, "duration_min": duration_min} if profile else {"power_w": program[CONF_POWER_W], "duration_min": duration_min}

            placement: Placement | None = find_best_placement(buckets, item, max_power, points, base_load, committed)
            if placement is None:
                results[name] = DeviceSchedule(name, None, None, None)
                continue
            start = buckets[placement.index]["start"]
            end = start + timedelta(minutes=duration_min)
            results[name] = DeviceSchedule(name, start, end, placement.coverage_pct)
            committed.append({**item, "start": start, "end": end})
        return results
