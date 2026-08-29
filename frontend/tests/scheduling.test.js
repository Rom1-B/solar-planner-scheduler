import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BUCKET_MS,
  DRAG_SNAP_MS,
  findBestPlacement,
  scheduleProposals,
  findPeakConflicts,
  instantDeficitWh,
  coveragePercent,
  phaseSegments,
  snapToGrid,
} from "../solar-planner-card.js";

function mkBuckets(watts, startHour = 8) {
  const day = new Date("2026-01-15T00:00:00Z");
  return watts.map((_, i) => ({ start: new Date(day.getTime() + (startHour * 2 + i) * BUCKET_MS) }));
}

// Same time grid as mkBuckets, as a forecast curve (interpolate() linearly interpolates between
// these) — findBestPlacement now queries this directly instead of a pre-baked per-bucket value.
function mkPoints(watts, startHour = 8) {
  const day = new Date("2026-01-15T00:00:00Z");
  return watts.map((w, i) => ({ time: new Date(day.getTime() + (startHour * 2 + i) * BUCKET_MS), w }));
}

test("sunny day: earliest perfect-fit slot wins", () => {
  const watts = [100, 100, 3000, 3000, 3000, 3000, 100, 100];
  const buckets = mkBuckets(watts);
  const points = mkPoints(watts);
  const item = { powerW: 2000, durationMin: 60 };
  const placement = findBestPlacement(buckets, item, 4000, points, 0, []);
  assert.equal(placement.index, 2);
  assert.equal(placement.coveragePct, 150);
});

test("winter day: no perfect fit exists, least-bad slot is returned instead of null", () => {
  // Under instant (not bucket-mean) scoring, the interpolated curve ramps between values instead of
  // stepping — the true best-worst-instant 2-bucket window straddles the 400/500 rise and 500/450
  // fall (index 3: buckets at 400 and 500), not just the single 500 bucket's own pair.
  const watts = [100, 150, 300, 400, 500, 450, 300, 150];
  const buckets = mkBuckets(watts);
  const points = mkPoints(watts);
  const item = { powerW: 2000, durationMin: 60 };
  const placement = findBestPlacement(buckets, item, 10000, points, 0, []);
  assert.ok(placement !== null);
  assert.equal(placement.index, 3);
  assert.ok(placement.coveragePct < 100, "winter day never fully covers a 2000 W hour");
});

test("findBestPlacement prefers the candidate that draws less real energy from the grid", () => {
  // A: brief severe dip. B: evenly ~90% all hour. A draws less total energy from the grid, so it scores higher.
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const points = [
    { time: t(10, 0), w: 1200 },
    { time: t(10, 24), w: 1200 },
    { time: t(10, 25), w: 200 }, // a brief severe dip inside candidate A's window
    { time: t(10, 30), w: 200 },
    { time: t(10, 31), w: 1200 },
    { time: t(11, 0), w: 1200 },
    { time: t(12, 0), w: 900 }, // candidate B: flat, evenly under the 1000 W item the whole hour
    { time: t(13, 0), w: 900 },
  ];
  const item = { powerW: 1000, durationMin: 60 };
  const buckets = [{ start: t(10, 0) }, { start: t(12, 0) }];
  const placement = findBestPlacement(buckets, item, 4000, points, 0, []);
  assert.equal(placement.index, 0, "expected candidate A (smaller real grid draw) over candidate B (evenly ~90% but more total Wh)");
  assert.equal(placement.coveragePct, 93);
});

test("max_simultaneous_power stays a hard filter even over a zero-deficit candidate", () => {
  const watts = [3000, 3000, 3000, 3000];
  const buckets = mkBuckets(watts);
  const points = mkPoints(watts);
  const others = [{ start: buckets[0].start, end: new Date(buckets[0].start.getTime() + BUCKET_MS), powerW: 3900 }];
  const item = { powerW: 2000, durationMin: 30 };
  const placement = findBestPlacement(buckets, item, 4000, points, 0, others);
  assert.notEqual(placement.index, 0);
});

