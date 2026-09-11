import "./dom-shim.js";
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { getCardClass } from "./dom-shim.js";

await import("../solar-planner-card.js");
const Card = getCardClass("solar-planner-card");

function pad(n) {
  return String(n).padStart(2, "0");
}

// Pins `new Date()`/`Date.now()` to a fixed hour:minute (today) for the duration of `fn`, restoring
// the real Date constructor afterward even if `fn` throws. `new Date(x)` with explicit args (used
// throughout the card for arithmetic) is left untouched.
function withFixedNow(hours, minutes, fn) {
  const fixed = new Date();
  fixed.setHours(hours, minutes, 0, 0);
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...args) {
      super(...(args.length === 0 ? [fixed.getTime()] : args));
    }
    static now() {
      return fixed.getTime();
    }
  }
  global.Date = FixedDate;
  try {
    return fn(fixed);
  } finally {
    global.Date = RealDate;
  }
}

// The card reads forecast/max_simultaneous_power from this sensor's attributes instead of its own
// config (production_entity/consumption_entity are card config, see setConfig()): spread into
// every test's `states` object.
const BASE_CONFIG_ENTITY = {
  "sensor.solar_planner_scheduler_config": {
    state: "4000",
    attributes: {
      forecast_entity: "sensor.forecast",
      forecast_tomorrow_entity: null,
      fixed_loads: [],
      devices: [],
      theoretical_forecast: [],
    },
  },
};

// theoretical_forecast is server-normalized (coordinator.py's theoretical_forecast_points()):
// {time, w, w10, w90}, regardless of which forecast provider produced it. Overrides (not merges)
// the config sensor's theoretical_forecast attribute — use addForecastPoints() to append instead
// (e.g. a "tomorrow" entity's points on top of today's).
function configEntityWithForecast(points) {
  const base = BASE_CONFIG_ENTITY["sensor.solar_planner_scheduler_config"];
  return { "sensor.solar_planner_scheduler_config": { ...base, attributes: { ...base.attributes, theoretical_forecast: points } } };
}

function setForecastPoints(card, points) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, theoretical_forecast: points },
  };
}

function addForecastPoints(card, points) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  const merged = [...(entity.attributes.theoretical_forecast || []), ...points].sort((a, b) => new Date(a.time) - new Date(b.time));
  setForecastPoints(card, merged);
}

// One program named "Eco" per device slug, with the program's own row-slug equal to the device
// slug: matches the real integration when a device has exactly one program, and keeps this
// suite's entity_ids (datetime.<slug>_start etc.) unchanged from before per-program rows existed.
// `names` overrides the display name per slug (defaults to the slug itself), matching
// sensor.py's real "name" field (the device's configured CONF_NAME, not its entity_id slug).
function singleProgramDevices(slugs, { programName = "Eco", names = {} } = {}) {
  return slugs.map((slug) => ({ name: names[slug] ?? slug, slug, programs: [{ name: programName, slug }] }));
}

// Overwrites the config sensor's devices attribute: call after setConfig({devices: [...]}) with
// a matching list of slugs, or _programRows() finds nothing to render.
function setDevicesAttr(card, devices) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, devices },
  };
}

// Card no longer reads forecast_tomorrow_entity from its own config, flips it on in the shared
// config sensor's attributes instead.
function enableTomorrowForecast(card) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, forecast_tomorrow_entity: "sensor.forecast_tomorrow" },
  };
}

// Card no longer reads fixed_loads from its own config either, set them on the shared config
// sensor's attributes instead. Each load is {name, start_time, power_profile}, matching what
// BaseConfigSensor exposes (already wrapped as a single-phase profile server-side).
function setFixedLoads(card, fixedLoads) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, fixed_loads: fixedLoads },
  };
}

// {provider: entity_id}, matches resolve_forecast_history_entities()'s server-side shape.
function setForecastHistoryEntities(card, entities) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, forecast_history_entities: entities },
  };
}

// Server-normalized shape (coordinator.py's theoretical_forecast_points()): {time, w, w10, w90},
// watts already in W. w10/w90 default to w (no confidence band) unless withConfidence is set.
function buildForecast(dayStart, peakKw = 3, withConfidence = false) {
  const points = [];
  for (let h = 6; h <= 20; h++) {
    for (const m of [0, 30]) {
      const t = new Date(dayStart);
      t.setHours(h, m, 0, 0);
      const sunFactor = Math.max(0, Math.sin(((h + m / 60 - 6) / 14) * Math.PI));
      const w = sunFactor * peakKw * 1000;
      points.push({ time: t.toISOString(), w, w10: withConfidence ? w * 0.7 : w, w90: withConfidence ? w * 1.3 : w });
    }
  }
  return points;
}

// The 3 entities solar_planner_scheduler exposes for one program row, matching what
// _readProgramState reads.
function deviceEntities(
  slug,
  {
    name = slug,
    active = true,
    start = null,
    end = null,
    coveragePct = null,
    powerW = null,
    profile = null,
    shouldRun = false,
    locked = false,
    estimatedCost = null,
    estimatedSavings = null,
    currency = null,
  } = {}
) {
  return {
    [`datetime.${slug}_start`]: {
      state: start ? start.toISOString() : "unknown",
      attributes: {
        friendly_name: `${name} start`,
        coverage_pct: coveragePct,
        end: end ? end.toISOString() : null,
        power_w: powerW,
        profile,
        locked,
        estimated_cost: estimatedCost,
        estimated_savings: estimatedSavings,
        currency,
      },
    },
    [`binary_sensor.${slug}_should_run`]: { state: shouldRun ? "on" : "off" },
    [`switch.${slug}_active`]: { state: active ? "on" : "off" },
  };
}

function baseConfig() {
  return { devices: ["lave_linge", "lave_vaisselle"] };
}

// Two devices (Lave-linge 120min@1800W, Lave-vaisselle 90min@1200W) plus one PAC fixed load:
// the baseline most tests build on. withActiveSelections=false gives both programs an inactive switch.
// PAC's start_time is computed relative to the real current time, not hardcoded, since a fixed "13:00"
// eventually falls outside the forecast's 6h-20h daylight window during a long test/dev session.
function buildCard({ withActiveSelections = true } = {}) {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const pacStart = new Date(Date.now() + 20 * 60000);
  const pacStartTime = `${pad(pacStart.getHours())}:${pad(pacStart.getMinutes())}`;
  const card = new Card();
  card.setConfig(baseConfig());

  const slotStart = new Date(Date.now() + 10 * 60000);

  card._hass = {
    themes: { darkMode: false },
    callService: async () => {},
    callWS: async () => ({}),
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities(
        "lave_linge",
        withActiveSelections
          ? { name: "Lave-linge", start: slotStart, end: new Date(slotStart.getTime() + 120 * 60000), powerW: 1800, coveragePct: 100 }
          : { name: "Lave-linge", active: false }
      ),
      ...deviceEntities(
        "lave_vaisselle",
        withActiveSelections
          ? { name: "Lave-vaisselle", start: slotStart, end: new Date(slotStart.getTime() + 90 * 60000), powerW: 1200, coveragePct: 100 }
          : { name: "Lave-vaisselle", active: false }
      ),
    },
  };
  setDevicesAttr(
    card,
    singleProgramDevices(["lave_linge", "lave_vaisselle"], { names: { lave_linge: "Lave-linge", lave_vaisselle: "Lave-vaisselle" } })
  );
  setFixedLoads(card, [{ name: "PAC", start_time: pacStartTime, power_profile: [{ minutes: 60, power_w: 1500 }] }]);
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

