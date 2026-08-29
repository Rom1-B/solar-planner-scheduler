import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { getCardClass } from "./dom-shim.js";

await import("../solar-planner-card.js");
const Card = getCardClass("solar-planner-card");

function pad(n) {
  return String(n).padStart(2, "0");
}

// Matches the card's own fmtHaDatetime — local time, not toISOString() (which is UTC and would
// silently shift when parsed back by the card's local-time parser).
function fmtLocal(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
}

function buildForecast(dayStart, peakKw = 3) {
  const detailedForecast = [];
  for (let h = 6; h <= 20; h++) {
    for (const m of [0, 30]) {
      const t = new Date(dayStart);
      t.setHours(h, m, 0, 0);
      const sunFactor = Math.max(0, Math.sin(((h + m / 60 - 6) / 14) * Math.PI));
      detailedForecast.push({ period_start: t.toISOString(), pv_estimate: sunFactor * peakKw });
    }
  }
  return detailedForecast;
}

// PAC's start_time is computed relative to the real current time (below), not hardcoded — same
// reasoning as slotStart: a fixed "13:00" eventually falls outside the forecast's 6h-20h daylight
// window during a long session (see buildForecast above).
function makeConfig(pacStartTime) {
  return {
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    devices: [
      {
        name: "Lave-linge",
        power_sensor: "sensor.ll_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 120, power_w: 1800 }] }],
      },
      {
        name: "Lave-vaisselle",
        power_sensor: "sensor.lv_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 90, power_w: 1200 }] }],
      },
    ],
    fixed_loads: [{ name: "PAC", start_time: pacStartTime, power_profile: [{ minutes: 60, power_w: 1500 }] }],
  };
}

// state_entity applies to every device at once — there's no per-device wiring anymore, so a card
// is either "all confirmed selections" (state_entity set) or "all preview ghosts" (unset).
function buildCard({ wired }) {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);

  const pacStart = new Date(Date.now() + 20 * 60000);
  const baseConfig = makeConfig(`${pad(pacStart.getHours())}:${pad(pacStart.getMinutes())}`);

  const card = new Card();
  card.setConfig(wired ? { ...baseConfig, state_entity: "input_text.solar_planner_state" } : baseConfig);

  card._estimates.set("sensor.ll_power::Eco", { powerW: 1800 });
  card._estimates.set("sensor.lv_power::Eco", { powerW: 1200 });

  // Anchored to the real current time (+10 min), not a fixed hour-of-day — a hardcoded hour
  // eventually falls outside the forecast's 6h-20h daylight window during a long test/dev session
  // (see buildForecast above). A slot in the past relative to "now" is exercised separately below.
  const slotStart = new Date(Date.now() + 10 * 60000);
  const sharedState = { "Lave-linge": { duration: 120, start: fmtLocal(slotStart) } };

  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.ll_power": { state: "0" },
      "sensor.lv_power": { state: "0" },
      ...(wired ? { "input_text.solar_planner_state": { state: JSON.stringify(sharedState) } } : {}),
    },
  };
  return card;
}

function rectsWithClass(html, cls) {
  const rectRe = /<rect ([^>]*)\/>/g;
  const rects = [];
  let match;
  while ((match = rectRe.exec(html))) {
    const classMatch = /class="([^"]*)"/.exec(match[1]);
    if (classMatch && classMatch[1].split(" ").includes(cls)) rects.push(match[1]);
  }
  return rects;
}

function allStackRects(html) {
  return [...rectsWithClass(html, "stack-confirmed"), ...rectsWithClass(html, "stack-ghost"), ...rectsWithClass(html, "stack-fixed")];
}

test("stacked consumption renders valid, non-negative rect geometry", () => {
  const card = buildCard({ wired: true });
  card._render();
  const rects = allStackRects(card.shadowRoot.innerHTML);
  assert.ok(rects.length > 0, "expected at least one stacked rect");
  for (const attrs of rects) {
    const w = parseFloat(/width="([^"]*)"/.exec(attrs)?.[1] ?? "NaN");
    const h = parseFloat(/height="([^"]*)"/.exec(attrs)?.[1] ?? "NaN");
    const y = parseFloat(/ y="([^"]*)"/.exec(attrs)?.[1] ?? "NaN");
    assert.ok(!Number.isNaN(w) && w >= 0, `bad width in: ${attrs}`);
    assert.ok(!Number.isNaN(h) && h >= 0, `bad height in: ${attrs}`);
    assert.ok(!Number.isNaN(y), `bad y in: ${attrs}`);
  }
});

test("with state_entity set, an active selection renders as a confirmed segment", () => {
  const card = buildCard({ wired: true });
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(rectsWithClass(html, "stack-confirmed").length > 0, "expected a confirmed segment for Lave-linge's active selection");
  assert.ok(rectsWithClass(html, "stack-fixed").length > 0, "expected a fixed-load segment");
});