test("scheduleProposals returns a null start when the item can't fit in the remaining day at all", () => {
  const watts = [500, 500];
  const buckets = mkBuckets(watts);
  const points = mkPoints(watts);
  const items = [{ deviceName: "d", programName: "p", durationMin: 150, powerW: 2000 }];
  const proposals = scheduleProposals(buckets, items, 4000, points, 0);
  assert.equal(proposals[0].start, null);
});

// Real bug: a candidate whose 30-min bucket *average* balances out can still have its brief
// high-power phase land well before the forecast curve actually reaches that power level — invisible
// to a bucket-mean check, but a real shortfall at that exact moment (and visible on the chart, where
// the profile's spike is drawn above the forecast line). This is what the user hit in practice: the
// scheduler proposed a 9:30 start showing "100% covered," while manually delaying to 10:00 was
// visibly better — instantDeficitWh must prefer the later, truly-covered candidate.
test("instantDeficitWh catches a spike that outpaces the ramping forecast, even when the bucket average would balance out", () => {
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const points = [
    { time: t(9, 30), w: 1000 },
    { time: t(10, 0), w: 2500 },
    { time: t(10, 30), w: 2600 },
    { time: t(11, 0), w: 2600 },
  ];
  const profile = [
    { minutes: 20, power_w: 100 },
    { minutes: 20, power_w: 2200 },
    { minutes: 20, power_w: 100 },
  ];

  const startEarly = t(9, 30);
  const endEarly = new Date(startEarly.getTime() + 60 * 60000);
  const deficitEarly = instantDeficitWh(phaseSegments({ profile, start: startEarly, end: endEarly }), [], points, 0, startEarly, endEarly);
  assert.ok(deficitEarly > 0, "the 2200 W spike at 9:50-10:10 outpaces the forecast still ramping from 1000 to 2500 W");

  const startLater = t(10, 0);
  const endLater = new Date(startLater.getTime() + 60 * 60000);
  const deficitLater = instantDeficitWh(phaseSegments({ profile, start: startLater, end: endLater }), [], points, 0, startLater, endLater);
  assert.equal(deficitLater, 0, "delaying 30 min moves the spike to 10:20-10:40, where the forecast has already caught up");

  // findBestPlacement must actually prefer the later, truly-covered start over the earlier one.
  const buckets = [t(9, 30), t(10, 0), t(10, 30)].map((start) => ({ start }));
  const item = { powerW: 800, profile, durationMin: 60 };
  const placement = findBestPlacement(buckets, item, 4000, points, 0, []);
  assert.equal(placement.index, 1, "expected the 10:00 start (fully covered), not the earlier 9:30 one");
  assert.ok(placement.coveragePct >= 100, `expected the 10:00 start to never dip below the forecast, got ${placement.coveragePct}%`);
});

test("coveragePercent reports the ratio at a single flat active bucket", () => {
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const start = t(10, 0);
  const end = t(11, 0);
  const points = [
    { time: start, w: 1000 },
    { time: end, w: 1000 },
  ];
  const profile = [{ minutes: 60, power_w: 2000 }];
  const segments = phaseSegments({ profile, start, end });
  assert.equal(coveragePercent(segments, [], points, 0, start, end), 50);
});

test("coveragePercent weighs a real deficit by its energy share, not just its worst raw ratio", () => {
  // The spike's 500 Wh shortfall against 733.3 Wh total need drives the score to ~32%, not masked by later margin.
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const start = t(10, 0);
  const end = t(11, 0);
  const points = [
    { time: t(10, 0), w: 500 }, // well below the 2000 W spike for the first phase
    { time: t(10, 19), w: 500 }, // held flat right up to the phase boundary, no ramp to interpolate through
    { time: t(10, 20), w: 6000 }, // then far more than the item ever draws, also held flat
    { time: t(11, 0), w: 6000 },
  ];
  const profile = [
    { minutes: 20, power_w: 2000 },
    { minutes: 40, power_w: 100 },
  ];
  const segments = phaseSegments({ profile, start, end });
  const pct = coveragePercent(segments, [], points, 0, start, end);
  assert.equal(pct, 32, "expected the spike's deficit weighed against total energy need, not just its raw ratio");
});