test("stacked consumption renders valid, non-negative rect geometry", () => {
  const card = buildCard();
  card._render();
  const rects = [...rectsWithClass(card.shadowRoot.innerHTML, "stack-confirmed"), ...rectsWithClass(card.shadowRoot.innerHTML, "stack-fixed")];
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

test("a device with a start/end from its sensor renders as a confirmed stack segment", () => {
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(rectsWithClass(html, "stack-confirmed").length > 0, "expected a confirmed segment for Lave-linge");
  assert.ok(rectsWithClass(html, "stack-fixed").length > 0, "expected a fixed-load segment");
});

test("power labels show total energy (Wh/kWh), not an uninformative average watt figure", () => {
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  // Lave-linge: 120 min @ 1800 W -> 3600 Wh (3.6 kWh) total, no "peak" (no profile to break down).
  assert.ok(html.includes("3.6 kWh"), "expected the slot-row/gantt label to show total energy, not avg watts");
  // PAC (fixed_loads) always carries its config's own power_profile: 60 min @ 1500 W -> 1500 Wh
  // (1.5 kWh) total, 1.5 kW peak.
  assert.ok(html.includes("1.5 kWh · peak 1.5 kW"), "expected the fixed-load label to show total energy plus peak");
});

test("the table shows energy in kWh, merging the Device and Program columns", () => {
  const card = buildCard();
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Energy</th>"), "expected the table header to read Energy, not Power");
  assert.ok(!html.includes("<th>Power</th>"), "expected no leftover Power header");
  assert.ok(!html.includes("<th>Program</th>"), "expected the Program column merged into Device");
  assert.match(html, /<td>PAC \(external\)<\/td>\s*<td>[\s\S]*?<\/td>\s*<td>1\.5 kWh<\/td>/);
  assert.match(html, /<td>Lave-linge - Eco<\/td>/);
});

test("a table row is italicized once its window's start has passed", () => {
  const card = buildCard();
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() - 10 * 60000),
      end: new Date(Date.now() + 110 * 60000),
      powerW: 1800,
    }),
  };
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /<tr class="row-started">\s*<td>Lave-linge - Eco<\/td>/, "expected the started row to carry row-started");
  assert.match(html, /<tr class="">\s*<td>Lave-vaisselle - Eco<\/td>/, "expected the not-yet-started row to stay unmarked");
});

test("the table's Cost column shows estimated_cost when present, \"-\" otherwise", () => {
  const card = buildCard();
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      estimatedCost: 0.44,
      currency: "EUR",
    }),
  };
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Cost</th>"), "expected a Cost column header");
  assert.ok(html.includes("~0.44 EUR"), "expected the estimated cost in the table row");
  assert.match(html, /<td>PAC \(external\)<\/td>.*?<td>-<\/td>\s*<\/tr>/s, "expected \"-\" for a fixed load with no estimated_cost");
});

test("the table's Cost column shows a fixed load's estimated_cost when the server provides one", () => {
  const card = buildCard();
  setFixedLoads(card, [
    {
      name: "PAC",
      start_time: "13:00",
      power_profile: [{ minutes: 60, power_w: 1500 }],
      estimated_cost: 0.28,
      currency: "EUR",
    },
  ]);
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /<td>PAC \(external\)<\/td>\s*<td>[\s\S]*?<\/td>\s*<td>[^<]*<\/td>\s*<td>~0.28 EUR<\/td>/);
});

test("an inactive program renders no gantt bar or stack segment", () => {
  const card = buildCard({ withActiveSelections: false });
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.equal(rectsWithClass(html, "stack-confirmed").length, 0, "nothing should be scheduled without an active program");
  assert.ok(html.includes('class="program-toggle "'), "expected an inactive toggle button to render");
});

test("a slot scheduled after sunset still renders within the chart's fixed display window", () => {
  // The fallback "least-bad slot" placement can push a device past sunset when no full-solar window
  // exists that day. The default 6h-past/24h-future window comfortably covers a same-day slot from
  // any time of day, so it must still render, not be clipped.
  const card = buildCard({ withActiveSelections: false });
  const lateStart = new Date();
  lateStart.setHours(23, 0, 0, 0);
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: lateStart,
      end: new Date(lateStart.getTime() + 120 * 60000),
      powerW: 1800,
      coveragePct: 0,
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
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
  const card = buildCard({ withActiveSelections: false });
  const lateStart = new Date();
  lateStart.setHours(23, 0, 0, 0);
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: lateStart,
      end: new Date(lateStart.getTime() + 120 * 60000),
      powerW: 1800,
      coveragePct: 0,
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /<span class="coverage-pct coverage-low">0% solar<\/span>/);
});

function ganttBarGeometry(html, slug) {
  const re = new RegExp(`<rect x="([\\d.]+)"[^>]*width="([\\d.]+)"[^>]*data-device="${slug}"`);
  const m = re.exec(html);
  return m ? { x: parseFloat(m[1]), width: parseFloat(m[2]) } : null;
}

test("forecast_tomorrow_entity does not change the fixed display window or today's bar geometry", () => {
  const withoutTomorrow = buildCard();
  withoutTomorrow._render();
  const barWithout = ganttBarGeometry(withoutTomorrow.shadowRoot.innerHTML, "lave_linge");
  assert.ok(barWithout, "expected Lave-linge's gantt bar to render without forecast_tomorrow_entity");

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withTomorrow = buildCard();
  enableTomorrowForecast(withTomorrow);
  addForecastPoints(withTomorrow, buildForecast(tomorrowStart));
  withTomorrow._render();
  const htmlWith = withTomorrow.shadowRoot.innerHTML;
  const barWith = ganttBarGeometry(htmlWith, "lave_linge");
  assert.ok(barWith, "expected Lave-linge's gantt bar to render with forecast_tomorrow_entity");

  assert.ok(Math.abs(barWith.x - barWithout.x) < 0.1, `expected the same x position, got ${barWith.x} vs ${barWithout.x}`);
  assert.ok(Math.abs(barWith.width - barWithout.width) < 0.1, `expected the same width, got ${barWith.width} vs ${barWithout.width}`);

  const chartWidth = parseFloat(/<svg class="chart" viewBox="0 0 ([\d.]+)/.exec(htmlWith)?.[1] ?? "NaN");
  assert.equal(chartWidth, 600, `expected the chart to keep the fixed 600 width, got ${chartWidth}`);
});

test("chart_hours_past/chart_hours_future control the visible window (now-line position)", () => {
  const card = buildCard();
  card._config.chart_hours_past = 2;
  card._config.chart_hours_future = 6;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/class="now-line"/.test(html) && /x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1]);
  const marginLeft = 28;
  const innerW = 600 - marginLeft - 4;
  const expectedNowX = marginLeft + (2 / (2 + 6)) * innerW;
  assert.ok(Math.abs(nowX - expectedNowX) < 0.5, `expected now-line at ${expectedNowX}, got ${nowX}`);
});