test("power labels show total energy (Wh/kWh), not an uninformative average watt figure", () => {
  const card = buildCard({ wired: true });
  card._render();
  const html = card.shadowRoot.innerHTML;
  // Lave-linge's estimate has no per-phase profile (buildCard sets only powerW) — 120 min @ 1800 W ->
  // 3600 Wh (3.6 kWh) total, no "peak" (nothing to break down without phases).
  assert.ok(html.includes("3.6 kWh"), "expected the slot-row/gantt label to show total energy, not avg watts");
  // PAC (fixed_loads) always carries its config's own power_profile — 60 min @ 1500 W -> 1500 Wh
  // (1.5 kWh) total, 1.5 kW peak.
  assert.ok(html.includes("1.5 kWh · peak 1.5 kW"), "expected the fixed-load label to show total energy plus peak");
  assert.ok(!html.includes("avg "), 'expected no leftover "avg " power label anywhere in the render');
});

test('the table shows energy in kWh and "-" for a fixed load\'s program column', () => {
  const card = buildCard({ wired: true });
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Energy</th>"), "expected the table header to read Energy, not Power");
  assert.ok(!html.includes("<th>Power</th>"), "expected no leftover Power header");
  // PAC (external, fixed_loads) has no real program of its own — "-" instead of repeating the load's name.
  assert.match(html, /<td>PAC \(external\)<\/td><td>-<\/td>/);
  // 60 min @ 1500 W -> 1.5 kWh, not "1500 W"/"1.5 kW".
  assert.match(html, /<td>PAC \(external\)<\/td><td>-<\/td>\s*<td>[^<]*<\/td>\s*<td>1\.5 kWh<\/td>/);
  // A real device keeps its program name.
  assert.match(html, /<td>Lave-linge<\/td><td>Eco<\/td>/);
});

test("without state_entity, every device renders as a preview ghost", () => {
  const card = buildCard({ wired: false });
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(rectsWithClass(html, "stack-ghost").length > 0, "expected preview (ghost) segments with no state_entity configured");
  assert.equal(rectsWithClass(html, "stack-confirmed").length, 0, "nothing should be confirmed without state_entity");
});

test("a slot scheduled after sunset still renders within the chart (view widens beyond daylight)", () => {
  // The fallback "least-bad slot" placement can push a device past sunset when no full-solar window
  // exists that day. The chart view used to be clamped to the daylight window (+30 min margin), so
  // such a slot had no bucket to render into and no x-coordinate inside the SVG's viewBox — it must
  // widen to include every scheduled item, not just daylight hours.
  const card = buildCard({ wired: true });
  const lateStart = new Date();
  lateStart.setHours(23, 0, 0, 0);
  const sharedState = { "Lave-linge": { duration: 120, start: fmtLocal(lateStart) } };
  card._hass.states["input_text.solar_planner_state"] = { state: JSON.stringify(sharedState) };
  card._render();
  const html = card.shadowRoot.innerHTML;
  // viewBox width scales with the visible span (see "Chart width scaling" in CLAUDE.local.md), not
  // necessarily past the 600 baseline — viewStart is now also floored at "now - 6h" (see "viewStart
  // floor" below), which can make the total span shorter than todayViewSpanMs and width < 600. What
  // matters here is that the post-sunset slot lands inside whatever width was actually rendered.
  const chartWidth = parseFloat(/<svg class="chart" viewBox="0 0 ([\d.]+)/.exec(html)?.[1] ?? "NaN");
  assert.ok(chartWidth > 0, `expected a valid chart width, got ${chartWidth}`);
  const rects = rectsWithClass(html, "stack-confirmed");
  assert.ok(rects.length > 0, "expected the post-sunset slot to render as a confirmed stack segment");
  for (const attrs of rects) {
    const x = parseFloat(/^x="([^"]*)"/.exec(attrs)?.[1] ?? "NaN");
    assert.ok(x >= 0 && x <= chartWidth, `expected rect within the chart's ${chartWidth}-wide viewBox, got x=${x}`);
  }
});

test("a slot with no solar coverage shows a red 0% coverage badge", () => {
  // The warning used to only appear as text when coverage fell short — easy to miss, and absent
  // entirely otherwise. It's now an always-visible badge, colored via coverage-good/coverage-low.
  // A slot scheduled well after sunset has zero forecast surplus for its whole span (0%, coverage-low)
  // — an unambiguous case to assert both the number and the color class render.
  const card = buildCard({ wired: true });
  const lateStart = new Date();
  lateStart.setHours(23, 0, 0, 0);
  const sharedState = { "Lave-linge": { duration: 120, start: fmtLocal(lateStart) } };
  card._hass.states["input_text.solar_planner_state"] = { state: JSON.stringify(sharedState) };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /<span class="coverage-pct coverage-low">0% solar<\/span>/);
});

function ganttBarGeometry(html, deviceName) {
  const re = new RegExp(`<rect x="([\\d.]+)"[^>]*width="([\\d.]+)"[^>]*data-device="${deviceName}"`);
  const m = re.exec(html);
  return m ? { x: parseFloat(m[1]), width: parseFloat(m[2]) } : null;
}