test("coveragePercent exceeds 100% only when solar comfortably exceeds the item's need at every instant", () => {
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const start = t(10, 0);
  const end = t(11, 0);
  const points = [
    { time: start, w: 3000 },
    { time: end, w: 3000 },
  ];
  const profile = [{ minutes: 60, power_w: 1000 }];
  const segments = phaseSegments({ profile, start, end });
  assert.equal(coveragePercent(segments, [], points, 0, start, end), 300);
});

test("coveragePercent sums every concurrent other, not just the first one that overlaps", () => {
  // Real bug: powerAt() (shared by instantDeficitWh/coveragePercent) used Array.find(), which returns
  // only the first segment overlapping a given instant — when otherSegments is the concatenation of
  // several *different* items' own segments (routinely true: an always-on base-load fixed load plus
  // another device's spike, say), only whichever happened to sit first in the array got subtracted,
  // silently dropping every other concurrent item's power. Reported by a user with an always-on
  // "Conso de base" fixed load (110 W) listed before a heat pump's 30-min spike (1100 W): a device
  // deliberately overlapping the heat pump's spike still read as ~110% covered (as if only the 110 W
  // base load were concurrent) instead of the true ~57% once both are actually subtracted together.
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const start = t(11, 0);
  const end = t(11, 10);
  const points = [
    { time: start, w: 2360 },
    { time: end, w: 2360 },
  ];
  const profile = [{ minutes: 10, power_w: 2000 }];
  const segments = phaseSegments({ profile, start, end });
  // Base load first in the array (as it is in production: fixedLoads is concatenated after
  // activeSelections, and "Conso de base" is configured before "PAC" in fixed_loads), heat pump second.
  const otherSegments = [
    { start: t(0, 0), end: t(23, 59), power: 110 }, // always-on base load
    { start: t(11, 0), end: t(11, 30), power: 1100 }, // heat pump spike, concurrent with the item
  ];
  const pct = coveragePercent(segments, otherSegments, points, 0, start, end);
  // (2360 - 110 - 1100) / 2000 = 57.5%, rounds down to 57 here on floating-point imprecision — not
  // ~112% ((2360-110)/2000), which is what dropping the heat pump's power would have given.
  assert.equal(pct, 57, `expected both concurrent others subtracted together, got ${pct}%`);
});

// Real bug reported after the fix above shipped: the deficit *scoring* had gone instant/fine-grained,
// but findBestPlacement's candidate *search* was still hardcoded to 30-min steps — a genuinely better
// start sitting strictly between two 30-min marks (a narrow midday peak: forecast rises, peaks
// briefly, then declines, so the spike only clears it for a short window) was never even considered.
// User's exact report: the auto-scheduler proposed one time, but dragging the bar (5-min snap,
// DRAG_SNAP_MS) to a time in between showed better coverage than either 30-min-aligned candidate.
test("findBestPlacement's candidate grid matches DRAG_SNAP_MS, not a hardcoded 30-min step — it must find what a manual drag could", () => {
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const points = [
    { time: t(10, 0), w: 1500 },
    { time: t(10, 30), w: 2100 },
    { time: t(11, 0), w: 2050 },
    { time: t(11, 30), w: 1700 },
  ];
  const profile = [
    { minutes: 10, power_w: 100 },
    { minutes: 10, power_w: 2200 },
    { minutes: 10, power_w: 100 },
  ];
  const item = { powerW: 800, profile, durationMin: 30 };

  // The true optimum (10:20) sits strictly between the two 30-min-grid marks that bracket it —
  // neither 10:00 nor 10:30 is as good, so a 30-min-only search settles for 10:30's worse result.
  const coarseBuckets = [t(10, 0), t(10, 30), t(11, 0), t(11, 30)].map((start) => ({ start }));
  const coarsePlacement = findBestPlacement(coarseBuckets, item, 4000, points, 0, []);
  assert.equal(coarsePlacement.index, 1, "expected the 30-min grid's best available pick to be 10:30");

  const fineBuckets = [];
  for (let m = 0; m <= 90; m += 5) fineBuckets.push({ start: new Date(t(10, 0).getTime() + m * 60000) });
  const finePlacement = findBestPlacement(fineBuckets, item, 4000, points, 0, []);
  const fineStart = fineBuckets[finePlacement.index].start;
  assert.equal(`${fineStart.getUTCHours()}:${String(fineStart.getUTCMinutes()).padStart(2, "0")}`, "10:20");
  // Unrounded ratio, not coveragePct — both round to "95%" despite one being genuinely better.
  assert.ok(
    finePlacement.ratio > coarsePlacement.ratio,
    `expected the 5-min grid to find a strictly better start (${finePlacement.ratio}) than the 30-min grid (${coarsePlacement.ratio})`
  );
});

