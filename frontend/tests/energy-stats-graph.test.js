import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { getCardClass } from "./dom-shim.js";

const { strings, autoPeriod, buckets, resolveSeries, chartSeries, axisBounds, energyCollection } = await import("../energy-stats-graph-card.js");
const Card = getCardClass("energy-stats-graph-card");

const H0 = new Date(2026, 10, 10, 10).getTime();
const H1 = H0 + 3600e3;
const H2 = H1 + 3600e3;

const METADATA = [
  { statistic_id: "sensor.pac", has_sum: true, unit_class: "energy", statistics_unit_of_measurement: "Wh" },
  { statistic_id: "sensor.poele", has_sum: true, unit_class: "energy", statistics_unit_of_measurement: "kWh" },
  { statistic_id: "sensor.ext", has_sum: false, unit_class: "temperature", statistics_unit_of_measurement: "°C" },
];
const CONFIG = {
  series: [
    { entity: "sensor.pac", name: "PAC" },
    { entity: "sensor.poele" },
    { entity: "sensor.ext", name: "Outside", dashed: true },
    { entity: "sensor.solar", name: "Solar", type: "line", color: "#123456" },
  ],
};

const resolved = () => resolveSeries(CONFIG, [...METADATA, { statistic_id: "sensor.solar", has_sum: true, unit_class: "energy" }], (id) => `friendly ${id}`);

test("strings fall back to English and match a regional variant", () => {
  assert.equal(strings("fr-FR").total("2 kWh"), "Total : 2 kWh");
  assert.equal(strings("de").total("2 kWh"), "Total: 2 kWh");
});

test("autoPeriod mirrors the Energy dashboard's hour / day / month steps", () => {
  const start = new Date(2026, 10, 1);
  assert.equal(autoPeriod(start, new Date(2026, 10, 2)), "hour");
  assert.equal(autoPeriod(start, new Date(2026, 10, 8)), "day");
  assert.equal(autoPeriod(start, new Date(2027, 2, 1)), "month");
});

test("buckets cover the range and keep starts present in the data", () => {
  const start = new Date(2026, 10, 10, 10, 30);
  assert.deepEqual(buckets(start, new Date(H2), "hour"), [H0, H1]);
  assert.deepEqual(buckets(start, new Date(H1), "hour", [H2]), [H0, H2]);
});

test("resolveSeries picks change bars for meters, mean lines otherwise, energy in kWh", () => {
  const [pac, poele, ext, solar] = resolved();
  assert.deepEqual([pac.stat, pac.type, pac.unit], ["change", "bar", "kWh"]);
  assert.equal(poele.name, "friendly sensor.poele");
  assert.deepEqual([ext.stat, ext.type, ext.unit, ext.dashed], ["mean", "line", "°C", true]);
  assert.deepEqual([solar.stat, solar.type, solar.color], ["change", "line", "#123456"]);
});

test("axisBounds matches the native Energy charts' time axis", () => {
  const start = new Date(2026, 10, 1);
  const dayEnd = new Date(2026, 10, 1, 23, 59, 59, 999);
  assert.deepEqual(axisBounds("hour", start, dayEnd), { min: start, max: new Date(2026, 10, 1, 23, 30) });
  assert.deepEqual(axisBounds("day", start, new Date(2026, 10, 7, 23, 59, 59, 999)).max, new Date(2026, 10, 7));
  assert.deepEqual(axisBounds("day", start, new Date(2026, 10, 8)).max, new Date(2026, 10, 7));
  assert.deepEqual(axisBounds("month", new Date(2026, 0, 1), new Date(2026, 11, 31, 23, 59, 59, 999)), {
    min: new Date(2025, 11, 27),
    max: new Date(2026, 11, 6),
  });
});

test("chartSeries stacks bars per unit, keeps every bucket and turns zero bars into gaps", () => {
  const series = resolved();
  const values = {
    "sensor.pac": new Map([[H0, 1], [H1, 2]]),
    "sensor.poele": new Map([[H0, 0], [H1, 0.5]]),
    "sensor.ext": new Map([[H0, 5]]),
    "sensor.solar": new Map([[H1, 3]]),
  };
  const [pac, poele, ext, solar] = chartSeries(series, values, [H0, H1], "hour");
  assert.equal(pac.stack, "bar-kWh");
  assert.equal(poele.stack, "bar-kWh");
  assert.deepEqual(poele.data.map((p) => p[1]), [null, 0.5]);
  assert.deepEqual(pac.data[0], [H0 + 1800e3, 1, H0]);
  assert.deepEqual(chartSeries(series, values, [H0], "day")[0].data[0], [H0, 1, H0]);
  assert.equal(ext.yAxisIndex, 1);
  assert.equal(ext.lineStyle.type, "dashed");
  assert.deepEqual(ext.data.map((p) => p[1]), [5, null]);
  assert.equal(solar.type, "line");
  assert.equal(solar.stack, undefined);
  assert.equal(solar.yAxisIndex, 0);
});

