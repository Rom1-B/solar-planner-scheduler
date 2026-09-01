import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { getCardClass } from "./dom-shim.js";

await import("../solar-planner-card.js");
const Card = getCardClass("solar-planner-card");

function pad(n) {
  return String(n).padStart(2, "0");
}

// The card reads forecast/production/consumption/max_simultaneous_power from this sensor's
// attributes instead of its own config — spread into every test's `states` object.
const BASE_CONFIG_ENTITY = {
  "sensor.solar_planner_scheduler_config": {
    state: "4000",
    attributes: {
      forecast_entity: "sensor.forecast",
      forecast_tomorrow_entity: null,
      production_entity: null,
      consumption_entity: null,
      fixed_loads: [],
      devices: [],
    },
  },
};

// One program named "Eco" per device slug, with the program's own row-slug equal to the device
// slug — matches the real integration when a device has exactly one program, and keeps this
// suite's entity_ids (datetime.<slug>_start etc.) unchanged from before per-program rows existed.
// `names` overrides the display name per slug (defaults to the slug itself) — matches
// sensor.py's real "name" field (the device's configured CONF_NAME, not its entity_id slug).
function singleProgramDevices(slugs, { programName = "Eco", names = {} } = {}) {
  return slugs.map((slug) => ({ name: names[slug] ?? slug, slug, programs: [{ name: programName, slug }] }));
}

// Overwrites the config sensor's devices attribute — call after setConfig({devices: [...]}) with
// a matching list of slugs, or _programRows() finds nothing to render.
function setDevicesAttr(card, devices) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, devices },
  };
}

// Card no longer reads forecast_tomorrow_entity from its own config — flips it on in the shared
// config sensor's attributes instead.
function enableTomorrowForecast(card) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, forecast_tomorrow_entity: "sensor.forecast_tomorrow" },
  };
}

// Card no longer reads fixed_loads from its own config either — set them on the shared config
// sensor's attributes instead. Each load is {name, start_time, power_profile}, matching what
// BaseConfigSensor exposes (already wrapped as a single-phase profile server-side).
function setFixedLoads(card, fixedLoads) {
  const entity = card._hass.states["sensor.solar_planner_scheduler_config"];
  card._hass.states["sensor.solar_planner_scheduler_config"] = {
    ...entity,
    attributes: { ...entity.attributes, fixed_loads: fixedLoads },
  };
}

function buildForecast(dayStart, peakKw = 3, withConfidence = false) {
  const detailedForecast = [];
  for (let h = 6; h <= 20; h++) {
    for (const m of [0, 30]) {
      const t = new Date(dayStart);
      t.setHours(h, m, 0, 0);
      const sunFactor = Math.max(0, Math.sin(((h + m / 60 - 6) / 14) * Math.PI));
      const pvEstimate = sunFactor * peakKw;
      const point = { period_start: t.toISOString(), pv_estimate: pvEstimate };
      if (withConfidence) {
        point.pv_estimate10 = pvEstimate * 0.7;
        point.pv_estimate90 = pvEstimate * 1.3;
      }
      detailedForecast.push(point);
    }
  }
  return detailedForecast;
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
      },
    },
    [`binary_sensor.${slug}_should_run`]: { state: shouldRun ? "on" : "off" },
    [`switch.${slug}_active`]: { state: active ? "on" : "off" },
  };
}

function baseConfig() {
  return { devices: ["lave_linge", "lave_vaisselle"] };
}

// Two devices (Lave-linge 120min@1800W, Lave-vaisselle 90min@1200W) plus one PAC fixed load —
// the baseline most tests build on. withActiveSelections=false gives both programs an inactive switch.
// PAC's start_time is computed relative to the real current time, not hardcoded — a fixed "13:00"
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
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
  // PAC (fixed_loads) always carries its config's own power_profile — 60 min @ 1500 W -> 1500 Wh
  // (1.5 kWh) total, 1.5 kW peak.
  assert.ok(html.includes("1.5 kWh · peak 1.5 kW"), "expected the fixed-load label to show total energy plus peak");
});

test('the table shows energy in kWh and "-" for a fixed load\'s program column', () => {
  const card = buildCard();
  card._showTable = true;
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(html.includes("<th>Energy</th>"), "expected the table header to read Energy, not Power");
  assert.ok(!html.includes("<th>Power</th>"), "expected no leftover Power header");
  assert.match(html, /<td>PAC \(external\)<\/td><td>-<\/td>/);
  assert.match(html, /<td>PAC \(external\)<\/td><td>-<\/td>\s*<td>[^<]*<\/td>\s*<td>1\.5 kWh<\/td>/);
  assert.match(html, /<td>Lave-linge<\/td><td>Eco<\/td>/);
});

test("an inactive program renders no gantt bar or stack segment", () => {
  const card = buildCard({ withActiveSelections: false });
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.equal(rectsWithClass(html, "stack-confirmed").length, 0, "nothing should be scheduled without an active program");
  assert.ok(html.includes('class="program-toggle "'), "expected an inactive toggle button to render");
});