test("forecast_tomorrow_entity widens the chart without shrinking today's own resolution", () => {
  // The invariant that actually matters (not just that width/width% move together, which is true by
  // construction of the formula regardless of whether the formula itself is correct): a device's
  // gantt bar sitting entirely within today must render at the exact same pixel geometry whether or
  // not forecast_tomorrow_entity is configured — that's "today doesn't shrink," made concrete.
  const withoutTomorrow = buildCard({ wired: true });
  withoutTomorrow._render();
  const barWithout = ganttBarGeometry(withoutTomorrow.shadowRoot.innerHTML, "Lave-linge");
  assert.ok(barWithout, "expected Lave-linge's gantt bar to render without forecast_tomorrow_entity");

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withTomorrow = buildCard({ wired: true });
  withTomorrow._config.forecast_tomorrow_entity = "sensor.forecast_tomorrow";
  withTomorrow._hass.states["sensor.forecast_tomorrow"] = { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } };
  withTomorrow._render();
  const htmlWith = withTomorrow.shadowRoot.innerHTML;
  const barWith = ganttBarGeometry(htmlWith, "Lave-linge");
  assert.ok(barWith, "expected Lave-linge's gantt bar to render with forecast_tomorrow_entity");

  assert.ok(Math.abs(barWith.x - barWithout.x) < 0.1, `expected the same x position, got ${barWith.x} vs ${barWithout.x}`);
  assert.ok(Math.abs(barWith.width - barWithout.width) < 0.1, `expected the same width, got ${barWith.width} vs ${barWithout.width}`);

  const chartWidth = parseFloat(/<svg class="chart" viewBox="0 0 ([\d.]+)/.exec(htmlWith)?.[1] ?? "NaN");
  assert.ok(chartWidth > 600, `expected tomorrow's forecast to widen the view past the 600 baseline, got ${chartWidth}`);
});

test("a daily fixed load also shows tomorrow's occurrence once forecast_tomorrow_entity is set", () => {
  // fixed_loads are daily recurring (start_time repeats every day) — without forecast_tomorrow_entity
  // there's only one occurrence to show (today's). Once the chart extends into tomorrow, tomorrow's
  // recurrence of "PAC" (from makeConfig) must also render, in the same gantt lane/color, not be
  // silently missing from the newly-visible day.
  const withoutTomorrow = buildCard({ wired: true });
  withoutTomorrow._render();
  const fixedRectsWithout = rectsWithClass(withoutTomorrow.shadowRoot.innerHTML, "fixed");
  assert.equal(fixedRectsWithout.length, 1, `expected exactly today's PAC occurrence, got ${fixedRectsWithout.length}`);

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withTomorrow = buildCard({ wired: true });
  withTomorrow._config.forecast_tomorrow_entity = "sensor.forecast_tomorrow";
  withTomorrow._hass.states["sensor.forecast_tomorrow"] = { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } };
  withTomorrow._render();
  const htmlWith = withTomorrow.shadowRoot.innerHTML;
  const fixedRectsWith = rectsWithClass(htmlWith, "fixed");
  assert.equal(fixedRectsWith.length, 2, `expected today's + tomorrow's PAC occurrence, got ${fixedRectsWith.length}`);

  // Both occurrences stay in a single lane (same fixed_loads[] entry), not two separate lanes —
  // laneCount must be keyed by config entry count, not by the number of rendered occurrences.
  const ganttHeight = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(htmlWith)?.[1] ?? "NaN");
  const ganttHeightWithout = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(withoutTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN");
  assert.ok(Math.abs(ganttHeight - ganttHeightWithout) < 0.1, `expected the same lane count/gantt height, got ${ganttHeight} vs ${ganttHeightWithout}`);
});

test("the table marks tomorrow's fixed-load occurrence so it doesn't read as an unexplained duplicate", () => {
  // Both occurrences are real, distinct windows (see the test above) — but the table only ever showed
  // HH:MM, so today's "PAC (external) … 13:00 – 13:30" and tomorrow's own row looked like an exact,
  // unexplained duplicate with no way to tell them apart.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const card = buildCard({ wired: true });
  card._config.forecast_tomorrow_entity = "sensor.forecast_tomorrow";
  card._hass.states["sensor.forecast_tomorrow"] = { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } };
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const rows = [...html.matchAll(/<td>PAC[^<]*<\/td><td>[^<]*<\/td>\s*<td>([^<]*)<\/td>/g)].map((m) => m[1]);
  assert.equal(rows.length, 2, `expected two PAC rows (today + tomorrow), got ${rows.length}`);
  assert.ok(
    rows.some((r) => r.startsWith("Tomorrow ")) && rows.some((r) => !r.startsWith("Tomorrow ")),
    `expected exactly one row marked "Tomorrow ", got: ${JSON.stringify(rows)}`
  );
});