test("chart_visible_hours narrower than the data window widens the chart past 100% (scroll enabled)", () => {
  const card = buildCard();
  card._config.chart_hours_past = 6;
  card._config.chart_hours_future = 24;
  card._config.chart_visible_hours = 15;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const widthPercent = parseFloat(/<svg class="chart" viewBox="0 0 [\d.]+ [\d.]+"[^>]*style="width: ([\d.]+)%/.exec(html)?.[1] ?? "NaN");
  assert.equal(widthPercent, 200, `expected 200% width for a 30h window shown over 15 visible hours, got ${widthPercent}`);
});

test("chart_visible_hours left unset defaults to the full data window (no scroll)", () => {
  const card = buildCard();
  card._config.chart_hours_past = 6;
  card._config.chart_hours_future = 24;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const widthPercent = parseFloat(/<svg class="chart" viewBox="0 0 [\d.]+ [\d.]+"[^>]*style="width: ([\d.]+)%/.exec(html)?.[1] ?? "NaN");
  assert.equal(widthPercent, 100, `expected the default to keep 100% width (no scroll), got ${widthPercent}`);
});

test("chart_visible_hours wider than the data window never shrinks the chart below 100%", () => {
  const card = buildCard();
  card._config.chart_hours_past = 6;
  card._config.chart_hours_future = 24;
  card._config.chart_visible_hours = 48;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const widthPercent = parseFloat(/<svg class="chart" viewBox="0 0 [\d.]+ [\d.]+"[^>]*style="width: ([\d.]+)%/.exec(html)?.[1] ?? "NaN");
  assert.equal(widthPercent, 100, `expected the width to clamp at 100%, got ${widthPercent}`);
});

test("hour-tick density follows chart_visible_hours, not the full scrollable span", () => {
  // A wide chart_hours_future with a narrow chart_visible_hours used to space ticks by the full
  // span/8 (e.g. one tick every ~19h for a 148h span), leaving a single 24h scroll position with
  // at most one readable tick. Density must instead track the visible window alone.
  const card = buildCard();
  card._config.chart_hours_past = 4;
  card._config.chart_hours_future = 144;
  card._config.chart_visible_hours = 24;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const hourValues = [...html.matchAll(/class="axis-label" text-anchor="middle">(\d+)h</g)].map((m) => Number(m[1]));
  assert.ok(hourValues.length >= 6, `expected several hour ticks across the 148h span, got ${hourValues.length}`);
  // chart_visible_hours/8 = 3h apart (round(24/8)), not the ~19h step a full-148h-span/8 formula would give.
  const steps = hourValues.slice(1).map((h, i) => (h - hourValues[i] + 24) % 24);
  assert.ok(steps.every((s) => s === 3), `expected every consecutive tick 3h apart, got steps ${JSON.stringify(steps)}`);
});

test("a chart_hours_future spanning several midnights labels each day boundary distinctly", () => {
  // 6h past (default) + 48h future always spans at least 2 midnights regardless of the current
  // time of day, so this exercises the duplicate-"Tomorrow" bug deterministically.
  const card = buildCard();
  card._config.chart_hours_future = 48;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const labels = [...html.matchAll(/class="axis-label day-label">([^<]*)</g)].map((m) => m[1]);
  assert.ok(labels.length >= 2, `expected at least two day boundaries with a 48h future window, got ${JSON.stringify(labels)}`);
  assert.equal(labels[0], "Tomorrow", `expected the first boundary labeled Tomorrow, got ${labels[0]}`);
  assert.equal(new Set(labels).size, labels.length, `expected every day boundary to have a distinct label, got ${JSON.stringify(labels)}`);
});

test("a day boundary past tomorrow is labeled with its weekday name, not 'In N days'", () => {
  const card = buildCard();
  card._config.chart_hours_future = 48;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const labels = [...html.matchAll(/class="axis-label day-label">([^<]*)</g)].map((m) => m[1]);
  assert.ok(labels.length >= 2, `expected at least two day boundaries with a 48h future window, got ${JSON.stringify(labels)}`);
  const dayAfterTomorrow = new Date();
  dayAfterTomorrow.setHours(0, 0, 0, 0);
  dayAfterTomorrow.setDate(dayAfterTomorrow.getDate() + 2);
  const expectedWeekday = dayAfterTomorrow.toLocaleDateString("en-US", { weekday: "long" });
  assert.equal(labels[1], expectedWeekday, `expected the second boundary labeled with its weekday name, got ${labels[1]}`);
});

// A fixed load recurs every calendar day the chart_hours_past/chart_hours_future window touches,
// independent of forecast_tomorrow_entity (that field only controls forecast *data*, not which days
// the gantt/table cover). Mirrors _visibleDayOffsets()'s own formula rather than hardcoding a day
// count, since the exact count shifts with the real current time of day.
function expectedVisibleDayCount(chartHoursPast, chartHoursFuture) {
  const now = new Date();
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const viewStart = new Date(now.getTime() - chartHoursPast * 3600000);
  const viewEnd = new Date(now.getTime() + chartHoursFuture * 3600000);
  const firstOffset = Math.floor((viewStart.getTime() - today.getTime()) / (24 * 3600000));
  const lastOffset = Math.floor((viewEnd.getTime() - today.getTime()) / (24 * 3600000));
  return lastOffset - firstOffset + 1;
}

test("a daily fixed load's occurrence count follows chart_hours_future, not forecast_tomorrow_entity", () => {
  const narrow = buildCard();
  narrow._config.chart_hours_past = 0;
  narrow._config.chart_hours_future = 1;
  narrow._render();
  const fixedRectsNarrow = rectsWithClass(narrow.shadowRoot.innerHTML, "fixed");
  assert.equal(
    fixedRectsNarrow.length,
    expectedVisibleDayCount(0, 1),
    `expected one PAC occurrence per visible day, got ${fixedRectsNarrow.length}`
  );

  const wide = buildCard();
  wide._config.chart_hours_past = 6;
  wide._config.chart_hours_future = 48;
  wide._render();
  const htmlWide = wide.shadowRoot.innerHTML;
  const fixedRectsWide = rectsWithClass(htmlWide, "fixed");
  assert.equal(
    fixedRectsWide.length,
    expectedVisibleDayCount(6, 48),
    `expected one PAC occurrence per visible day, got ${fixedRectsWide.length}`
  );
  assert.ok(fixedRectsWide.length > fixedRectsNarrow.length, "expected more PAC occurrences with a wider future window");

  // Enabling forecast_tomorrow_entity must not change how many days the fixed load renders on.
  const wideWithForecast = buildCard();
  wideWithForecast._config.chart_hours_past = 6;
  wideWithForecast._config.chart_hours_future = 48;
  enableTomorrowForecast(wideWithForecast);
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);
  addForecastPoints(wideWithForecast, buildForecast(tomorrowStart));
  wideWithForecast._render();
  const fixedRectsWideWithForecast = rectsWithClass(wideWithForecast.shadowRoot.innerHTML, "fixed");
  assert.equal(
    fixedRectsWideWithForecast.length,
    fixedRectsWide.length,
    "expected forecast_tomorrow_entity to not affect the fixed-load occurrence count"
  );

  const ganttHeight = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(htmlWide)?.[1] ?? "NaN");
  const ganttHeightNarrow = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(narrow.shadowRoot.innerHTML)?.[1] ?? "NaN");
  assert.ok(Math.abs(ganttHeight - ganttHeightNarrow) < 0.1, `expected the same lane count/gantt height, got ${ganttHeight} vs ${ganttHeightNarrow}`);
});

