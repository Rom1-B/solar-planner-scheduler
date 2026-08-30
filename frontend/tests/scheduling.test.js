import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { DRAG_SNAP_MS, instantDeficitWh, coveragePercent, phaseSegments, snapToGrid } from "../solar-planner-card.js";

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

test("snapToGrid rounds a dragged timestamp to the nearest 5-minute mark", () => {
  const base = new Date("2026-01-15T10:00:00Z").getTime();
  assert.equal(snapToGrid(base + 2 * 60000), base);
  assert.equal(snapToGrid(base + 3 * 60000), base + 5 * 60000);
  assert.equal(snapToGrid(base - 3 * 60000), base - 5 * 60000);
  assert.equal(snapToGrid(base + 7 * 60000, DRAG_SNAP_MS), base + 5 * 60000);
});
