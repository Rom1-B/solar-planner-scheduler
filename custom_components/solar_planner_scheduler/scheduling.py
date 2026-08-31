"""Pure scheduling math.

Ported line-for-line from solar-planner-card's solar-planner-card.js
(github.com/Rom1-B/solar-planner-card) so the two projects never disagree about what "best slot"
or "solar coverage" means. Any algorithm change in the JS reference must be mirrored here, along
with its accompanying case in tests/scheduling.test.js -> tests/test_scheduling.py.

Field names are snake_case here (power_w, duration_min, device_name) where the JS uses camelCase
(powerW, durationMin, deviceName) — the only deliberate naming difference from the reference.
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
    t = start
    while t < end:
        step_end = min(t + _SMOOTH_BUCKET, end)
        mid = t + (step_end - t) / 2
        item_power = _power_at(item_segments, mid)
        others_power = _power_at(other_segments, mid)
        solar_available = max(0.0, interpolate(points, mid) - base_load - others_power)
        deficit = max(0.0, item_power - solar_available)
        deficit_wh += deficit * (step_end - t).total_seconds() / 3600
        t = step_end
    return deficit_wh


def _coverage_ratio(
    item_segments: Sequence[dict],
    other_segments: Sequence[dict],
    points: Sequence[dict],
    base_load: float,
    start: datetime,
    end: datetime,
) -> float:
    """Deficit weighted by energy share while any shortfall exists (bounded 0-1); once fully
    covered, worst ratio among above-average-power instants only (unbounded). Unrounded — callers
    needing a display value should go through coverage_percent().
    """
    deficit_wh = instant_deficit_wh(item_segments, other_segments, points, base_load, start, end)
    total_energy_wh = sum(seg["power"] * (seg["end"] - seg["start"]).total_seconds() / 3600 for seg in item_segments)
    if deficit_wh > 0:
        return (1 - deficit_wh / total_energy_wh) if total_energy_wh > 0 else 0.0

    duration_hours = (end - start).total_seconds() / 3600
    avg_power_w = total_energy_wh / duration_hours if duration_hours else 0.0
    min_ratio: Optional[float] = None
    t = start
    while t < end:
        step_end = min(t + _SMOOTH_BUCKET, end)
        mid = t + (step_end - t) / 2
        item_power = _power_at(item_segments, mid)
        if item_power > 0 and item_power >= avg_power_w:
            others_power = _power_at(other_segments, mid)
            solar_available = max(0.0, interpolate(points, mid) - base_load - others_power)
            ratio = solar_available / item_power
            if min_ratio is None or ratio < min_ratio:
                min_ratio = ratio
        t = step_end
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


def find_best_placement(
    buckets: Sequence[dict],
    item: dict,
    max_simultaneous_power: float,
    points: Sequence[dict],
    base_load: float,
    others: Sequence[dict],
) -> Optional[Placement]:
    """Finds the start bucket maximizing coverage ratio (unrounded — avoids ties from rounding).

    max_simultaneous_power stays a hard filter via _fits_peak_ceiling; the candidate step comes
    from `buckets` itself, not a hardcoded 30 min.
    """
    step = buckets[1]["start"] - buckets[0]["start"] if len(buckets) > 1 else timedelta(milliseconds=DRAG_SNAP_MS)
    step_minutes = step.total_seconds() / 60
    span = max(1, math.ceil(item["duration_min"] / step_minutes))
    other_segments = [seg for o in others if o.get("start") and o.get("end") for seg in phase_segments(o)]

    best: Optional[Placement] = None
    for i in range(0, len(buckets) - span + 1):
        start = buckets[i]["start"]
        end = start + timedelta(minutes=item["duration_min"])
        candidate = {**item, "start": start, "end": end}
        item_segments = phase_segments(candidate)
        if not _fits_peak_ceiling(item_segments, other_segments, max_simultaneous_power):
            continue
        ratio = _coverage_ratio(item_segments, other_segments, points, base_load, start, end)
        if best is None or ratio > best.ratio:
            best = Placement(index=i, ratio=ratio, coverage_pct=_round_half_up(ratio * 100))
    return best


def schedule_proposals(
    buckets: Sequence[dict],
    items: Sequence[dict],
    max_simultaneous_power: float,
    points: Sequence[dict],
    base_load: float,
    pre_committed: Optional[Sequence[dict]] = None,
) -> list[dict]:
    """pre_committed seeds already-reserved items; each newly placed item is added to it so later
    items in the batch see earlier placements.
    """
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
    """Exact conflict check: sweeps real phase boundaries and sums truly concurrent power.

    Returns a list of the conflicting entry dicts, deduplicated by identity (JS returns a Set of
    object references; Python dicts aren't hashable, so this is the closest equivalent).
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