test("the table marks tomorrow's fixed-load occurrence so it doesn't read as an unexplained duplicate", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const card = buildCard();
  enableTomorrowForecast(card);
  addForecastPoints(card, buildForecast(tomorrowStart));
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const rows = [...html.matchAll(/<td>PAC[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
  assert.equal(rows.length, 2, `expected two PAC rows (today + tomorrow), got ${rows.length}`);
  assert.ok(
    rows.some((r) => r.startsWith("Tomorrow ")) && rows.some((r) => !r.startsWith("Tomorrow ")),
    `expected exactly one row marked "Tomorrow ", got: ${JSON.stringify(rows)}`
  );
});

test("a daily fixed load still shows exactly today+tomorrow just after midnight, not a stale yesterday row too", () => {
  // Regression for a real CI failure: chart_hours_past (default 6) reaches into "yesterday" for any
  // real "now" before 6am, and _visibleDayOffsets() counts that as a 3rd visible calendar day. Pinning
  // "now" to 2:30am reproduces that exact edge deterministically, instead of depending on when CI runs.
  withFixedNow(2, 30, () => {
    const card = buildCard();
    card._showTable = true;
    card._render();
    const html = card.shadowRoot.innerHTML;
    const rows = [...html.matchAll(/<td>PAC[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
    assert.equal(rows.length, 2, `expected exactly two PAC rows (today + tomorrow), got ${rows.length}: ${JSON.stringify(rows)}`);
  });
});

test("a fixed load that already finished earlier today still shows as recent history", () => {
  // Regression for a live report right after the fix above first shipped: an earlier, wrong
  // attempt filtered by "already elapsed relative to now" instead of "before chart_hours_past's
  // cutoff", which also hid an occurrence that ran earlier today, not just yesterday's stale one.
  withFixedNow(14, 0, () => {
    const card = new Card();
    card.setConfig({});
    card._hass = { themes: { darkMode: false }, states: { ...BASE_CONFIG_ENTITY } };
    setDevicesAttr(card, []);
    setFixedLoads(card, [{ name: "PAC", start_time: "08:00", power_profile: [{ minutes: 30, power_w: 1500 }] }]);
    card._showTable = true;
    card._render();
    const html = card.shadowRoot.innerHTML;
    const rows = [...html.matchAll(/<td>PAC[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
    assert.ok(
      rows.some((r) => r.startsWith("08:00")),
      `expected PAC's already-elapsed-today 08:00 occurrence to still show, got ${JSON.stringify(rows)}`
    );
  });
});

test("the table labels a day boundary past tomorrow with its weekday name, not a flat 'Tomorrow '", () => {
  const card = buildCard();
  card._config.chart_hours_future = 96; // reaches at least day+2 regardless of the current time of day
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  const rows = [...html.matchAll(/<td>PAC[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
  assert.ok(rows.length >= 3, `expected at least 3 PAC rows across a 96h future window, got ${JSON.stringify(rows)}`);
  assert.ok(rows.some((r) => r.startsWith("Tomorrow ")), `expected one row marked "Tomorrow ", got ${JSON.stringify(rows)}`);
  const dayAfterTomorrow = new Date();
  dayAfterTomorrow.setHours(0, 0, 0, 0);
  dayAfterTomorrow.setDate(dayAfterTomorrow.getDate() + 2);
  const expectedWeekday = dayAfterTomorrow.toLocaleDateString("en-US", { weekday: "long" });
  assert.ok(
    rows.some((r) => r.startsWith(`${expectedWeekday} `)),
    `expected a row labeled with ${expectedWeekday}, got ${JSON.stringify(rows)}`
  );
});

test("a sunnier tomorrow doesn't rescale today's Y-axis", () => {
  const withoutTomorrow = buildCard();
  withoutTomorrow._render();
  const maxLineYWithout = parseFloat(/y1="([\d.]+)"[^>]*class="max-line"/.exec(withoutTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(maxLineYWithout), "expected a max-line to render");

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withSunnyTomorrow = buildCard();
  enableTomorrowForecast(withSunnyTomorrow);
  addForecastPoints(withSunnyTomorrow, buildForecast(tomorrowStart, 8));
  withSunnyTomorrow._render();
  const maxLineYWith = parseFloat(/y1="([\d.]+)"[^>]*class="max-line"/.exec(withSunnyTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN");

  assert.ok(Math.abs(maxLineYWith - maxLineYWithout) < 0.1, `expected the same max-line y, got ${maxLineYWith} vs ${maxLineYWithout}`);
});

test("a full-day fixed load doesn't pull the default view back to midnight", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({ devices: ["lave_linge"] });
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_linge", { name: "Lave-linge", active: false }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_linge"]));
  setFixedLoads(card, [{ name: "Conso de base", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 110 }] }]);
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(nowX), "expected a now-line to render");
  const marginLeft = 28;
  assert.ok(nowX - marginLeft <= 260, `expected "now" within ~6h of the left margin, got ${nowX - marginLeft}px past it`);
  assert.ok(nowX - marginLeft >= 0, `expected "now" at or after the left margin, got ${nowX - marginLeft}`);
});

test("setConfig accepts an omitted devices array for a forecast-only card", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  assert.doesNotThrow(() => card.setConfig({}));
  card._hass = {
    themes: { darkMode: false },
    states: { ...BASE_CONFIG_ENTITY, ...configEntityWithForecast(buildForecast(dayStart)) },
  };
  setDevicesAttr(card, []);
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(nowX), "expected the forecast chart to render even with no devices");
});

test('devices: "*" renders every device the server reports, without listing them by hand', () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  assert.doesNotThrow(() => card.setConfig({ devices: "*" }));
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_linge", { name: "Lave-linge" }),
      ...deviceEntities("lave_vaisselle", { name: "Lave-vaisselle" }),
    },
  };
  setDevicesAttr(
    card,
    singleProgramDevices(["lave_linge", "lave_vaisselle"], { names: { lave_linge: "Lave-linge", lave_vaisselle: "Lave-vaisselle" } })
  );
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /Lave-linge/);
  assert.match(html, /Lave-vaisselle/);
});

test('devices: "*" picks up a device added later server-side with no card config change', () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({ devices: "*" });
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_linge", { name: "Lave-linge" }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_linge"], { names: { lave_linge: "Lave-linge" } }));
  card._render();
  assert.match(card.shadowRoot.innerHTML, /Lave-linge/);
  assert.doesNotMatch(card.shadowRoot.innerHTML, /Ballon/);

  card._hass.states = { ...card._hass.states, ...deviceEntities("ballon", { name: "Ballon" }) };
  setDevicesAttr(card, singleProgramDevices(["lave_linge", "ballon"], { names: { lave_linge: "Lave-linge", ballon: "Ballon" } }));
  card._render();
  assert.match(card.shadowRoot.innerHTML, /Ballon/, "expected the newly-added device to appear with no config change");
});

test('setConfig rejects a devices value that is neither an array nor "*"', () => {
  const card = new Card();
  assert.throws(() => card.setConfig({ devices: "lave_linge" }), /must be an array of device slugs, or "\*"/);
});

test("the forecast line starts at now, not at viewStart, when chart_hours_past reaches into days with no forecast data", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({}); // forecast-only card, same real-world config that surfaced this gap
  card._hass = {
    themes: { darkMode: false },
    states: { ...BASE_CONFIG_ENTITY, ...configEntityWithForecast(buildForecast(dayStart)) },
  };
  setDevicesAttr(card, []);
  card._config = { ...card._config, chart_hours_past: 48, chart_hours_future: 0 };
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  const forecastStartX = parseFloat(/<path d="M([\d.]+),[\d.]+[^"]*" class="forecast-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(nowX) && !Number.isNaN(forecastStartX), "expected both a now-line and a forecast-line");
  assert.ok(Math.abs(forecastStartX - nowX) < 0.5, `expected the forecast line to start at "now" (${nowX}), got ${forecastStartX}`);
});

test("the forecast line extends before now once forecast history points are available", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({});
  card._hass = {
    themes: { darkMode: false },
    states: { ...BASE_CONFIG_ENTITY, ...configEntityWithForecast(buildForecast(dayStart)) },
  };
  setDevicesAttr(card, []);
  card._forecastHistoryPoints = [{ time: new Date(Date.now() - 2 * 3600000), w: 50, w10: 50, w90: 50 }];
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  const forecastStartX = parseFloat(/<path d="M([\d.]+),[\d.]+[^"]*" class="forecast-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(forecastStartX < nowX - 1, `expected the forecast line to start before "now" (${nowX}), got ${forecastStartX}`);
});

test("_refresh fetches and stores the active provider's forecast history", async () => {
  // "average"/"min" resolve server-side to this entry's own AverageForecastPowerNowSensor/
  // MinForecastPowerNowSensor (coordinator.py's resolve_forecast_history_entities()) now, a plain
  // recorded entity exactly like a raw provider's own "power now" one: the card just fetches
  // whichever single entity forecast_history_entities[activeProvider] points to, no client-side
  // combining left to test here (see scheduling.test.js's Python-mirrored combine tests instead,
  // now gone from this file since coordinator.py owns that math unconditionally).
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const twoHoursAgo = new Date(Date.now() - 2 * 3600000);
  const card = new Card();
  card.setConfig({});
  card._hass = {
    themes: { darkMode: false },
    callWS: async ({ entity_ids }) => ({ [entity_ids[0]]: [{ last_changed: twoHoursAgo.toISOString(), state: "200" }] }),
    states: { ...BASE_CONFIG_ENTITY, ...configEntityWithForecast(buildForecast(dayStart)) },
  };
  setDevicesAttr(card, []);
  setForecastHistoryEntities(card, { average: "sensor.solar_planner_scheduler_forecast_average_power_now" });
  card._hass.states["select.solar_planner_scheduler_forecast_source"] = {
    state: "Average",
    attributes: { options: ["Solcast", "Helios Forecast", "Average"], provider: "average" },
  };

  await card._refresh();

  // smoothCurve() forward-fills the single raw sample into every 5-minute bucket since it, not a
  // 1:1 passthrough of raw samples: every bucket must show the fetched value, not just the first.
  assert.ok(card._forecastHistoryPoints.length > 1, JSON.stringify(card._forecastHistoryPoints));
  assert.ok(
    card._forecastHistoryPoints.every((p) => p.w === 200),
    `expected every bucket to show the average sensor's 200W, got ${JSON.stringify(card._forecastHistoryPoints)}`
  );
});

test("_refresh renders the schedule immediately, before the history fetches resolve", async () => {
  // Regression: _refresh() used to await Promise.all(jobs) before ever calling _render(), so the
  // card stayed blank (first load) or stale (periodic refresh) for however long the 3 history
  // round trips take, even though the schedule/theoretical forecast come straight from hass.states
  // and need no fetch at all.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig(baseConfig());
  let resolveWS;
  card._hass = {
    themes: { darkMode: false },
    callWS: () => new Promise((resolve) => (resolveWS = resolve)),
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_linge", {
        name: "Lave-linge",
        start: new Date(Date.now() + 10 * 60000),
        end: new Date(Date.now() + 130 * 60000),
        powerW: 1800,
        coveragePct: 67,
      }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_linge"], { names: { lave_linge: "Lave-linge" } }));
  setForecastHistoryEntities(card, { solcast: "sensor.solcast_power_now" });
  card._hass.states["select.solar_planner_scheduler_forecast_source"] = {
    state: "Solcast",
    attributes: { options: ["Solcast"], provider: "solcast" },
  };

  const refreshPromise = card._refresh();
  await new Promise((r) => setTimeout(r, 0)); // let the synchronous render before the awaited fetches land

  assert.match(
    card.shadowRoot.innerHTML,
    /67% solar/,
    "expected the schedule rendered immediately, without waiting on the history fetches"
  );

  resolveWS({});
  await refreshPromise;
});