test("a slot scheduled after sunset still renders within the chart (view widens beyond daylight)", () => {
  // The fallback "least-bad slot" placement can push a device past sunset when no full-solar window
  // exists that day. The chart view must widen to include every scheduled item, not just daylight hours.
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

test("forecast_tomorrow_entity widens the chart without shrinking today's own resolution", () => {
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
  withTomorrow._hass.states["sensor.forecast_tomorrow"] = { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } };
  withTomorrow._render();
  const htmlWith = withTomorrow.shadowRoot.innerHTML;
  const barWith = ganttBarGeometry(htmlWith, "lave_linge");
  assert.ok(barWith, "expected Lave-linge's gantt bar to render with forecast_tomorrow_entity");

  assert.ok(Math.abs(barWith.x - barWithout.x) < 0.1, `expected the same x position, got ${barWith.x} vs ${barWithout.x}`);
  assert.ok(Math.abs(barWith.width - barWithout.width) < 0.1, `expected the same width, got ${barWith.width} vs ${barWithout.width}`);

  const chartWidth = parseFloat(/<svg class="chart" viewBox="0 0 ([\d.]+)/.exec(htmlWith)?.[1] ?? "NaN");
  assert.ok(chartWidth > 600, `expected tomorrow's forecast to widen the view past the 600 baseline, got ${chartWidth}`);
});

test("a daily fixed load also shows tomorrow's occurrence once forecast_tomorrow_entity is set", () => {
  const withoutTomorrow = buildCard();
  withoutTomorrow._render();
  const fixedRectsWithout = rectsWithClass(withoutTomorrow.shadowRoot.innerHTML, "fixed");
  assert.equal(fixedRectsWithout.length, 1, `expected exactly today's PAC occurrence, got ${fixedRectsWithout.length}`);

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const withTomorrow = buildCard();
  enableTomorrowForecast(withTomorrow);
  withTomorrow._hass.states["sensor.forecast_tomorrow"] = { state: "3", attributes: { detailedForecast: buildForecast(tomorrowStart) } };
  withTomorrow._render();
  const htmlWith = withTomorrow.shadowRoot.innerHTML;
  const fixedRectsWith = rectsWithClass(htmlWith, "fixed");
  assert.equal(fixedRectsWith.length, 2, `expected today's + tomorrow's PAC occurrence, got ${fixedRectsWith.length}`);

  const ganttHeight = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(htmlWith)?.[1] ?? "NaN");
  const ganttHeightWithout = parseFloat(/<svg class="gantt" viewBox="0 0 [\d.]+ ([\d.]+)/.exec(withoutTomorrow.shadowRoot.innerHTML)?.[1] ?? "NaN");
  assert.ok(Math.abs(ganttHeight - ganttHeightWithout) < 0.1, `expected the same lane count/gantt height, got ${ganttHeight} vs ${ganttHeightWithout}`);
});

test("the table marks tomorrow's fixed-load occurrence so it doesn't read as an unexplained duplicate", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const tomorrowStart = new Date(dayStart);
  tomorrowStart.setDate(tomorrowStart.getDate() + 1);

  const card = buildCard();
  enableTomorrowForecast(card);
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
  withSunnyTomorrow._hass.states["sensor.forecast_tomorrow"] = {
    state: "8",
    attributes: { detailedForecast: buildForecast(tomorrowStart, 8) },
  };
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
      ...deviceEntities("lave_linge", { name: "Lave-linge", active: false }),
    },
  };
  setDevicesAttr(card, singleProgramDevices(["lave_linge"]));
  setFixedLoads(card, [{ name: "Conso de base", start_time: "00:00", power_profile: [{ minutes: 1440, power_w: 110 }] }]);
  card._render();
  const html = card.shadowRoot.innerHTML;
  const nowX = parseFloat(/x1="([\d.]+)"[^>]*class="now-line"/.exec(html)?.[1] ?? "NaN");
  assert.ok(!Number.isNaN(nowX), "expected a now-line to render");
  const marginLeft = 44;
  assert.ok(nowX - marginLeft <= 260, `expected "now" within ~6h of the left margin, got ${nowX - marginLeft}px past it`);
  assert.ok(nowX - marginLeft >= 0, `expected "now" at or after the left margin, got ${nowX - marginLeft}`);
});

test("each device's coverage badge reflects its own sensor attribute independently", () => {
  // Coverage subtraction between overlapping devices is now computed server-side (coordinator.py) —
  // this only checks the card renders each device's own reported number, not the subtraction math itself.
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
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
  // for non-profile programs) — the energy label must sum minutes*power_w per phase instead of
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
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
  const card = new Card();
  card.setConfig({ devices: ["lave_linge"] });
  card._hass = {
    themes: { darkMode: false },
    states: {
      ...BASE_CONFIG_ENTITY,
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(new Date()) } },
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
  // real pointer events) — this only guards the static markup _bindGanttDrag depends on: a single
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
      "sensor.forecast": { state: "3", attributes: { detailedForecast: buildForecast(dayStart) } },
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

test("a forecast entity with P10/P90 percentiles renders a confidence band and its legend", () => {
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const card = buildCard();
  card._hass.states["sensor.forecast"] = { state: "3", attributes: { detailedForecast: buildForecast(dayStart, 3, true) } };
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(/<path d="M[^"]+Z" class="confidence-band"/.test(html), "expected a closed confidence-band path");
  assert.ok(html.includes("Confidence (P10-P90)"), "expected a confidence legend entry");
});

test("a forecast entity without P10/P90 draws no confidence band or legend", () => {
  const card = buildCard();
  card._render();
  const html = card.shadowRoot.innerHTML;
  assert.ok(!html.includes('class="confidence-band"'), "expected no confidence-band path without percentiles");
  assert.ok(!html.includes("Confidence (P10-P90)"), "expected no confidence legend entry without percentiles");
});