test("a sunnier tomorrow doesn't rescale today's Y-axis", () => {
  // maxW must come from today-only forecast points — if tomorrow's (taller) peak leaked into it, the
  // max_simultaneous_power reference line would shift between these two renders even though nothing
  // about today changed. buildForecast's default 3 kW peak already sits under max_simultaneous_power
  // (4000 W in makeConfig), so the baseline render's maxW is pinned by max_simultaneous_power, not the
  // forecast — tomorrow's 8 kW peak would only move it if the bug were reintroduced.
  const withoutTomorrow = buildCard({ wired: true });
  withoutTomorrow._render();
  const maxLineYWithout = /class="max-line"/.test(withoutTomorrow.shadowRoot.innerHTML)
    ? parseFloat(/y1="([\d.]+)"[^>]*class="max-line"/.exec(withoutTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN")
    : NaN;
  assert.ok(!Number.isNaN(maxLineYWithout), "expected a max-line to render");

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withSunnyTomorrow = buildCard({ wired: true });
  withSunnyTomorrow._config.forecast_tomorrow_entity = "sensor.forecast_tomorrow";
  withSunnyTomorrow._hass.states["sensor.forecast_tomorrow"] = {
    state: "8",
    attributes: { detailedForecast: buildForecast(tomorrowStart, 8) },
  };
  withSunnyTomorrow._render();
  const maxLineYWith = parseFloat(/y1="([\d.]+)"[^>]*class="max-line"/.exec(withSunnyTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN");

  assert.ok(Math.abs(maxLineYWith - maxLineYWithout) < 0.1, `expected the same max-line y, got ${maxLineYWith} vs ${maxLineYWithout}`);
});

test("a full-day fixed load doesn't pull the default view back to midnight", () => {
  // A fixed_load starting at "00:00" with a 1440-min (24h) profile used to pull viewStart all the way
  // back to midnight via the scheduledTimes widening, regardless of the time of day — by evening this
  // pushed "now" (and the rest of the day, which matters more than the far past) off the default,
  // unscrolled view entirely. viewStart is now floored at "now - 6h", so the now-line should sit no
  // more than roughly 6 daylight-hours' worth of pixels from the left margin.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    devices: [
      {
        name: "Lave-linge",
        power_sensor: "sensor.ll_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 120, power_w: 1800 }] }],
      },
    ],
    fixed_loads: [{ name: "Conso de base", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 110 }] }],
  });
  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.ll_power": { state: "0" },
    },
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(nowX), "expected a now-line to render");
  // baseInnerW (548) * 6h / ~15h (buildForecast's 6h-20h window ± 30 min margins) ≈ 219; generous
  // slack above that (not an exact-pixel check) to stay robust to whatever hour tests happen to run at.
  // Note: this only exercises the pastFloor path when it's actually the binding constraint (now - 6h
  // later than daylightStart, i.e. roughly after 11:30 local time) — run before that, daylightStart
  // wins and the assertion holds trivially without the floor doing any work.
  const marginLeft = 44;
  assert.ok(nowX - marginLeft <= 260, `expected "now" within ~6h of the left margin, got ${nowX - marginLeft}px past it`);
  assert.ok(nowX - marginLeft >= 0, `expected "now" at or after the left margin, got ${nowX - marginLeft}`);
});

test("two devices with overlapping peaks each show reduced coverage, not each >100%", () => {
  // coveragePctByDevice used to score each device against the forecast alone, with no otherSegments —
  // two devices whose peaks overlap could each individually read as fully (or more than) covered even
  // though combined they draw more than the forecast, which is exactly backwards: the whole point of
  // the badge is to warn about grid draw, and two "covered" badges hid the exact case that matters.
  // A flat 2000 W forecast, two 1200 W devices fully overlapping: alone each would score
  // 2000/1200 ≈ 167% (coverage-good); with the other device's 1200 W correctly subtracted, each should
  // see only 800 W available for its own 1200 W draw — 800/1200 ≈ 67% (coverage-low).
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const forecastStart = new Date(Date.now() - 60 * 60000);
  const forecastEnd = new Date(Date.now() + 4 * 60 * 60000);
  const detailedForecast = [
    { period_start: forecastStart.toISOString(), pv_estimate: 2 },
    { period_start: forecastEnd.toISOString(), pv_estimate: 2 },
  ];
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "Lave-linge",
        power_sensor: "sensor.ll_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 60, power_w: 1200 }] }],
      },
      {
        name: "Lave-vaisselle",
        power_sensor: "sensor.lv_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 60, power_w: 1200 }] }],
      },
    ],
    fixed_loads: [],
  });
  card._estimates.set("sensor.ll_power::Eco", { powerW: 1200 });
  card._estimates.set("sensor.lv_power::Eco", { powerW: 1200 });
  const slotStart = new Date(Date.now() + 10 * 60000);
  const sharedState = {
    "Lave-linge": { duration: 60, start: fmtLocal(slotStart) },
    "Lave-vaisselle": { duration: 60, start: fmtLocal(slotStart) },
  };
  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "2", attributes: { detailedForecast } },
      "sensor.surplus": { state: "3000" },
      "sensor.ll_power": { state: "0" },
      "sensor.lv_power": { state: "0" },
      "input_text.solar_planner_state": { state: JSON.stringify(sharedState) },
    },
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  const badges = [...html.matchAll(/<span class="coverage-pct ([\w-]+)">(\d+)% solar<\/span>/g)];
  assert.equal(badges.length, 2, `expected a coverage badge for each device, got ${badges.length}`);
  for (const [, cls, pct] of badges) {
    assert.equal(cls, "coverage-low", `expected coverage-low given the overlap, got ${cls} (${pct}%)`);
    assert.equal(pct, "67", `expected 800/1200 ≈ 67%, got ${pct}%`);
  }
});