test("switching the forecast source re-fetches history even within the 5-minute refresh throttle", async () => {
  // Regression for a live bug: set hass() only calls the throttled _refresh() (the sole place that
  // fetches forecast history) when REFRESH_INTERVAL_MS has elapsed; a source switch shortly after
  // page load fell into the else branch (_requestRender() only), leaving _forecastHistoryPoints
  // frozen on whichever provider was active during the last real refresh.
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const oneHourAgo = new Date(Date.now() - 3600000);
  let callCount = 0;
  const callWS = async ({ entity_ids }) => {
    callCount++;
    const value = entity_ids[0] === "sensor.solcast_power_now" ? "100" : "300";
    return { [entity_ids[0]]: [{ last_changed: oneHourAgo.toISOString(), state: value }] };
  };
  const forecastConfigEntity = configEntityWithForecast(buildForecast(dayStart))["sensor.solar_planner_scheduler_config"];
  const statesWithSource = (source, provider) => ({
    ...BASE_CONFIG_ENTITY,
    "sensor.solar_planner_scheduler_config": {
      ...forecastConfigEntity,
      attributes: {
        ...forecastConfigEntity.attributes,
        devices: [],
        forecast_history_entities: { solcast: "sensor.solcast_power_now", helios_forecast: "sensor.helios_power_now" },
      },
    },
    "select.solar_planner_scheduler_forecast_source": {
      state: source,
      attributes: { options: ["Solcast", "Helios Forecast"], provider },
    },
  });

  const card = new Card();
  card.setConfig({});
  card.hass = { themes: { darkMode: false }, callWS, states: statesWithSource("Solcast", "solcast") };
  await new Promise((r) => setTimeout(r, 10));
  // Only the active provider's history is fetched, not every configured one: see the "faster card
  // load, especially on a provider switch" optimization (2026-09-10).
  assert.equal(callCount, 1, "expected the first hass= to fetch only the active provider's history");
  assert.ok(
    card._forecastHistoryPoints.every((p) => p.w === 100),
    `expected Solcast's history, got ${JSON.stringify(card._forecastHistoryPoints)}`
  );

  card.hass = { themes: { darkMode: false }, callWS, states: statesWithSource("Helios Forecast", "helios_forecast") };
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(callCount, 2, "expected switching source to trigger a second fetch, not reuse the stale one");
  assert.ok(
    card._forecastHistoryPoints.every((p) => p.w === 300),
    `expected Helios's history after switching, got ${JSON.stringify(card._forecastHistoryPoints)}`
  );
});

test("the forecast history stays empty when no history entity was resolved server-side", async () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({});
  card._hass = {
    themes: { darkMode: false },
    callWS: async () => {
      throw new Error("must not be called when forecast_history_entities is empty");
    },
    states: { ...BASE_CONFIG_ENTITY, ...configEntityWithForecast(buildForecast(dayStart)) },
  };
  setDevicesAttr(card, []);

  await card._refresh();

  assert.deepEqual(card._forecastHistoryPoints, []);
});

test("_refresh prefers recorder statistics over raw history for production/consumption", async () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const fiveMinAgo = new Date(Date.now() - 5 * 60000);
  const card = new Card();
  card.setConfig({ production_entity: "sensor.production", consumption_entity: "sensor.consumption" });
  const base = configEntityWithForecast(buildForecast(dayStart))["sensor.solar_planner_scheduler_config"];
  card._hass = {
    themes: { darkMode: false },
    callWS: async (msg) => {
      assert.equal(msg.type, "recorder/statistics_during_period", "expected only a statistics call, no raw history fallback");
      const id = msg.statistic_ids[0];
      const value = id === "sensor.production" ? 500 : 200;
      return { [id]: [{ start: fiveMinAgo.getTime(), end: Date.now(), mean: value }] };
    },
    states: { ...BASE_CONFIG_ENTITY, "sensor.solar_planner_scheduler_config": base },
  };
  setDevicesAttr(card, []);

  await card._refresh();

  assert.deepEqual(card._actualCurve.map((p) => p.w), [500]);
  assert.deepEqual(card._consumptionCurve.map((p) => p.w), [200]);
});

test("_refresh falls back to raw history when an entity has no recorder statistics", async () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const twoHoursAgo = new Date(Date.now() - 2 * 3600000);
  const card = new Card();
  card.setConfig({ production_entity: "sensor.production" });
  const base = configEntityWithForecast(buildForecast(dayStart))["sensor.solar_planner_scheduler_config"];
  card._hass = {
    themes: { darkMode: false },
    callWS: async (msg) => {
      if (msg.type === "recorder/statistics_during_period") return {};
      assert.equal(msg.type, "history/history_during_period");
      assert.equal(msg.minimal_response, true, "expected the raw-history fallback to request the compact minimal_response shape");
      return { "sensor.production": [{ last_changed: twoHoursAgo.toISOString(), state: "700" }] };
    },
    states: { ...BASE_CONFIG_ENTITY, "sensor.solar_planner_scheduler_config": base },
  };
  setDevicesAttr(card, []);

  await card._refresh();

  // smoothCurve() forward-fills the single raw sample into every 5-minute bucket, so every bucket
  // (not just the first) must show it.
  assert.ok(card._actualCurve.length > 1, JSON.stringify(card._actualCurve));
  assert.ok(
    card._actualCurve.every((p) => p.w === 700),
    `expected every bucket to show the raw-history fallback value, got ${JSON.stringify(card._actualCurve)}`
  );
});

test("each device's coverage badge reflects its own sensor attribute independently", () => {
  // Coverage subtraction between overlapping devices is now computed server-side (coordinator.py).
  // This only checks the card renders each device's own reported number, not the subtraction math itself.
  const card = buildCard();
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      coveragePct: 67,
    }),
    ...deviceEntities("lave_vaisselle", {
      name: "Lave-vaisselle",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 100 * 60000),
      powerW: 1200,
      coveragePct: 95,
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  const badges = [...html.matchAll(/<span class="coverage-pct ([\w-]+)">(\d+)% solar<\/span>/g)];
  assert.equal(badges.length, 2, `expected a coverage badge for each device, got ${badges.length}`);
  const pcts = badges.map((m) => m[2]).sort();
  assert.deepEqual(pcts, ["67", "95"]);
});

test("estimated cost shows next to the coverage badge when present, hidden when absent", () => {
  const card = buildCard();
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      coveragePct: 67,
      estimatedCost: 0.42,
      currency: "EUR",
    }),
    ...deviceEntities("lave_vaisselle", {
      name: "Lave-vaisselle",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 100 * 60000),
      powerW: 1200,
      coveragePct: 95,
      // estimatedCost omitted: price tracking disabled for this device's config entry.
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  const costBadges = [...html.matchAll(/<span class="estimated-cost">([^<]+)<\/span>/g)];
  assert.equal(costBadges.length, 1, `expected exactly one cost badge, got ${costBadges.length}`);
  assert.equal(costBadges[0][1], "~0.42 EUR");
});

test("cost_display: both shows both a cost and a savings badge", () => {
  const card = new Card();
  card.setConfig({ ...baseConfig(), cost_display: "both" });
  card._hass = buildCard()._hass;
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      coveragePct: 67,
      estimatedCost: 0.42,
      estimatedSavings: 1.1,
      currency: "EUR",
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes('<span class="estimated-cost">~0.42 EUR</span>'), "expected the cost badge");
  assert.ok(html.includes('<span class="estimated-savings">~1.10 EUR saved</span>'), "expected the savings badge");
});

test("cost_display: savings hides the cost badge and shows only the savings badge", () => {
  const card = new Card();
  card.setConfig({ ...baseConfig(), cost_display: "savings" });
  card._hass = buildCard()._hass;
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      coveragePct: 67,
      estimatedCost: 0.42,
      estimatedSavings: 1.1,
      currency: "EUR",
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes('class="estimated-cost"'), "expected the cost badge hidden");
  assert.ok(html.includes('<span class="estimated-savings">~1.10 EUR saved</span>'), "expected the savings badge");
});

