"""Ported 1:1 from solar-planner-card's tests/scheduling.test.js — same scenarios, same expected
values, so this port's behavior is verified against the exact same cases as the JS reference
rather than re-derived from memory. Keep this file in lockstep with the JS one.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.solar_planner_scheduler.scheduling import (
    BUCKET_MS,
    DRAG_SNAP_MS,
    detect_runs,
    find_best_placement,
    find_peak_conflicts,
    instant_deficit_wh,
    coverage_percent,
    phase_segments,
    schedule_proposals,
    snap_to_grid,
)

DAY = datetime(2026, 1, 15, tzinfo=timezone.utc)


def t(h, m):
    return DAY + timedelta(hours=h, minutes=m)


def mk_buckets(watts, start_hour=8):
    return [{"start": DAY + timedelta(milliseconds=(start_hour * 2 + i) * BUCKET_MS)} for i in range(len(watts))]


def mk_points(watts, start_hour=8):
    return [{"time": DAY + timedelta(milliseconds=(start_hour * 2 + i) * BUCKET_MS), "w": w} for i, w in enumerate(watts)]


def test_sunny_day_earliest_perfect_fit_slot_wins():
    watts = [100, 100, 3000, 3000, 3000, 3000, 100, 100]
    buckets = mk_buckets(watts)
    points = mk_points(watts)
    item = {"power_w": 2000, "duration_min": 60}
    placement = find_best_placement(buckets, item, 4000, points, 0, [])
    assert placement.index == 2
    assert placement.coverage_pct == 150


def test_winter_day_no_perfect_fit_least_bad_slot_returned():
    # Under instant (not bucket-mean) scoring, the interpolated curve ramps between values instead
    # of stepping — the true best-worst-instant 2-bucket window straddles the 400/500 rise and
    # 500/450 fall (index 3), not just the single 500 bucket's own pair.
    watts = [100, 150, 300, 400, 500, 450, 300, 150]
    buckets = mk_buckets(watts)
    points = mk_points(watts)
    item = {"power_w": 2000, "duration_min": 60}
    placement = find_best_placement(buckets, item, 10000, points, 0, [])
    assert placement is not None
    assert placement.index == 3
    assert placement.coverage_pct < 100, "winter day never fully covers a 2000 W hour"


def test_find_best_placement_prefers_smaller_real_grid_draw():
    # A: brief severe dip. B: evenly ~90% all hour. A draws less total energy from the grid.
    points = [
        {"time": t(10, 0), "w": 1200},
        {"time": t(10, 24), "w": 1200},
        {"time": t(10, 25), "w": 200},  # brief severe dip inside candidate A's window
        {"time": t(10, 30), "w": 200},
        {"time": t(10, 31), "w": 1200},
        {"time": t(11, 0), "w": 1200},
        {"time": t(12, 0), "w": 900},  # candidate B: flat, evenly under the 1000 W item the whole hour
        {"time": t(13, 0), "w": 900},
    ]
    item = {"power_w": 1000, "duration_min": 60}
    buckets = [{"start": t(10, 0)}, {"start": t(12, 0)}]
    placement = find_best_placement(buckets, item, 4000, points, 0, [])
    assert placement.index == 0, "expected candidate A (smaller real grid draw) over candidate B"
    assert placement.coverage_pct == 93


def test_max_simultaneous_power_stays_a_hard_filter_even_over_zero_deficit_candidate():
    watts = [3000, 3000, 3000, 3000]
    buckets = mk_buckets(watts)
    points = mk_points(watts)
    others = [{"start": buckets[0]["start"], "end": buckets[0]["start"] + timedelta(milliseconds=BUCKET_MS), "power_w": 3900}]
    item = {"power_w": 2000, "duration_min": 30}
    placement = find_best_placement(buckets, item, 4000, points, 0, others)
    assert placement.index != 0


def test_schedule_proposals_returns_null_start_when_item_cannot_fit():
    watts = [500, 500]
    buckets = mk_buckets(watts)
    points = mk_points(watts)
    items = [{"device_name": "d", "program_name": "p", "duration_min": 150, "power_w": 2000}]
    proposals = schedule_proposals(buckets, items, 4000, points, 0)
    assert proposals[0]["start"] is None


def test_instant_deficit_wh_catches_a_spike_that_outpaces_the_ramping_forecast():
    # Real bug in the JS reference: a candidate whose 30-min bucket average balances out can still
    # have its brief high-power phase land well before the forecast curve actually reaches that
    # power level. instant_deficit_wh must prefer the later, truly-covered candidate.
    points = [
        {"time": t(9, 30), "w": 1000},
        {"time": t(10, 0), "w": 2500},
        {"time": t(10, 30), "w": 2600},
        {"time": t(11, 0), "w": 2600},
    ]
    profile = [
        {"minutes": 20, "power_w": 100},
        {"minutes": 20, "power_w": 2200},
        {"minutes": 20, "power_w": 100},
    ]

    start_early = t(9, 30)
    end_early = start_early + timedelta(minutes=60)
    deficit_early = instant_deficit_wh(phase_segments({"profile": profile, "start": start_early, "end": end_early}), [], points, 0, start_early, end_early)
    assert deficit_early > 0, "the 2200 W spike at 9:50-10:10 outpaces the forecast still ramping from 1000 to 2500 W"

    start_later = t(10, 0)
    end_later = start_later + timedelta(minutes=60)
    deficit_later = instant_deficit_wh(phase_segments({"profile": profile, "start": start_later, "end": end_later}), [], points, 0, start_later, end_later)
    assert deficit_later == 0, "delaying 30 min moves the spike to 10:20-10:40, where the forecast has already caught up"

    buckets = [{"start": t(9, 30)}, {"start": t(10, 0)}, {"start": t(10, 30)}]
    item = {"power_w": 800, "profile": profile, "duration_min": 60}
    placement = find_best_placement(buckets, item, 4000, points, 0, [])
    assert placement.index == 1, "expected the 10:00 start (fully covered), not the earlier 9:30 one"
    assert placement.coverage_pct >= 100


def test_coverage_percent_reports_the_ratio_at_a_single_flat_active_bucket():
    start = t(10, 0)
    end = t(11, 0)
    points = [{"time": start, "w": 1000}, {"time": end, "w": 1000}]
    profile = [{"minutes": 60, "power_w": 2000}]
    segments = phase_segments({"profile": profile, "start": start, "end": end})
    assert coverage_percent(segments, [], points, 0, start, end) == 50


def test_coverage_percent_weighs_deficit_by_energy_share():
    # The spike's 500 Wh shortfall against 733.3 Wh total need drives the score to ~32%, not masked
    # by the later abundant margin.
    start = t(10, 0)
    end = t(11, 0)
    points = [
        {"time": t(10, 0), "w": 500},
        {"time": t(10, 19), "w": 500},
        {"time": t(10, 20), "w": 6000},
        {"time": t(11, 0), "w": 6000},
    ]
    profile = [
        {"minutes": 20, "power_w": 2000},
        {"minutes": 40, "power_w": 100},
    ]
    segments = phase_segments({"profile": profile, "start": start, "end": end})
    pct = coverage_percent(segments, [], points, 0, start, end)
    assert pct == 32


def test_coverage_percent_exceeds_100_only_when_solar_comfortably_exceeds_need():
    start = t(10, 0)
    end = t(11, 0)
    points = [{"time": start, "w": 3000}, {"time": end, "w": 3000}]
    profile = [{"minutes": 60, "power_w": 1000}]
    segments = phase_segments({"profile": profile, "start": start, "end": end})
    assert coverage_percent(segments, [], points, 0, start, end) == 300


def test_coverage_percent_sums_every_concurrent_other():
    # Real bug in the JS reference: powerAt() used a "first match" lookup instead of summing every
    # concurrent segment, silently dropping all but one concurrent item's power.
    start = t(11, 0)
    end = t(11, 10)
    points = [{"time": start, "w": 2360}, {"time": end, "w": 2360}]
    profile = [{"minutes": 10, "power_w": 2000}]
    segments = phase_segments({"profile": profile, "start": start, "end": end})
    other_segments = [
        {"start": t(0, 0), "end": t(23, 59), "power": 110},  # always-on base load
        {"start": t(11, 0), "end": t(11, 30), "power": 1100},  # heat pump spike, concurrent
    ]
    pct = coverage_percent(segments, other_segments, points, 0, start, end)
    # (2360 - 110 - 1100) / 2000 = 57.5%, rounds down to 57 on floating-point imprecision — not
    # ~112% ((2360-110)/2000), which is what dropping the heat pump's power would have given.
    assert pct == 57


def test_find_best_placement_candidate_grid_matches_drag_snap_ms():
    points = [
        {"time": t(10, 0), "w": 1500},
        {"time": t(10, 30), "w": 2100},
        {"time": t(11, 0), "w": 2050},
        {"time": t(11, 30), "w": 1700},
    ]
    profile = [
        {"minutes": 10, "power_w": 100},
        {"minutes": 10, "power_w": 2200},
        {"minutes": 10, "power_w": 100},
    ]
    item = {"power_w": 800, "profile": profile, "duration_min": 30}

    coarse_buckets = [{"start": t(10, 0)}, {"start": t(10, 30)}, {"start": t(11, 0)}, {"start": t(11, 30)}]
    coarse_placement = find_best_placement(coarse_buckets, item, 4000, points, 0, [])
    assert coarse_placement.index == 1, "expected the 30-min grid's best available pick to be 10:30"

    fine_buckets = [{"start": t(10, 0) + timedelta(minutes=m)} for m in range(0, 91, 5)]
    fine_placement = find_best_placement(fine_buckets, item, 4000, points, 0, [])
    fine_start = fine_buckets[fine_placement.index]["start"]
    assert (fine_start.hour, fine_start.minute) == (10, 20)
    # Unrounded ratio, not coverage_pct — both round to "95%" despite one being genuinely better.
    assert fine_placement.ratio > coarse_placement.ratio


def test_find_best_placement_span_derived_from_bucket_spacing():
    buckets = [{"start": DAY + timedelta(milliseconds=i * DRAG_SNAP_MS)} for i in range(3)]
    points = [{"time": DAY, "w": 3000}, {"time": DAY + timedelta(milliseconds=3 * DRAG_SNAP_MS), "w": 3000}]
    item = {"power_w": 500, "duration_min": 20}
    placement = find_best_placement(buckets, item, 4000, points, 0, [])
    assert placement is None, "a 20-min item cannot fit in a 15-min search window"


def test_instant_deficit_wh_subtracts_concurrent_others_power():
    points = [{"time": t(9, 0), "w": 2000}, {"time": t(11, 0), "w": 2000}]
    start = t(9, 0)
    end = t(10, 0)
    item_segments = phase_segments({"power_w": 1500, "start": start, "end": end})

    without_others = instant_deficit_wh(item_segments, [], points, 0, start, end)
    assert without_others == 0, "2000 W available comfortably covers a flat 1500 W load alone"

    other_segments = [{"start": t(9, 0), "end": t(10, 0), "power": 1000}]
    with_others = instant_deficit_wh(item_segments, other_segments, points, 0, start, end)
    assert with_others > 0, "1000 W already spoken for leaves only 1000 W, short of the 1500 W load"


def test_find_peak_conflicts_does_not_flag_phases_that_share_a_bucket_but_never_overlap():
    washer = {
        "device_name": "washer",
        "start": datetime(2026, 8, 23, 13, 12, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 23, 15, 42, tzinfo=timezone.utc),
        "profile": [
            {"minutes": 20, "power_w": 150},
            {"minutes": 20, "power_w": 2200},
            {"minutes": 110, "power_w": 150},
        ],
    }
    dishwasher = {
        "device_name": "dishwasher",
        "start": datetime(2026, 8, 23, 13, 22, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 23, 15, 22, tzinfo=timezone.utc),
        "profile": [
            {"minutes": 32, "power_w": 100},
            {"minutes": 8, "power_w": 2000},
            {"minutes": 75, "power_w": 100},
            {"minutes": 5, "power_w": 2000},
        ],
    }
    conflicted = find_peak_conflicts([washer, dishwasher], 4000)
    assert len(conflicted) == 0


def test_find_peak_conflicts_still_flags_a_genuine_simultaneous_overlap():
    washer = {
        "device_name": "washer",
        "start": datetime(2026, 8, 23, 13, 12, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 23, 15, 42, tzinfo=timezone.utc),
        "profile": [
            {"minutes": 20, "power_w": 150},
            {"minutes": 20, "power_w": 2200},
            {"minutes": 110, "power_w": 150},
        ],
    }
    dishwasher = {
        "device_name": "dishwasher",
        "start": datetime(2026, 8, 23, 13, 12, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 23, 15, 12, tzinfo=timezone.utc),
        "profile": [
            {"minutes": 32, "power_w": 100},
            {"minutes": 8, "power_w": 2000},
            {"minutes": 75, "power_w": 100},
            {"minutes": 5, "power_w": 2000},
        ],
    }
    conflicted = find_peak_conflicts([washer, dishwasher], 4000)
    assert len(conflicted) == 2


def test_find_best_placement_does_not_falsely_reject_bucket_sharing_non_overlapping_peaks():
    anchor = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    buckets = [{"start": anchor + timedelta(milliseconds=i * BUCKET_MS)} for i in range(8)]
    points = [{"time": anchor + timedelta(milliseconds=i * BUCKET_MS), "w": 3000} for i in range(9)]
    committed = {
        "start": anchor,
        "end": anchor + timedelta(minutes=120),
        "power_w": 500,
        "profile": [
            {"minutes": 27, "power_w": 0},
            {"minutes": 5, "power_w": 2500},
            {"minutes": 88, "power_w": 0},
        ],
    }
    item = {
        "duration_min": 120,
        "power_w": 500,
        "profile": [
            {"minutes": 28, "power_w": 100},
            {"minutes": 10, "power_w": 1600},
            {"minutes": 82, "power_w": 100},
        ],
    }
    placement = find_best_placement(buckets, item, 4000, points, 0, [committed])
    assert placement is not None
    assert placement.index == 1
    assert placement.coverage_pct >= 100


def test_snap_to_grid_rounds_to_nearest_5_minute_mark():
    base = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    assert snap_to_grid(base + timedelta(minutes=2)) == base
    assert snap_to_grid(base + timedelta(minutes=3)) == base + timedelta(minutes=5)
    assert snap_to_grid(base - timedelta(minutes=3)) == base - timedelta(minutes=5)
    assert snap_to_grid(base + timedelta(minutes=7), DRAG_SNAP_MS) == base + timedelta(minutes=5)


def test_detect_runs_reports_one_simple_run():
    anchor = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    samples = [{"time": anchor + timedelta(minutes=i), "value": 100.0} for i in range(4)]
    runs = detect_runs(samples, idle_threshold=10, gap_tolerance_min=3)
    assert len(runs) == 1
    assert runs[0]["duration_min"] == pytest.approx(3.0)
    assert runs[0]["avg_w"] == pytest.approx(100.0)


def test_detect_runs_merges_two_runs_within_gap_tolerance():
    anchor = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    samples = [
        {"time": anchor, "value": 100.0},
        {"time": anchor + timedelta(minutes=1), "value": 100.0},
        {"time": anchor + timedelta(minutes=1, seconds=30), "value": 0.0},  # idle sample closes run 1
        {"time": anchor + timedelta(minutes=3), "value": 200.0},  # 2 min gap, within the 3 min tolerance
        {"time": anchor + timedelta(minutes=4), "value": 200.0},
    ]
    runs = detect_runs(samples, idle_threshold=10, gap_tolerance_min=3)
    assert len(runs) == 1
    assert runs[0]["duration_min"] == pytest.approx(4.0)
    assert runs[0]["avg_w"] == pytest.approx(75.0)