test("stacked consumption has no implicit base-load layer", () => {
  // Base load is an internal estimate for the scheduling math, not something the user configures —
  // it must not appear as an unexplained gray layer. A real base consumption belongs in fixed_loads.
  const card = buildCard({ wired: true });
  card._render();
  assert.equal(rectsWithClass(card.shadowRoot.innerHTML, "stack-base").length, 0);
});

test("an active selection whose slot already ended still renders in the stack", () => {
  // The stack's own bucket grid must span the full visible daylight window, not the future-only
  // grid used for scheduling (_futureSurplusBuckets) — otherwise a slot that's already over (it's
  // evening, the device ran this morning) has no bucket left to render into and silently vanishes.
  const card = buildCard({ wired: true });
  const pastStart = new Date(Date.now() - 180 * 60000);
  const sharedState = { "Lave-linge": { duration: 120, start: fmtLocal(pastStart) } };
  card._hass.states["input_text.solar_planner_state"] = { state: JSON.stringify(sharedState) };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(rectsWithClass(html, "stack-confirmed").length > 0, "expected the past slot to still render as a confirmed stack segment");
});

test("stacked chart segments render at exact phase-boundary granularity, not a fixed bucket", () => {
  // One segment per exact phase boundary, not a fixed grid — widths must be proportional to duration.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "Lave-vaisselle",
        power_sensor: "sensor.lv_power",
        programs: [
          {
            name: "Eco",
            power_profile: [
              { minutes: 40, power_w: 100 },
              { minutes: 10, power_w: 2000 },
              { minutes: 15, power_w: 100 },
              { minutes: 5, power_w: 2000 },
              { minutes: 20, power_w: 100 },
            ],
          },
        ],
      },
    ],
  });
  const program = card._config.devices[0].programs[0];
  card._estimates.set("sensor.lv_power::Eco", { powerW: 700, profile: program.power_profile, approximate: false });

  const slotStart = new Date(Date.now() + 10 * 60000);
  const sharedState = { "Lave-vaisselle": { duration: program.duration_minutes, start: fmtLocal(slotStart) } };
  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.lv_power": { state: "0" },
      "input_text.solar_planner_state": { state: JSON.stringify(sharedState) },
    },
  };
  card._render();
  const rects = rectsWithClass(card.shadowRoot.innerHTML, "stack-confirmed");
  assert.equal(rects.length, 5, `expected exactly one segment per phase, got ${rects.length}`);
  const widths = rects.map((attrs) => parseFloat(/width="([^"]*)"/.exec(attrs)?.[1] ?? "NaN"));
  assert.ok(widths[3] < widths[0] / 4, `expected the 5-min phase narrower than the 40-min one, got ${widths[3]} vs ${widths[0]}`);
});

test("a short power spike renders at its true peak, not diluted by a bucket average", () => {
  // 3-min, 2000 W phase starting at :22 (not a 5-min multiple) — the old windowPowerAt().mean grid
  // would have diluted it to ~1240 W.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "Lave-vaisselle",
        power_sensor: "sensor.lv_power",
        programs: [
          {
            name: "Eco",
            power_profile: [
              { minutes: 22, power_w: 100 },
              { minutes: 3, power_w: 2000 },
              { minutes: 35, power_w: 100 },
            ],
          },
        ],
      },
    ],
  });
  const program = card._config.devices[0].programs[0];
  card._estimates.set("sensor.lv_power::Eco", { powerW: 700, profile: program.power_profile, approximate: false });

  const slotStart = new Date(Date.now() + 10 * 60000);
  const sharedState = { "Lave-vaisselle": { duration: program.duration_minutes, start: fmtLocal(slotStart) } };
  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.lv_power": { state: "0" },
      "input_text.solar_planner_state": { state: JSON.stringify(sharedState) },
    },
  };
  card._render();
  const rects = rectsWithClass(card.shadowRoot.innerHTML, "stack-confirmed");
  assert.equal(rects.length, 3, `expected exactly one segment per phase, got ${rects.length}`);
  const heights = rects.map((attrs) => parseFloat(/height="([^"]*)"/.exec(attrs)?.[1] ?? "NaN"));
  // True peak is 20x the base phase; a diluted mean would only be ~12.4x.
  assert.ok(heights[1] > heights[0] * 15, `expected the spike's height to reflect its true peak, got ${heights[1]} vs base ${heights[0]}`);
});

