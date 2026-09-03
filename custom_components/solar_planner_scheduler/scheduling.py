"""Pure scheduling math, mirrored from frontend/solar-planner-card.js. Keep both in sync,
including tests/scheduling.test.js <-> tests/test_scheduling.py. Fields are snake_case here,
camelCase there — the only deliberate difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

SMOOTH_BUCKET_MS = 5 * 60 * 1000
DRAG_SNAP_MS = 5 * 60 * 1000
BUCKET_MS = 30 * 60 * 1000

_SMOOTH_BUCKET = timedelta(milliseconds=SMOOTH_BUCKET_MS)


def _round_half_up(x: float) -> int:
    """Math.round() semantics (always rounds .5 up), not Python's banker's rounding."""
    return int(math.floor(x + 0.5))


def snap_to_grid(dt: datetime, step_ms: int = DRAG_SNAP_MS) -> datetime:
    """Snaps a timestamp to the nearest step_ms-aligned mark."""
    epoch_ms = dt.timestamp() * 1000
    snapped_ms = _round_half_up(epoch_ms / step_ms) * step_ms
    return datetime.fromtimestamp(snapped_ms / 1000, tz=dt.tzinfo)


def interpolate(points: Sequence[dict], t: datetime) -> float:
    if not points:
        return 0.0
    if t <= points[0]["time"]:
        return points[0]["w"]
    last = points[-1]
    if t >= last["time"]:
        return last["w"]
    for a, b in zip(points, points[1:]):
        if a["time"] <= t <= b["time"]:
            span = (b["time"] - a["time"]).total_seconds()
            ratio = (t - a["time"]).total_seconds() / span if span else 0.0
            return a["w"] + (b["w"] - a["w"]) * ratio
    return 0.0


def phase_segments(item: dict) -> list[dict]:
    """Breaks an item into absolute-time phase segments; no profile means one flat segment."""
    profile = item.get("profile")
    if not profile:
        return [{"start": item["start"], "end": item["end"], "power": item["power_w"]}]
    segments = []
    t = item["start"]
    for phase in profile:
        start = t
        t = t + timedelta(minutes=phase["minutes"])
        segments.append({"start": start, "end": t, "power": phase["power_w"]})
    return segments


def _power_at(segments: Sequence[dict], t: datetime) -> float:
    """Power at time t, summing every overlapping segment (never just the first match)."""
    return sum(s["power"] for s in segments if s["start"] <= t < s["end"])


def _instant_steps(
    item_segments: Sequence[dict], other_segments: Sequence[dict], start: datetime, end: datetime
):
    """Yields (t, step_end, mid) sub-buckets, at most SMOOTH_BUCKET_MS wide and never straddling
    an item/other phase boundary — a bucket spanning a short phase's boundary used to misattribute
    the whole bucket to whichever side its midpoint landed in, over- or under-counting deficit.
    """
    breakpoints = {start, end}
    for seg in (*item_segments, *other_segments):
        if start < seg["start"] < end:
            breakpoints.add(seg["start"])
        if start < seg["end"] < end:
            breakpoints.add(seg["end"])
    sorted_bp = sorted(breakpoints)
    for t0, t1 in zip(sorted_bp, sorted_bp[1:]):
        t = t0
        while t < t1:
            step_end = min(t + _SMOOTH_BUCKET, t1)
            yield t, step_end, t + (step_end - t) / 2
            t = step_end


def instant_deficit_wh(
    item_segments: Sequence[dict],
    other_segments: Sequence[dict],
    points: Sequence[dict],
    base_load: float,
    start: datetime,
    end: datetime,
) -> float:
    """Solar-coverage deficit (Wh), checked instant-by-instant at SMOOTH_BUCKET_MS."""
    deficit_wh = 0.0
    for t, step_end, mid in _instant_steps(item_segments, other_segments, start, end):
        item_power = _power_at(item_segments, mid)
        others_power = _power_at(other_segments, mid)
        solar_available = max(0.0, interpolate(points, mid) - base_load - others_power)
        deficit = max(0.0, item_power - solar_available)
        deficit_wh += deficit * (step_end - t).total_seconds() / 3600
    return deficit_wh


def price_at(t: datetime, tariff_bands: Sequence[dict]) -> float:
    """€/kWh at time t. Empty tariff_bands returns the neutral price 1.0.

    Bands carry only a start time ("HH:MM"): the last band whose start is <= t applies, wrapping
    to the last band in the list when t is before the first band of the day.
    """
    if not tariff_bands:
        return 1.0
    sorted_bands = sorted(tariff_bands, key=lambda b: b["start"])
    times = [datetime.strptime(b["start"], "%H:%M").time() for b in sorted_bands]
    now_time = t.time()
    current = sorted_bands[-1]
    for band, band_time in zip(sorted_bands, times):
        if band_time <= now_time:
            current = band
    return current["price"]


def instant_deficit_cost(
    item_segments: Sequence[dict],
    other_segments: Sequence[dict],
    points: Sequence[dict],
    base_load: float,
    start: datetime,
    end: datetime,
    tariff_bands: Sequence[dict],
) -> float:
    """Grid-draw cost (€): same walk as instant_deficit_wh(), priced per step via price_at()."""
    cost = 0.0
    for t, step_end, mid in _instant_steps(item_segments, other_segments, start, end):
        item_power = _power_at(item_segments, mid)
        others_power = _power_at(other_segments, mid)
        solar_available = max(0.0, interpolate(points, mid) - base_load - others_power)
        deficit = max(0.0, item_power - solar_available)
        deficit_kwh = deficit * (step_end - t).total_seconds() / 3600 / 1000
        cost += deficit_kwh * price_at(mid, tariff_bands)
    return cost


def _coverage_ratio(
    item_segments: Sequence[dict],
    other_segments: Sequence[dict],
    points: Sequence[dict],
    base_load: float,
    start: datetime,
    end: datetime,
) -> float:
    """Bounded 0-1 while a shortfall exists; unbounded once fully covered. Use coverage_percent()
    for a display value.
    """
    deficit_wh = instant_deficit_wh(item_segments, other_segments, points, base_load, start, end)
    total_energy_wh = sum(seg["power"] * (seg["end"] - seg["start"]).total_seconds() / 3600 for seg in item_segments)
    if deficit_wh > 0:
        return max(0.0, 1 - deficit_wh / total_energy_wh) if total_energy_wh > 0 else 0.0

    duration_hours = (end - start).total_seconds() / 3600
    avg_power_w = total_energy_wh / duration_hours if duration_hours else 0.0
    min_ratio: Optional[float] = None
    for _, _, mid in _instant_steps(item_segments, other_segments, start, end):
        item_power = _power_at(item_segments, mid)
        if item_power > 0 and item_power >= avg_power_w:
            others_power = _power_at(other_segments, mid)
            solar_available = max(0.0, interpolate(points, mid) - base_load - others_power)
            ratio = solar_available / item_power
            if min_ratio is None or ratio < min_ratio:
                min_ratio = ratio
    return 1.0 if min_ratio is None else min_ratio


def coverage_percent(
    item_segments: Sequence[dict],
    other_segments: Sequence[dict],
    points: Sequence[dict],
    base_load: float,
    start: datetime,
    end: datetime,
) -> int:
    return _round_half_up(_coverage_ratio(item_segments, other_segments, points, base_load, start, end) * 100)


def _fits_peak_ceiling(item_segments: Sequence[dict], other_segments: Sequence[dict], max_simultaneous_power: float) -> bool:
    """Exact max_simultaneous_power check: sweeps real phase boundaries, not fixed buckets."""
    item_start = item_segments[0]["start"]
    item_end = item_segments[-1]["end"]
    all_segments = [*other_segments, *item_segments]
    breakpoints = {item_start, item_end}
    for seg in all_segments:
        if item_start < seg["start"] < item_end:
            breakpoints.add(seg["start"])
        if item_start < seg["end"] < item_end:
            breakpoints.add(seg["end"])
    sorted_bp = sorted(breakpoints)
    for t0, t1 in zip(sorted_bp, sorted_bp[1:]):
        total = sum(seg["power"] for seg in all_segments if seg["start"] <= t0 and t1 <= seg["end"])
        if total > max_simultaneous_power:
            return False
    return True


@dataclass
class Placement:
    index: int
    ratio: float
    coverage_pct: int
    cost: float = 0.0


def find_best_placement(
    buckets: Sequence[dict],
    item: dict,
    max_simultaneous_power: float,
    points: Sequence[dict],
    base_load: float,
    others: Sequence[dict],
    blocked: Sequence[dict] = (),
    tariff_bands: Sequence[dict] = (),
) -> Optional[Placement]:
    """Finds the start bucket minimizing estimated cost, breaking ties by coverage ratio.

    With tariff_bands empty, price_at() is a flat 1.0, so this reproduces the old ratio-only
    ranking. `blocked` excludes candidates outright (same-device mutual exclusion); `others` only
    competes for the shared power budget.
    """
    step = buckets[1]["start"] - buckets[0]["start"] if len(buckets) > 1 else timedelta(milliseconds=DRAG_SNAP_MS)
    step_minutes = step.total_seconds() / 60
    span = max(1, math.ceil(item["duration_min"] / step_minutes))
    other_segments = [seg for o in others if o.get("start") and o.get("end") for seg in phase_segments(o)]

    best: Optional[Placement] = None
    for i in range(0, len(buckets) - span + 1):
        start = buckets[i]["start"]
        end = start + timedelta(minutes=item["duration_min"])
        if any(start < b["end"] and end > b["start"] for b in blocked):
            continue
        candidate = {**item, "start": start, "end": end}
        item_segments = phase_segments(candidate)
        if not _fits_peak_ceiling(item_segments, other_segments, max_simultaneous_power):
            continue
        ratio = _coverage_ratio(item_segments, other_segments, points, base_load, start, end)
        cost = instant_deficit_cost(item_segments, other_segments, points, base_load, start, end, tariff_bands)
        if best is None or cost < best.cost or (cost == best.cost and ratio > best.ratio):
            best = Placement(index=i, ratio=ratio, coverage_pct=_round_half_up(ratio * 100), cost=cost)
    return best


def schedule_proposals(
    buckets: Sequence[dict],
    items: Sequence[dict],
    max_simultaneous_power: float,
    points: Sequence[dict],
    base_load: float,
    pre_committed: Optional[Sequence[dict]] = None,
) -> list[dict]:
    """pre_committed seeds reserved items; each placed item is added so later ones see it."""
    committed = list(pre_committed or [])
    sorted_items = sorted(items, key=lambda it: it["power_w"], reverse=True)
    proposals = []
    for item in sorted_items:
        placement = find_best_placement(buckets, item, max_simultaneous_power, points, base_load, committed)
        if placement is not None:
            start = buckets[placement.index]["start"]
            end = start + timedelta(minutes=item["duration_min"])
            placed = {**item, "start": start, "end": end}
            committed.append(placed)
            proposals.append(placed)
        else:
            proposals.append({**item, "start": None, "end": None})
    return proposals


def find_peak_conflicts(entries: Sequence[dict], max_simultaneous_power: float) -> list[dict]:
    """Sweeps real phase boundaries, sums truly concurrent power, returns conflicting entries
    deduplicated by identity.
    """
    with_segments = [(entry, phase_segments(entry)) for entry in entries if entry.get("start") and entry.get("end")]
    breakpoints = set()
    for _, segments in with_segments:
        for seg in segments:
            breakpoints.add(seg["start"])
            breakpoints.add(seg["end"])
    sorted_bp = sorted(breakpoints)
    conflicted: dict[int, dict] = {}
    for t0, t1 in zip(sorted_bp, sorted_bp[1:]):
        involved = []
        total = 0.0
        for entry, segments in with_segments:
            seg = next((s for s in segments if s["start"] <= t0 and t1 <= s["end"]), None)
            if seg is not None:
                total += seg["power"]
                involved.append(entry)
        if total > max_simultaneous_power:
            for e in involved:
                conflicted[id(e)] = e
    return list(conflicted.values())