test("findBestPlacement's span is derived from the buckets' own spacing, not hardcoded to 30 min", () => {
  // A hardcoded `Math.ceil(durationMin / 30)` badly under-counts how many 5-min buckets a 20-min item
  // actually needs (1 instead of 4), so the search loop tries starts near the end of a short buckets
  // array whose real end time is far past what the buckets/points actually cover — interpolate()
  // silently clamps to the last known point there, which can make an out-of-range candidate look
  // spuriously well-covered instead of correctly excluded. With just 3 buckets (15 min of real
  // search window, 5-min spaced) a 20-min item can never actually fit — findBestPlacement must say so.
  const day = new Date("2026-01-15T00:00:00Z");
  const buckets = [0, 1, 2].map((i) => ({ start: new Date(day.getTime() + i * DRAG_SNAP_MS) }));
  const points = [
    { time: new Date(day.getTime()), w: 3000 },
    { time: new Date(day.getTime() + 3 * DRAG_SNAP_MS), w: 3000 },
  ];
  const item = { powerW: 500, durationMin: 20 };
  const placement = findBestPlacement(buckets, item, 4000, points, 0, []);
  assert.equal(placement, null, "a 20-min item cannot fit in a 15-min search window");
});

test("instantDeficitWh subtracts concurrent others' power from available solar, same as fitsPeakCeiling", () => {
  const day = new Date("2026-01-15T00:00:00Z");
  const t = (h, m) => new Date(day.getTime() + (h * 60 + m) * 60000);
  const points = [
    { time: t(9, 0), w: 2000 },
    { time: t(11, 0), w: 2000 },
  ];
  const start = t(9, 0);
  const end = t(10, 0);
  const itemSegments = phaseSegments({ powerW: 1500, start, end });

  const withoutOthers = instantDeficitWh(itemSegments, [], points, 0, start, end);
  assert.equal(withoutOthers, 0, "2000 W available comfortably covers a flat 1500 W load alone");

  const otherSegments = [{ start: t(9, 0), end: t(10, 0), power: 1000 }];
  const withOthers = instantDeficitWh(itemSegments, otherSegments, points, 0, start, end);
  assert.ok(withOthers > 0, "1000 W already spoken for by another item leaves only 1000 W, short of the 1500 W load");
});

// Real case: a washing machine's 2200 W heating phase (15:12-15:32) and a dishwasher's 2000 W
// heating phase (15:54-16:02) both touch the same 30-min bucket (15:30-16:00) but are 22 minutes
// apart in wall-clock time — a bucket-quantized peak check summed them into a false "exceeds 4kW".
test("findPeakConflicts does not flag phases that share a bucket but never truly overlap", () => {
  const washer = {
    deviceName: "washer",
    start: new Date("2026-08-23T13:12:00Z"),
    end: new Date("2026-08-23T15:42:00Z"),
    profile: [
      { minutes: 20, power_w: 150 },
      { minutes: 20, power_w: 2200 },
      { minutes: 110, power_w: 150 },
    ],
  };
  const dishwasher = {
    deviceName: "dishwasher",
    start: new Date("2026-08-23T13:22:00Z"),
    end: new Date("2026-08-23T15:22:00Z"),
    profile: [
      { minutes: 32, power_w: 100 },
      { minutes: 8, power_w: 2000 },
      { minutes: 75, power_w: 100 },
      { minutes: 5, power_w: 2000 },
    ],
  };
  const conflicted = findPeakConflicts([washer, dishwasher], 4000);
  assert.equal(conflicted.size, 0);
});