test("fixed loads get distinct colors, not a shared gray", () => {
  const card = buildCard({ wired: true });
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    devices: [
      {
        name: "Lave-linge",
        power_sensor: "sensor.ll_power",
        programs: [{ name: "Eco", power_profile: [{ minutes: 120, power_w: 1800 }] }],
      },
    ],
    fixed_loads: [
      { name: "PAC", start_time: "13:00", power_profile: [{ minutes: 60, power_w: 1500 }] },
      { name: "Base conso", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 300 }] },
    ],
  });
  card._render();
  const html = card.shadowRoot.innerHTML;
  const styleMatches = [...html.matchAll(/style="fill:(#[0-9a-fA-F]+)" class="bar fixed"/g)].map((m) => m[1]);
  assert.equal(styleMatches.length, 2, "expected a fill color on each fixed-load bar");
  assert.notEqual(styleMatches[0], styleMatches[1], "the two fixed loads must not share the same color");
});

test('tomorrow is offered and committed when today has no window at all', async () => {
  // "Occupier" is a one-off committed selection (via state_entity), not a fixed_load — it spans all
  // of today at exactly max_simultaneous_power, so any candidate placement for TestDevice fails the
  // peak ceiling everywhere today regardless of what time the test runs, but leaves tomorrow free
  // (a one-off selection doesn't recur, unlike fixed_loads).
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000);

  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    forecast_tomorrow_entity: "sensor.forecast_tomorrow",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "Occupier",
        power_sensor: "sensor.occ_power",
        programs: [{ name: "AllDay", power_profile: [{ minutes: 1440, power_w: 4000 }] }],
      },
      {
        name: "TestDevice",
        power_sensor: "sensor.td_power",
        programs: [{ name: "Run", power_profile: [{ minutes: 60, power_w: 500 }] }],
      },
    ],
  });
  card._estimates.set("sensor.occ_power::AllDay", { powerW: 4000, profile: [{ minutes: 1440, power_w: 4000 }], approximate: false });
  card._estimates.set("sensor.td_power::Run", { powerW: 500, profile: [{ minutes: 60, power_w: 500 }], approximate: false });

  const sharedState = { Occupier: { duration: 1440, start: fmtLocal(dayStart) } };
  const writes = [];
  card._hass = {
    themes: { darkMode: false },
    callService: async (_domain, _service, data) => {
      writes.push(data);
    },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.forecast_tomorrow": { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.occ_power": { state: "0" },
      "sensor.td_power": { state: "0" },
      "input_text.solar_planner_state": { state: JSON.stringify(sharedState) },
    },
  };

  await card._onSelectProgram("TestDevice", 60);
  assert.ok(card._selectionError, "expected a 'no window today' error");
  assert.equal(card._scheduleChoice?.deviceName, "TestDevice");
  assert.equal(card._scheduleChoice?.todaySlot, null, "every candidate today is blocked by Occupier");
  // coveragePct is uncapped above 100 (the worst instant's ratio, see coveragePercent) — a free day
  // with plenty of headroom reads well above 100, not exactly 100.
  assert.ok(card._scheduleChoice?.tomorrowPct >= 100, `expected tomorrow to never dip below the forecast, got ${card._scheduleChoice?.tomorrowPct}%`);

  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes('class="use-tomorrow-btn"'), "expected a 'Use tomorrow' button to render");
  assert.ok(!html.includes('class="use-today-btn"'), "no today slot exists, so there's nothing to 'Use today'");

  await card._onUseTomorrow("TestDevice");
  assert.equal(card._selectionError, null, "expected the error to clear once tomorrow's slot is committed");
  assert.equal(card._scheduleChoice, null, "expected the choice to clear once accepted");
  assert.equal(writes.length, 1, "expected exactly one write to state_entity");
  const written = JSON.parse(writes[0].value);
  const writtenStart = new Date(written.TestDevice.start.replace(" ", "T"));
  assert.ok(writtenStart.getTime() >= tomorrowStart.getTime(), `expected the committed start to be tomorrow or later, got ${written.TestDevice.start}`);
});

