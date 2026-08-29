import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { smoothCurve } from "../solar-planner-card.js";

test("smoothCurve returns nothing for an empty input", () => {
  const rangeStart = new Date("2026-08-23T00:00:00Z");
  const rangeEnd = new Date("2026-08-23T01:00:00Z");
  assert.deepEqual(smoothCurve([], 15 * 60000, rangeStart, rangeEnd), []);
});

test("smoothCurve produces one time-weighted point per bucket, not a raw passthrough", () => {
  const rangeStart = new Date("2026-08-23T00:00:00Z");
  const rangeEnd = new Date("2026-08-23T00:30:00Z");
  // 6 raw, event-driven samples inside a single 15-min bucket — should collapse to 1 point.
  const points = [
    { time: new Date("2026-08-23T00:00:00Z"), value: 100 },
    { time: new Date("2026-08-23T00:02:00Z"), value: 200 },
    { time: new Date("2026-08-23T00:05:00Z"), value: 100 },
    { time: new Date("2026-08-23T00:09:00Z"), value: 300 },
    { time: new Date("2026-08-23T00:12:00Z"), value: 100 },
    { time: new Date("2026-08-23T00:20:00Z"), value: 500 },
  ];
  const smoothed = smoothCurve(points, 15 * 60000, rangeStart, rangeEnd);
  assert.equal(smoothed.length, 2);
  assert.equal(smoothed[0].time.getTime(), rangeStart.getTime());
  // Time-weighted, not a naive mean of the raw values: 100 held for 2min, 200 for 3min, 100 for
  // 4min, 300 for 3min, 100 for 3min (bucket ends at 15min) = (100*2+200*3+100*4+300*3+100*3)/15.
  assert.equal(smoothed[0].value, (100 * 2 + 200 * 3 + 100 * 4 + 300 * 3 + 100 * 3) / 15);
  assert.equal(smoothed[1].time.getTime(), new Date("2026-08-23T00:15:00Z").getTime());
  // The 00:12 sample (value 100) still holds from 00:15 to 00:20, when the 500 sample takes over.
  assert.equal(smoothed[1].value, (100 * 5 + 500 * 10) / 15);
});

test("smoothCurve skips buckets with no data instead of inventing a value", () => {
  const rangeStart = new Date("2026-08-23T00:00:00Z");
  const rangeEnd = new Date("2026-08-23T01:00:00Z");
  const points = [{ time: new Date("2026-08-23T00:45:00Z"), value: 42 }];
  const smoothed = smoothCurve(points, 15 * 60000, rangeStart, rangeEnd);
  assert.equal(smoothed.length, 1);
  assert.equal(smoothed[0].time.getTime(), new Date("2026-08-23T00:45:00Z").getTime());
  assert.equal(smoothed[0].value, 42);
});