test("stacked consumption has no implicit base-load layer", () => {
  const card = buildCard();
  card._render();
  assert.equal(rectsWithClass(card.shadowRoot.innerHTML, "stack-base").length, 0);
});

test("an active selection whose slot already ended still renders in the stack", () => {
  const card = buildCard({ withActiveSelections: false });
  const pastStart = new Date(Date.now() - 180 * 60000);
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: pastStart,
      end: new Date(pastStart.getTime() + 120 * 60000),
      powerW: 1800,
      coveragePct: 80,
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(rectsWithClass(html, "stack-confirmed").length > 0, "expected the past slot to still render as a confirmed stack segment");
});

test("stacked chart segments render at exact phase-boundary granularity, not a fixed bucket", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    max_simultaneous_power: 4000,
    devices: ["lave_vaisselle"],
  });
  const profile = [
    { minutes: 40, power_w: 100 },
    { minutes: 10, power_w: 2000 },
    { minutes: 15, power_w: 100 },
    { minutes: 5, power_w: 2000 },
    { minutes: 20, power_w: 100 },
  ];
  const durationMin = profile.reduce((s, p) => s + p.minutes, 0);
  const slotStart = new Date(Date.now() + 10 * 60000);
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_vaisselle", {
        name: "Lave-vaisselle",
        start: slotStart,
        end: new Date(slotStart.getTime() + durationMin * 60000),
        profile,
        coveragePct: 90,
      }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_vaisselle"]));
  card._render();
  const rects = rectsWithClass(card.shadowRoot.innerHTML, "stack-confirmed");
  assert.equal(rects.length, 5, `expected exactly one segment per phase, got ${rects.length}`);
  const widths = rects.map((attrs) => parseFloat(/width="([^"]*)"/.exec(attrs)?.[1] ?? "NaN"));
  assert.ok(widths[3] < widths[0] / 4, `expected the 5-min phase narrower than the 40-min one, got ${widths[3]} vs ${widths[0]}`);
});

test("a profile-based program's energy label sums its phases, not durationMin times a null powerW", () => {
  // Regression: profile-based programs have powerW=null (the flat-power field is only meaningful
  // for non-profile programs). The energy label must sum minutes*power_w per phase instead of
  // multiplying durationMin by a null powerW (which silently renders "0.0 kWh").
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    max_simultaneous_power: 4000,
    devices: ["lave_vaisselle"],
  });
  const profile = [
    { minutes: 40, power_w: 100 },
    { minutes: 10, power_w: 2000 },
    { minutes: 15, power_w: 100 },
    { minutes: 5, power_w: 2000 },
    { minutes: 20, power_w: 100 },
  ];
  const durationMin = profile.reduce((s, p) => s + p.minutes, 0);
  const slotStart = new Date(Date.now() + 10 * 60000);
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_vaisselle", {
        name: "Lave-vaisselle",
        start: slotStart,
        end: new Date(slotStart.getTime() + durationMin * 60000),
        profile,
        coveragePct: 90,
      }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_vaisselle"]));
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes("0.0 kWh"), "expected a real energy total, not the null-powerW artifact");
  assert.ok(html.includes("0.6 kWh · peak 2.0 kW"), `expected "0.6 kWh · peak 2.0 kW" in the gantt title, got: ${html}`);
});

test("a short power spike renders at its true peak, not diluted by a bucket average", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    max_simultaneous_power: 4000,
    devices: ["lave_vaisselle"],
  });
  const profile = [
    { minutes: 22, power_w: 100 },
    { minutes: 3, power_w: 2000 },
    { minutes: 35, power_w: 100 },
  ];
  const durationMin = profile.reduce((s, p) => s + p.minutes, 0);
  const slotStart = new Date(Date.now() + 10 * 60000);
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("lave_vaisselle", {
        name: "Lave-vaisselle",
        start: slotStart,
        end: new Date(slotStart.getTime() + durationMin * 60000),
        profile,
        coveragePct: 90,
      }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_vaisselle"]));
  card._render();
  const rects = rectsWithClass(card.shadowRoot.innerHTML, "stack-confirmed");
  assert.equal(rects.length, 3, `expected exactly one segment per phase, got ${rects.length}`);
  const heights = rects.map((attrs) => parseFloat(/height="([^"]*)"/.exec(attrs)?.[1] ?? "NaN"));
  assert.ok(heights[1] > heights[0] * 15, `expected the spike's height to reflect its true peak, got ${heights[1]} vs base ${heights[0]}`);
});

test("fixed loads get distinct colors, not a shared gray", () => {
  // Pinned before 13:00: PAC's window (13:00-14:00) must still be today's occurrence and not yet
  // elapsed, otherwise a 1h-future view (chart_hours_future: 1) has no next-day offset to fall
  // back to and would drop its bar entirely depending on the real wall-clock time the suite runs at.
  withFixedNow(8, 0, () => {
    const card = new Card();
    // A single-day view (chart_hours_past/future both narrow), so each load renders exactly one
    // occurrence regardless of what day offsets _visibleDayOffsets() would otherwise add.
    card.setConfig({ devices: ["lave_linge"], chart_hours_past: 0, chart_hours_future: 1 });
    card._hass = {
      themes: { darkMode: false },
      states: {
        ...BASE_CONFIG_ENTITY,
        ...configEntityWithForecast(buildForecast(new Date())),
        ...deviceEntities("lave_linge", { name: "Lave-linge", active: false }),
      },
    };
    setDevicesAttr(card, singleProgramDevices(["lave_linge"]));
    setFixedLoads(card, [
      { name: "PAC", start_time: "13:00", power_profile: [{ minutes: 60, power_w: 1500 }] },
      { name: "Base conso", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 300 }] },
    ]);
    card._render();
    const html = card.shadowRoot.innerHTML;
    const styleMatches = [...html.matchAll(/style="fill:(#[0-9a-fA-F]+)" class="bar fixed"/g)].map((m) => m[1]);
    assert.equal(styleMatches.length, 2, "expected a fill color on each fixed-load bar");
    assert.notEqual(styleMatches[0], styleMatches[1], "the two fixed loads must not share the same color");
  });
});

test("the forecast-source select is absent with fewer than two options", () => {
  const card = buildCard();
  // buildCard() never sets select.solar_planner_scheduler_forecast_source: absent entity,
  // same as zero options.
  card._render();
  assert.ok(!card.shadowRoot.innerHTML.includes('id="forecast-source-select"'));
});

test("the forecast-source select renders one option per configured provider", () => {
  const card = buildCard();
  card._hass.states["select.solar_planner_scheduler_forecast_source"] = {
    state: "Solcast",
    attributes: { options: ["Solcast", "Helios Forecast"] },
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes('id="forecast-source-select"'));
  assert.ok(html.includes('<option value="Solcast" selected>Solcast</option>'));
  assert.ok(html.includes('<option value="Helios Forecast" >Helios Forecast</option>'));
});

test("the forecast-source select is hidden when the chart section is collapsed", () => {
  const card = buildCard();
  card._hass.states["select.solar_planner_scheduler_forecast_source"] = {
    state: "Solcast",
    attributes: { options: ["Solcast", "Helios Forecast"] },
  };
  card._showChart = false;
  card._render();
  assert.ok(!card.shadowRoot.innerHTML.includes('id="forecast-source-select"'));
});

test("changing the forecast source calls select.select_option with the chosen option", async () => {
  const card = buildCard();
  const calls = [];
  card._hass.callService = async (domain, service, data) => calls.push({ domain, service, data });
  await card._onForecastSourceChange("Helios Forecast");
  assert.deepEqual(calls, [
    {
      domain: "select",
      service: "select_option",
      data: { entity_id: "select.solar_planner_scheduler_forecast_source", option: "Helios Forecast" },
    },
  ]);
});

test("selecting a new forecast source dims the chart and disables the select before the service call resolves", async () => {
  // The server-side switch always runs a full coordinator refresh before the service call itself
  // resolves (the new source drives real scheduling, not just display): with no immediate feedback
  // here the old curve was left looking frozen for however long that round trip takes.
  const card = buildCard();
  card._hass.states["select.solar_planner_scheduler_forecast_source"] = {
    state: "Solcast",
    attributes: { options: ["Solcast", "Helios Forecast"] },
  };
  card._showChart = true;
  let resolveCall;
  card._hass.callService = () => new Promise((resolve) => (resolveCall = resolve));

  const changePromise = card._onForecastSourceChange("Helios Forecast");
  await new Promise((r) => setTimeout(r, 0)); // let the synchronous _render() before the await land

  let html = card.shadowRoot.innerHTML;
  assert.match(html, /class="chart-scroll pending"/, "expected the chart dimmed while the switch is in flight");
  assert.match(html, /id="forecast-source-select" disabled/, "expected the select disabled while the switch is in flight");
  assert.match(html, /class="chart-spinner"/, "expected a loading spinner over the dimmed chart");

  resolveCall();
  await changePromise;
});