test('a poor (but non-null) solar match today is offered as a choice, not committed silently', async () => {
  // findBestPlacement never fails on a poor solar match — it always returns the least-bad slot (see
  // the scheduling model docs). Real bug this covers: the previous design only compared "found a slot"
  // vs "found nothing," so a cloudy/zero-surplus today with an otherwise perfectly placeable device
  // silently committed today's near-zero-solar slot with no prompt at all — even when tomorrow was
  // clearly sunnier — because nothing ever checked whether a better day existed. The user's own words:
  // "the goal of this algorithm is to find the most interesting moment possible," not just any moment.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000);

  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    forecast_tomorrow_entity: "sensor.forecast_tomorrow",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "TestDevice",
        power_sensor: "sensor.td_power",
        programs: [{ name: "Run", power_profile: [{ minutes: 120, power_w: 1000 }] }],
      },
    ],
  });
  card._estimates.set("sensor.td_power::Run", { powerW: 1000, profile: [{ minutes: 120, power_w: 1000 }], approximate: false });

  const writes = [];
  card._hass = {
    themes: { darkMode: false },
    callService: async (_domain, _service, data) => {
      writes.push(data);
    },
    states: {
      // Flat zero forecast today (peakKw=0) — no solar at all, so any placement has a 100% deficit —
      // against a normally sunny tomorrow (buildForecast's default 3 kW peak), which is unambiguously better.
      "sensor.forecast": { state: "0", attributes: { detailedForecast: buildForecast(dayStart, 0) } },
      "sensor.forecast_tomorrow": { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } },
      "sensor.surplus": { state: "0" },
      "sensor.td_power": { state: "0" },
      "input_text.solar_planner_state": { state: "{}" },
    },
  };

  await card._onSelectProgram("TestDevice", 120);
  assert.ok(card._selectionError?.includes("tomorrow covers"), `expected a comparison message, got: ${card._selectionError}`);
  assert.equal(card._scheduleChoice?.todayPct, 0, "today has zero solar coverage");
  // coveragePct is uncapped above 100 (the worst instant's ratio, see coveragePercent) — a sunny,
  // uncontested tomorrow reads well above 100, not exactly 100.
  assert.ok(card._scheduleChoice?.tomorrowPct >= 100, `expected tomorrow to never dip below the forecast, got ${card._scheduleChoice?.tomorrowPct}%`);
  assert.equal(writes.length, 0, "must not commit either option silently — that's the bug this test catches");

  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes('class="use-today-btn"'), "expected a 'Use today' button since a today slot was found");
  assert.ok(html.includes('class="use-tomorrow-btn"'), "expected a 'Use tomorrow' button since it's better");

  // The comparison is informative, not prescriptive — the user can still pick the worse option.
  await card._onUseToday("TestDevice");
  assert.equal(card._selectionError, null, "expected the warning to clear once the user accepts today's slot");
  assert.equal(card._scheduleChoice, null, "expected the choice to clear once accepted");
  assert.equal(writes.length, 1, "expected exactly one write to state_entity");
  const written = JSON.parse(writes[0].value);
  const writtenStart = new Date(written.TestDevice.start.replace(" ", "T"));
  assert.ok(writtenStart.getTime() < tomorrowStart.getTime(), `expected the committed start to stay today, got ${written.TestDevice.start}`);
});

test("a poor today's slot is committed silently when tomorrow isn't actually better", async () => {
  // Counterpart to the comparison test above: tomorrow must be genuinely better to interrupt at all —
  // if it's equally bad (both flat zero forecast here), there's nothing more to offer, so the least-bad
  // slot found is used exactly like before this feature existed. Prevents nagging when there's no
  // actionable improvement available on either day.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000);

  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    forecast_tomorrow_entity: "sensor.forecast_tomorrow",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "TestDevice",
        power_sensor: "sensor.td_power",
        programs: [{ name: "Run", power_profile: [{ minutes: 120, power_w: 1000 }] }],
      },
    ],
  });
  card._estimates.set("sensor.td_power::Run", { powerW: 1000, profile: [{ minutes: 120, power_w: 1000 }], approximate: false });

  const writes = [];
  card._hass = {
    themes: { darkMode: false },
    callService: async (_domain, _service, data) => {
      writes.push(data);
    },
    states: {
      "sensor.forecast": { state: "0", attributes: { detailedForecast: buildForecast(dayStart, 0) } },
      "sensor.forecast_tomorrow": { state: "0", attributes: { detailedForecast: buildForecast(tomorrowStart, 0) } },
      "sensor.surplus": { state: "0" },
      "sensor.td_power": { state: "0" },
      "input_text.solar_planner_state": { state: "{}" },
    },
  };

  await card._onSelectProgram("TestDevice", 120);
  assert.equal(card._selectionError, null, "expected no warning — nothing better is available to offer");
  assert.equal(card._scheduleChoice, null, "expected no schedule choice");
  assert.equal(writes.length, 1, "expected the poor-but-best-available slot to be committed directly");
  const written = JSON.parse(writes[0].value);
  const writtenStart = new Date(written.TestDevice.start.replace(" ", "T"));
  assert.ok(writtenStart.getTime() < tomorrowStart.getTime(), `expected the committed start to stay today, got ${written.TestDevice.start}`);
});