test("findPeakConflicts still flags a genuine simultaneous overlap", () => {
  const washer = {
    deviceName: "washer",
    start: new Date("2026-08-23T13:12:00Z"),
    end: new Date("2026-08-23T15:42:00Z"),
    profile: [
      { minutes: 20, power_w: 150 },
      { minutes: 20, power_w: 2200 },
      { minutes: 110, power_w: 150 },
    ],
  };
  const dishwasher = {
    deviceName: "dishwasher",
    start: new Date("2026-08-23T13:12:00Z"),
    end: new Date("2026-08-23T15:12:00Z"),
    profile: [
      { minutes: 32, power_w: 100 },
      { minutes: 8, power_w: 2000 },
      { minutes: 75, power_w: 100 },
      { minutes: 5, power_w: 2000 },
    ],
  };
  const conflicted = findPeakConflicts([washer, dishwasher], 4000);
  assert.equal(conflicted.size, 2);
});

// findBestPlacement had the same bucket-quantization flaw findPeakConflicts had: an already-committed
// item's 2500 W phase (00:27-00:32) only tails 2 min into bucket [00:30,01:00), and a candidate
// placed at that bucket's start has its own 1600 W phase only heading 2 min into the same bucket
// (00:58-01:00) — 26 minutes apart in wall-clock time, never truly simultaneous (true combined max is
// 2600 W). A bucket-quantized hard filter sums the two bucket-touching peaks (2500+1600=4100) and
// falsely rejects this candidate; starting one bucket earlier (00:00) is a genuine overlap and must
// still be rejected.
test("findBestPlacement does not falsely reject a candidate whose peak only shares a bucket, not real time, with an already-committed item", () => {
  const anchor = new Date("2026-08-23T00:00:00Z");
  const buckets = Array.from({ length: 8 }, (_, i) => ({ start: new Date(anchor.getTime() + i * BUCKET_MS) }));
  const points = Array.from({ length: 9 }, (_, i) => ({ time: new Date(anchor.getTime() + i * BUCKET_MS), w: 3000 }));
  const committed = {
    start: anchor,
    end: new Date(anchor.getTime() + 120 * 60000),
    powerW: 500,
    profile: [
      { minutes: 27, power_w: 0 },
      { minutes: 5, power_w: 2500 },
      { minutes: 88, power_w: 0 },
    ],
  };
  const item = {
    durationMin: 120,
    powerW: 500,
    profile: [
      { minutes: 28, power_w: 100 },
      { minutes: 10, power_w: 1600 },
      { minutes: 82, power_w: 100 },
    ],
  };
  const placement = findBestPlacement(buckets, item, 4000, points, 0, [committed]);
  assert.ok(placement !== null);
  assert.equal(placement.index, 1);
  assert.ok(placement.coveragePct >= 100, `expected the candidate to never dip below the forecast, got ${placement.coveragePct}%`);
});

test("snapToGrid rounds a dragged timestamp to the nearest 5-minute mark", () => {
  const base = new Date("2026-01-15T10:00:00Z").getTime();
  assert.equal(snapToGrid(base + 2 * 60000), base);
  assert.equal(snapToGrid(base + 3 * 60000), base + 5 * 60000);
  assert.equal(snapToGrid(base - 3 * 60000), base - 5 * 60000);
  assert.equal(snapToGrid(base + 7 * 60000, DRAG_SNAP_MS), base + 5 * 60000);
});