test("the pending dim clears once a forecast-source switch actually completes", async () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const forecastConfigEntity = configEntityWithForecast(buildForecast(dayStart))["sensor.solar_planner_scheduler_config"];
  const statesWithSource = (source, provider) => ({
    ...BASE_CONFIG_ENTITY,
    "sensor.solar_planner_scheduler_config": {
      ...forecastConfigEntity,
      attributes: {
        ...forecastConfigEntity.attributes,
        devices: [],
        forecast_history_entities: { solcast: "sensor.solcast_power_now", helios_forecast: "sensor.helios_power_now" },
      },
    },
    "select.solar_planner_scheduler_forecast_source": {
      state: source,
      attributes: { options: ["Solcast", "Helios Forecast"], provider },
    },
  });
  const callWS = async ({ entity_ids }) => ({ [entity_ids[0]]: [] });

  const card = new Card();
  card.setConfig({});
  card.hass = { themes: { darkMode: false }, callWS, states: statesWithSource("Solcast", "solcast") };
  await new Promise((r) => setTimeout(r, 10));

  card._forecastSourcePending = true;
  card._showChart = true;
  card._render();
  assert.match(card.shadowRoot.innerHTML, /class="chart-scroll pending"/);
  assert.match(card.shadowRoot.innerHTML, /class="chart-spinner"/, "expected the spinner while pending");

  card.hass = { themes: { darkMode: false }, callWS, states: statesWithSource("Helios Forecast", "helios_forecast") };
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(card._forecastSourcePending, false, "expected the switch's own refresh to clear the pending flag");
  assert.doesNotMatch(card.shadowRoot.innerHTML, /class="chart-scroll pending"/);
  assert.doesNotMatch(card.shadowRoot.innerHTML, /class="chart-spinner"/, "expected the spinner cleared too");
});

test("toggling a program dims that row and shows a spinner before the switch resolves", async () => {
  // async_set_program_active awaits a full coordinator refresh server-side before the switch
  // service call resolves (same shape as the forecast-source switch above): without immediate
  // feedback the row looks frozen for that whole round trip, easily mistaken for the click not
  // having registered at all.
  const card = buildCard({ withActiveSelections: false });
  let resolveCall;
  card._hass.callService = () => new Promise((resolve) => (resolveCall = resolve));

  const togglePromise = card._onToggleActive("lave_linge", true);
  await new Promise((r) => setTimeout(r, 0)); // let the synchronous _render() before the await land

  let html = card.shadowRoot.innerHTML;
  assert.match(html, /class="program-row pending"/, "expected the toggled row dimmed while the switch is in flight");
  assert.match(html, /data-row="lave_linge"[^>]*disabled/, "expected the toggle button disabled while in flight");
  assert.match(html, /class="row-spinner"/, "expected a spinner next to the pending row");

  resolveCall();
  await togglePromise;

  html = card.shadowRoot.innerHTML;
  assert.doesNotMatch(html, /class="program-row pending"/, "expected the dim cleared once the switch resolved");
  assert.doesNotMatch(html, /class="row-spinner"/, "expected the spinner cleared too");
});

test("activating a program calls switch.turn_on with the right entity", async () => {
  const card = buildCard({ withActiveSelections: false });
  const calls = [];
  card._hass.callService = async (domain, service, data) => calls.push({ domain, service, data });
  await card._onToggleActive("lave_linge", true);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { domain: "switch", service: "turn_on", data: { entity_id: "switch.lave_linge_active" } });
});

test("deactivating a program calls switch.turn_off with the right entity", async () => {
  const card = buildCard();
  const calls = [];
  card._hass.callService = async (domain, service, data) => calls.push({ domain, service, data });
  await card._onToggleActive("lave_linge", false);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { domain: "switch", service: "turn_off", data: { entity_id: "switch.lave_linge_active" } });
});

test("setting a manual time writes a single forced datetime.set_value call", async () => {
  const card = buildCard();
  const calls = [];
  card._hass.callService = async (domain, service, data) => calls.push({ domain, service, data });
  await card._onManualTime("lave_linge", "14:30");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].domain, "datetime");
  assert.equal(calls[0].service, "set_value");
  assert.equal(calls[0].data.entity_id, "datetime.lave_linge_start");
});

test("a locked slot renders an Auto button that calls reset_to_auto when clicked", async () => {
  const card = buildCard();
  card._hass.states["datetime.lave_linge_start"] = {
    ...card._hass.states["datetime.lave_linge_start"],
    attributes: { ...card._hass.states["datetime.lave_linge_start"].attributes, locked: true },
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes('class="auto-btn"'), "expected an Auto button when the slot is locked");

  const calls = [];
  card._hass.callService = async (domain, service, data) => calls.push({ domain, service, data });
  await card._onAutoMode("lave_linge");
  assert.deepEqual(calls, [
    { domain: "solar_planner_scheduler", service: "reset_to_auto", data: { entity_id: "datetime.lave_linge_start" } },
  ]);
});

test("an unlocked slot renders no Auto button", () => {
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes('class="auto-btn"'), "expected no Auto button when the slot isn't locked");
});

test("gantt markup includes a hidden live-percentage label for drag feedback", () => {
  // _bindGanttDrag can't be exercised here (dom-shim's querySelector/querySelectorAll are stubs, no
  // real pointer events). This only guards the static markup _bindGanttDrag depends on: a single
  // drag-pct-group (shared across bars, only one drag happens at a time), starting hidden, plus a
  // draggable class on the confirmed bar it's meant to follow.
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("bar-draggable"), "expected the confirmed bar to be draggable");
  assert.match(html, /<g class="drag-pct-group" style="opacity:0"/, "expected the live-% group to start hidden");
  assert.ok(html.includes('class="drag-pct-bg"'), "expected a background for the live-% label");
  assert.ok(html.includes('class="drag-pct"'), "expected the live-% text element");
});

test("stack order mirrors the gantt's top-to-bottom config order, not reversed", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = new Card();
  card.setConfig({
    forecast_entity: "sensor.forecast",
    max_simultaneous_power: 4000,
    devices: ["a", "b"],
  });
  const slotStart = new Date(Date.now() + 10 * 60000);
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      ...configEntityWithForecast(buildForecast(dayStart)),
      ...deviceEntities("a", { name: "A", start: slotStart, end: new Date(slotStart.getTime() + 60 * 60000), powerW: 500, coveragePct: 90 }),
      ...deviceEntities("b", { name: "B", start: slotStart, end: new Date(slotStart.getTime() + 60 * 60000), powerW: 800, coveragePct: 90 }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["a", "b"]));
  card._render();
  const html = card.shadowRoot.innerHTML;

  const colorA = /style="fill:(#[0-9a-fA-F]+)"[^>]*data-device="a"/.exec(html)?.[1];
  const colorB = /style="fill:(#[0-9a-fA-F]+)"[^>]*data-device="b"/.exec(html)?.[1];
  assert.ok(colorA && colorB && colorA !== colorB, "expected distinct colors for A and B's gantt bars");

  const stackRects = rectsWithClass(html, "stack-confirmed");
  const rectA = stackRects.find((attrs) => attrs.includes(`fill:${colorA}`));
  const rectB = stackRects.find((attrs) => attrs.includes(`fill:${colorB}`));
  assert.ok(rectA && rectB, "expected a stacked segment for each device");

  const yA = parseFloat(/ y="([^"]*)"/.exec(rectA)?.[1] ?? "NaN");
  const yB = parseFloat(/ y="([^"]*)"/.exec(rectB)?.[1] ?? "NaN");
  assert.ok(yA < yB, `expected A (first in config, gantt's top lane) drawn above B in the stack, got yA=${yA} vs yB=${yB}`);
});