test("Dismiss clears the comparison prompt without committing or touching the previous window", async () => {
  // Real gap: _selectionError/_scheduleChoice only ever cleared on a commit or a "None" selection —
  // _onRecalculate's "Keeping the previous window" message was literally true (nothing is written),
  // but there was no way to close the warning banner short of clicking "Use today"/"Use tomorrow",
  // both of which change the schedule. Dismiss must clear the banner and write nothing.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    forecast_tomorrow_entity: "sensor.forecast_tomorrow",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      {
        name: "TestDevice",
        power_sensor: "sensor.td_power",
        programs: [{ name: "Run", power_profile: [{ minutes: 120, power_w: 1000 }] }],
      },
    ],
  });
  card._estimates.set("sensor.td_power::Run", { powerW: 1000, profile: [{ minutes: 120, power_w: 1000 }], approximate: false });

  const writes = [];
  card._hass = {
    themes: { darkMode: false },
    callService: async (_domain, _service, data) => {
      writes.push(data);
    },
    states: {
      "sensor.forecast": { state: "0", attributes: { detailedForecast: buildForecast(dayStart, 0) } },
      "sensor.forecast_tomorrow": { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } },
      "sensor.surplus": { state: "0" },
      "sensor.td_power": { state: "0" },
      "input_text.solar_planner_state": { state: "{}" },
    },
  };

  await card._onSelectProgram("TestDevice", 120);
  assert.ok(card._selectionError, "expected the deficit warning to be set up first");
  assert.ok(card._scheduleChoice, "expected a schedule choice to be set up first");

  card._dismissSelectionError();
  assert.equal(card._selectionError, null, "expected Dismiss to clear the warning");
  assert.equal(card._scheduleChoice, null, "expected Dismiss to clear the choice");
  assert.equal(writes.length, 0, "Dismiss must not write anything");

  card._render();
  assert.ok(!card.shadowRoot.innerHTML.includes('class="warnings"'), "expected the warning banner to be gone after Dismiss");
});

test("gantt markup includes a hidden live-percentage label for drag feedback", () => {
  // _bindGanttDrag can't be exercised here (dom-shim's querySelector/querySelectorAll are stubs, no
  // real pointer events) — this only guards the static markup _bindGanttDrag depends on: a single
  // drag-pct-group (shared across bars, only one drag happens at a time), starting hidden, plus a
  // draggable class on the confirmed bar it's meant to follow.
  const card = buildCard({ wired: true });
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("bar-draggable"), "expected the confirmed bar to be draggable");
  assert.match(html, /<g class="drag-pct-group" style="opacity:0"/, "expected the live-% group to start hidden");
  assert.ok(html.includes('class="drag-pct-bg"'), "expected a background for the live-% label");
  assert.ok(html.includes('class="drag-pct"'), "expected the live-% text element");
});

test("stack order mirrors the gantt's top-to-bottom config order, not reversed", () => {
  // Gantt lane 0 (top) is the first device in config; the stack used to put that same device at the
  // bottom instead, so top-of-graph matched bottom-of-gantt.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    surplus_entity: "sensor.surplus",
    max_simultaneous_power: 4000,
    state_entity: "input_text.solar_planner_state",
    devices: [
      { name: "A", power_sensor: "sensor.a_power", programs: [{ name: "Run", power_profile: [{ minutes: 60, power_w: 500 }] }] },
      { name: "B", power_sensor: "sensor.b_power", programs: [{ name: "Run", power_profile: [{ minutes: 60, power_w: 800 }] }] },
    ],
  });
  card._estimates.set("sensor.a_power::Run", { powerW: 500, profile: [{ minutes: 60, power_w: 500 }], approximate: false });
  card._estimates.set("sensor.b_power::Run", { powerW: 800, profile: [{ minutes: 60, power_w: 800 }], approximate: false });

  const slotStart = new Date(Date.now() + 10 * 60000);
  const sharedState = {
    A: { duration: 60, start: fmtLocal(slotStart) },
    B: { duration: 60, start: fmtLocal(slotStart) },
  };
  card._hass = {
    themes: { darkMode: false },
    states: {
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      "sensor.surplus": { state: "500" },
      "sensor.a_power": { state: "0" },
      "sensor.b_power": { state: "0" },
      "input_text.solar_planner_state": { state: JSON.stringify(sharedState) },
    },
  };
  card._render();
  const html = card.shadowRoot.innerHTML;

  const colorA = /style="fill:(#[0-9a-fA-F]+)"[^>]*data-device="A"/.exec(html)?.[1];
  const colorB = /style="fill:(#[0-9a-fA-F]+)"[^>]*data-device="B"/.exec(html)?.[1];
  assert.ok(colorA && colorB && colorA !== colorB, "expected distinct colors for A and B's gantt bars");

  const stackRects = rectsWithClass(html, "stack-confirmed");
  const rectA = stackRects.find((attrs) => attrs.includes(`fill:${colorA}`));
  const rectB = stackRects.find((attrs) => attrs.includes(`fill:${colorB}`));
  assert.ok(rectA && rectB, "expected a stacked segment for each device");

  const yA = parseFloat(/ y="([^"]*)"/.exec(rectA)?.[1] ?? "NaN");
  const yB = parseFloat(/ y="([^"]*)"/.exec(rectB)?.[1] ?? "NaN");
  assert.ok(yA < yB, `expected A (first in config, gantt's top lane) drawn above B in the stack, got yA=${yA} vs yB=${yB}`);
});