test("energyCollection prefers collection_key, then the dashboard's own key, then the default", () => {
  const own = {};
  const fallback = {};
  const keyed = {};
  const connection = { _energy_lovelace: own, _energy: fallback, _energy_maison: keyed };
  assert.equal(energyCollection({ connection, panelUrl: "lovelace" }, "energy_maison"), keyed);
  assert.equal(energyCollection({ connection, panelUrl: "lovelace" }), own);
  assert.equal(energyCollection({ connection: { _energy: fallback }, panelUrl: "lovelace" }), fallback);
});

function cardWithData(locale = { language: "en", number_format: "language" }) {
  const card = new Card();
  card._config = CONFIG;
  card._hass = { locale };
  const series = resolved();
  card._data = {
    series,
    units: ["kWh", "°C"],
    period: "hour",
    start: new Date(H0),
    end: new Date(H2),
    values: { "sensor.pac": new Map([[H0, 1], [H1, 2]]), "sensor.poele": new Map([[H1, 0.5]]), "sensor.ext": new Map(), "sensor.solar": new Map([[H1, 3]]) },
  };
  return card;
}

const param = (id, name, value, subType) => ({ seriesId: id, seriesName: name, value: [0, value, H1], componentSubType: subType, marker: "" });

test("the tooltip totals a unit only when its stack has several bars, never the line series", () => {
  const card = cardWithData();
  const html = card._tooltipHtml([
    param("sensor.pac", "PAC", 2, "bar"),
    param("sensor.poele", "Poele", 0.5, "bar"),
    param("sensor.solar", "Solar", 3, "line"),
  ]);
  assert.match(html, /Total: 2\.5 kWh/);
  assert.doesNotMatch(card._tooltipHtml([param("sensor.pac", "PAC", 2, "bar"), param("sensor.solar", "Solar", 3, "line")]), /Total/);
});

test("the header total sums the bar series only", () => {
  assert.deepEqual(cardWithData()._totals(), ["3.5 kWh"]);
});

test("only the first unit's axis is shown; bars start at zero, lines fit their range", () => {
  const [left, right] = cardWithData()._options().yAxis;
  assert.deepEqual([left.show, left.name, left.scale], [true, "kWh", false]);
  assert.deepEqual([right.show, right.name, right.scale], [false, "°C", true]);
});

test("numbers follow the HA profile's number format, not only the language", () => {
  assert.equal(cardWithData({ language: "en", number_format: "decimal_comma" })._num(1.5, 1), "1,5");
  assert.equal(cardWithData({ language: "fr", number_format: "comma_decimal" })._num(1.5, 1), "1.5");
  assert.equal(cardWithData({ language: "fr", number_format: "language" })._num(1.5, 1), "1,5");
});

test("the axis keeps the date picker's period end, only the statistics query stops at now", async () => {
  if (!customElements.get("ha-chart-base")) customElements.define("ha-chart-base", class {});
  const calls = [];
  const card = new Card();
  card._config = CONFIG;
  card._render = () => {};
  card._hass = {
    locale: { language: "en" },
    states: {},
    callWS: async (msg) => {
      calls.push(msg);
      return msg.type === "recorder/get_statistics_metadata" ? [...METADATA, { statistic_id: "sensor.solar", has_sum: true, unit_class: "energy" }] : {};
    },
  };
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const end = new Date(start.getTime() + 24 * 3600e3 - 1);
  card._setPeriod({ start, end });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(card._error, null);
  const query = calls.find((m) => m.type === "recorder/statistics_during_period");
  assert.ok(Date.parse(query.end_time) <= Date.now());
  assert.equal(card._data.end.getTime(), end.getTime());
  assert.equal(card._options().xAxis.max.getTime(), new Date(start.getFullYear(), start.getMonth(), start.getDate(), 23, 30).getTime());
  assert.equal(card._data.times.length, 24);
});