test("a forecast entity with P10/P90 percentiles renders a confidence band", () => {
  // Pinned mid-afternoon: buildForecast() only covers 6:00-20:00, so the default 24h-future view
  // needs "now" comfortably inside that range, or the band has no data left past "now" depending
  // on the real wall-clock time the suite runs at.
  withFixedNow(14, 0, () => {
    const dayStart = new Date();
    dayStart.setHours(0, 0, 0, 0);
    const card = buildCard();
    setForecastPoints(card, buildForecast(dayStart, 3, true));
    card._render();
    const html = card.shadowRoot.innerHTML;
    assert.ok(/<path d="M[^"]+Z" class="confidence-band"/.test(html), "expected a closed confidence-band path");
  });
});

test("a forecast entity without P10/P90 draws no confidence band", () => {
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes('class="confidence-band"'), "expected no confidence-band path without percentiles");
});

test("an active program's slot shows a countdown to its start, hidden once it's running", () => {
  const card = buildCard();
  card._render();
  // buildCard() schedules Lave-linge 10 minutes from now.
  assert.match(card.shadowRoot.innerHTML, /<span class="countdown" data-target="[^"]+">in \d+m<\/span>/, "expected a countdown span for a future start");

  // buildCard() schedules both devices at the same slotStart, flip both to running.
  card._hass.states["binary_sensor.lave_linge_should_run"] = { state: "on" };
  card._hass.states["binary_sensor.lave_vaisselle_should_run"] = { state: "on" };
  card._render();
  assert.ok(!card.shadowRoot.innerHTML.includes('class="countdown"'), "expected no countdown once the program is running");
});

test("the table's Window column includes a countdown to a future fixed load's start", () => {
  const card = buildCard();
  card._showTable = true;
  card._render();
  // PAC (buildCard()'s fixed load) starts 20 minutes from now.
  assert.match(
    card.shadowRoot.innerHTML,
    /PAC \(external\)<\/td>\s*<td>[\d:]+ - [\d:]+<span class="countdown" data-target="[^"]+" data-wrap="paren"> \(in \d+m\)<\/span><\/td>/
  );
});

test("a full-day fixed load's tomorrow occurrence is deduped out of the table, unlike a shorter daily one", () => {
  // Pinned mid-morning: PAC's window (14:00-15:00) must still be today's occurrence and not yet
  // elapsed relative to the 6h-past view start, or it drops out depending on the real wall-clock
  // time the suite runs at (viewStart = now - 6h must stay <= PAC's own end, 15:00).
  withFixedNow(8, 0, () => {
    const dayStart = new Date();
    dayStart.setHours(0, 0, 0, 0);
    const tomorrowStart = new Date(dayStart);
    tomorrowStart.setDate(tomorrowStart.getDate() + 1);

    const card = buildCard();
    enableTomorrowForecast(card);
    addForecastPoints(card, buildForecast(tomorrowStart));
    setFixedLoads(card, [
      { name: "Conso de base", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 110 }] },
      { name: "PAC", start_time: "14:00", power_profile: [{ minutes: 60, power_w: 1500 }] },
    ]);
    card._showTable = true;
    card._render();
    const html = card.shadowRoot.innerHTML;

    const baseRows = [...html.matchAll(/<td>Conso de base[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)];
    assert.equal(baseRows.length, 1, `expected the full-day load's duplicate "Tomorrow" row removed, got ${baseRows.length} rows`);

    const pacRows = [...html.matchAll(/<td>PAC[^<]*<\/td>\s*<td>([\s\S]*?)<\/td>/g)];
    assert.equal(pacRows.length, 2, `expected a genuinely daily-recurring load to still show both today and tomorrow, got ${pacRows.length}`);
  });
});

test("chart_expanded/table_expanded config defaults drive which section renders open", () => {
  const cardDefault = buildCard();
  cardDefault._render();
  let html = cardDefault.shadowRoot.innerHTML;
  assert.ok(html.includes('class="chart-scroll"'), "expected the chart section open by default");
  assert.ok(!html.includes("<table>"), "expected the table closed by default");

  const cardTableOnly = new Card();
  cardTableOnly.setConfig({ devices: ["lave_linge", "lave_vaisselle"], chart_expanded: false, table_expanded: true });
  cardTableOnly._hass = cardDefault._hass;
  cardTableOnly._render();
  html = cardTableOnly.shadowRoot.innerHTML;
  assert.ok(!html.includes('class="chart-scroll"'), "expected the chart section closed when chart_expanded: false");
  assert.ok(html.includes("<table>"), "expected the table open when table_expanded: true");
});

test("the chart and table sections toggle independently of each other", () => {
  const card = buildCard();
  card._render();
  assert.match(card.shadowRoot.innerHTML, /id="toggle-chart" title="Hide planning"/, "expected an open-state icon by default");

  card._showChart = false;
  card._render();
  let html = card.shadowRoot.innerHTML;
  assert.match(html, /id="toggle-chart" title="Show planning"/);
  assert.ok(html.includes("mdi:chevron-down"), "expected the collapsed chart toggle to show a down chevron");
  assert.ok(!html.includes('class="chart-scroll"'), "expected the chart section removed once collapsed");
  assert.ok(html.includes('id="toggle-table"'), "expected the (still closed) table toggle to keep rendering");
  assert.ok(!html.includes("<table>"), "expected the table to stay closed, collapsing the chart must not open it");
});

test("connectedCallback refreshes the countdown display periodically, not via a full re-render", (t) => {
  mock.timers.enable({ apis: ["setInterval"] });
  t.after(() => mock.timers.reset());

  const card = buildCard();
  card._render();
  let renderCount = 0;
  let updateCount = 0;
  card._render = () => renderCount++;
  card._updateCountdowns = () => updateCount++;

  card.connectedCallback();
  mock.timers.tick(60 * 1000);
  assert.ok(updateCount >= 1, "expected a countdown update within 60s, well under the 5-minute full-refresh interval");
  assert.equal(renderCount, 0, "expected the countdown timer to skip the full innerHTML rebuild");

  const countIn60s = updateCount;
  card.disconnectedCallback();
  mock.timers.tick(5 * 60 * 1000);
  assert.equal(updateCount, countIn60s, "expected disconnectedCallback to stop the countdown timer too");
});

test("_updateCountdowns runs against the fake shadowRoot without throwing", () => {
  // This suite's dom-shim.js stubs querySelectorAll/querySelector to always return []/null (no real
  // DOM), so the actual per-span text mutation can't be asserted here — only that the method's own
  // logic (reading el.dataset, computing fmtCountdown) doesn't blow up when nothing matches.
  // The span markup itself (data-target/data-wrap) is covered by the innerHTML regex tests above.
  const card = buildCard();
  card._render();
  assert.doesNotThrow(() => card._updateCountdowns());
});

test("table_show_energy/table_show_cost hide their respective table columns", () => {
  const card = new Card();
  card.setConfig({ devices: ["lave_linge"], table_expanded: true, table_show_energy: false, table_show_cost: false });
  card._hass = buildCard()._hass;
  setDevicesAttr(card, singleProgramDevices(["lave_linge"], { names: { lave_linge: "Lave-linge" } }));
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes("<th>Energy</th>"), "expected the Energy header hidden");
  assert.ok(!html.includes("<th>Cost</th>"), "expected the Cost header hidden");
  assert.ok(html.includes("<th>Device</th>") && html.includes("<th>Window</th>"), "expected the other headers to still render");
});

test("table_show_energy/table_show_cost default to shown when unset", () => {
  const card = buildCard();
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Energy</th>") && html.includes("<th>Cost</th>"), "expected both columns shown by default");
  assert.ok(!html.includes("<th>Savings</th>"), "expected no Savings column with cost_display defaulting to cost");
});

test("cost_display: both adds a Savings column alongside Cost in the table", () => {
  const card = new Card();
  card.setConfig({ ...baseConfig(), cost_display: "both", table_expanded: true });
  card._hass = buildCard()._hass;
  card._hass.states = {
    ...card._hass.states,
    ...deviceEntities("lave_linge", {
      name: "Lave-linge",
      start: new Date(Date.now() + 10 * 60000),
      end: new Date(Date.now() + 130 * 60000),
      powerW: 1800,
      coveragePct: 67,
      estimatedCost: 0.42,
      estimatedSavings: 1.1,
      currency: "EUR",
    }),
  };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Cost</th>") && html.includes("<th>Savings</th>"), "expected both table columns");
  assert.ok(html.includes("~1.10 EUR"), "expected the savings value in a table cell");
});
