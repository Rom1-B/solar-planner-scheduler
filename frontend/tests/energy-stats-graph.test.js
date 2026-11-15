import "./dom-shim.js";
import { test } from "node:test";
import assert from "node:assert/strict";

const {
  suggestedPeriod,
  compareShift,
  pointOffset,
  resolveSeries,
  restrictEnergyData,
  lineSeries,
  defineCard,
  differenceInDays,
  differenceInMonths,
  energySources,
  hourlyShares,
  costStats,
  costEnergyData,
  sumCostStats,
  SOLAR_SUFFIX,
  COST_GRID,
  COST_SOLAR,
  COST_EXPORT,
  COST_NET,
  exportStats,
  compensationStat,
  netBillSeries,
  orderCostSeries,
} = await import("../energy-stats-graph-card.js");

globalThis.document ??= { createElement: () => ({ innerHTML: "" }) };

const d = (...args) => new Date(...args);
const endOfDay = (y, m, day) => new Date(y, m, day, 23, 59, 59, 999);

test("suggestedPeriod follows the native rule: whole months over 35 days, days over 2, hours otherwise", () => {
  assert.equal(suggestedPeriod(d(2026, 10, 15), endOfDay(2026, 10, 15)), "hour");
  assert.equal(suggestedPeriod(d(2026, 10, 11), endOfDay(2026, 10, 13)), "hour", "3 calendar days are 2 full days");
  assert.equal(suggestedPeriod(d(2026, 10, 10), endOfDay(2026, 10, 13)), "day");
  assert.equal(suggestedPeriod(d(2026, 9, 1), endOfDay(2026, 9, 31)), "day", "a single month is not over 35 days");
  assert.equal(suggestedPeriod(d(2026, 8, 1), endOfDay(2026, 9, 31)), "month");
  assert.equal(suggestedPeriod(d(2026, 9, 5), endOfDay(2026, 10, 13)), "day", "40 days not on month bounds");
  assert.equal(suggestedPeriod(d(2026, 0, 1), endOfDay(2026, 11, 31)), "month");
});

test("date differences count full units, like date-fns", () => {
  assert.equal(differenceInDays(endOfDay(2026, 10, 13), d(2026, 10, 11)), 2);
  assert.equal(differenceInDays(d(2026, 10, 13), d(2026, 10, 11)), 2);
  assert.equal(differenceInMonths(d(2026, 10, 1), d(2025, 10, 1)), 12);
  assert.equal(differenceInMonths(d(2026, 10, 1), d(2026, 9, 2)), 0);
});

test("compareShift maps compare dates onto the current axis by years, months or days", () => {
  // Previous year from a 1 January start: same calendar date one year later.
  const yoy = compareShift(d(2026, 0, 1), d(2025, 0, 1));
  assert.deepEqual(yoy(d(2025, 5, 15, 10)), d(2026, 5, 15, 10));
  // Previous month from a month start: same day of month, falling back to days when it doesn't exist.
  const month = compareShift(d(2026, 10, 1), d(2026, 9, 1));
  assert.deepEqual(month(d(2026, 9, 10)), d(2026, 10, 10));
  // 31 October has no 31 November: shifted by the day count instead (31 days to 1 December).
  assert.deepEqual(month(d(2026, 9, 31)), d(2026, 11, 1));
  // Previous day from a midnight start.
  const day = compareShift(d(2026, 10, 14), d(2026, 10, 13));
  assert.deepEqual(day(d(2026, 10, 13, 15)), d(2026, 10, 14, 15));
  assert.deepEqual(compareShift(d(2026, 10, 14), undefined)(d(2026, 10, 1)), d(2026, 10, 1));
});

test("hourly points sit mid-hour, other steps at their start", () => {
  assert.equal(pointOffset("hour"), 1800e3);
  assert.equal(pointOffset("day"), 0);
  assert.equal(pointOffset("month"), 0);
});

const METADATA = [
  { statistic_id: "sensor.pac", has_sum: true, unit_class: "energy", statistics_unit_of_measurement: "Wh", name: "PAC energy" },
  { statistic_id: "sensor.gas", has_sum: true, unit_class: "volume", statistics_unit_of_measurement: "m³" },
  { statistic_id: "sensor.ext", has_sum: false, unit_class: "temperature", statistics_unit_of_measurement: "°C" },
];

test("resolveSeries keeps native bars for energy meters and makes everything else a line", () => {
  const [pac, gas, ext, forced] = resolveSeries(
    { series: [{ entity: "sensor.pac" }, { entity: "sensor.gas" }, { entity: "sensor.ext", name: "Outside", dashed: true }, { entity: "sensor.pac", type: "line" }] },
    METADATA,
    (id) => id
  );
  assert.deepEqual([pac.type, pac.stat, pac.unit, pac.customName], ["bar", "change", "kWh", undefined]);
  assert.deepEqual([gas.type, gas.stat, gas.unit], ["line", "change", "m³"]);
  assert.deepEqual([ext.type, ext.stat, ext.dashed, ext.customName], ["line", "mean", true, "Outside"]);
  assert.ok(ext.color && gas.color && ext.color !== gas.color, "expected distinct line colors");
  assert.equal(forced.type, "line");
});

const ENERGY = {
  start: d(2026, 10, 14),
  end: endOfDay(2026, 10, 14),
  prefs: {
    energy_sources: [{ type: "grid" }],
    device_consumption: [{ stat_consumption: "sensor.tv", name: "TV" }, { stat_consumption: "sensor.pac", name: "PAC" }],
  },
  stats: { "sensor.tv": [{ start: 1, change: 1 }], "sensor.pac": [{ start: 1, change: 2 }] },
  statsCompare: undefined,
};

test("restrictEnergyData keeps every device in place for native colors but only the wanted statistics", () => {
  const bars = [
    { id: "sensor.pac", name: "PAC", type: "bar" },
    { id: "sensor.boiler", name: "Boiler", type: "bar", customName: "Boiler" },
  ];
  const data = restrictEnergyData(ENERGY, bars, { stats: { "sensor.boiler": [{ start: 1, change: 3 }] } });
  assert.deepEqual(data.prefs.energy_sources, [], "no sources, so no untracked series");
  assert.deepEqual(
    data.prefs.device_consumption.map((dev) => dev.stat_consumption),
    ["sensor.tv", "sensor.pac", "sensor.boiler"],
    "devices keep their Energy dashboard position, extra meters are appended"
  );
  assert.deepEqual(Object.keys(data.stats), ["sensor.pac", "sensor.boiler"]);
  assert.equal(data.statsCompare, undefined);
  assert.equal(ENERGY.prefs.device_consumption.length, 2, "the shared energy data is left untouched");
});

test("lineSeries draws the current line and a faded compare line shifted onto the current axis", () => {
  const line = { id: "sensor.ext", name: "Outside", type: "line", stat: "mean", color: "#e53935", dashed: true, unit: "°C" };
  const h = d(2026, 10, 14, 10).getTime();
  const hc = d(2026, 10, 13, 10).getTime();
  const data = { ...ENERGY, startCompare: d(2026, 10, 13), statsCompare: {} };
  const extra = { period: "hour", stats: { "sensor.ext": [{ start: h, mean: 5 }] }, statsCompare: { "sensor.ext": [{ start: hc, mean: 7 }] } };
  const [compare, current] = lineSeries([line], extra, data, 1);
  assert.deepEqual(current.data, [[h + 1800e3, 5, h]]);
  assert.equal(current.lineStyle.type, "dashed");
  assert.equal(current.yAxisIndex, 1);
  assert.equal(compare.id, "compare-sensor.ext");
  assert.deepEqual(compare.data, [[h + 1800e3, 7, hc]], "compare point moved onto the current day, original date kept for the tooltip");
  assert.equal(compare.color, "#e539357F");
  assert.equal(lineSeries([line], null, data, 1).length, 0);
});

// Stand-in for hui-energy-devices-detail-graph-card: records the data it is given and builds one series per device.
class FakeNative {
  constructor() {
    this._chartData = [];
    this._hiddenStats = [];
    this._createOptions = () => ({ yAxis: { type: "value", name: "kWh" }, tooltip: { trigger: "axis", formatter: (bars) => `native:${bars.map((b) => b.seriesId).join(",")}` } });
  }
  setConfig(config) {
    this._config = config;
  }
  willUpdate() {}
  requestUpdate() {}
  _getStatIdFromId(id) {
    return id.replace(/^compare-/, "").replace(/-\d+$/, "");
  }
  _processStatistics() {
    this.seen = this._data;
    this._chartData = [
      { id: "compare-placeholder", data: [] },
      ...this._data.prefs.device_consumption.map((dev) => ({ id: `${dev.stat_consumption}-0`, data: this._data.stats[dev.stat_consumption] ? [1] : [] })),
    ];
    this._legendData = this._data.prefs.device_consumption.map((dev) => ({ id: `${dev.stat_consumption}-0`, name: dev.name }));
  }
}

const Card = defineCard(FakeNative);

function buildCard() {
  const card = new Card();
  card.hass = { states: {}, locale: { language: "en", number_format: "language" } };
  card.setConfig({ series: [{ entity: "sensor.pac" }, { entity: "sensor.ext" }] });
  card._series = resolveSeries(card._config, METADATA, (id) => id);
  card._data = ENERGY;
  const h = d(2026, 10, 14, 10).getTime();
  card._extra = { data: ENERGY, period: "hour", stats: { "sensor.ext": [{ start: h, mean: 5 }] }, statsCompare: null };
  return card;
}

test("setConfig requires at least one series", () => {
  assert.throws(() => new Card().setConfig({}), /at least one series/);
});

test("the native computation gets the restricted data, then only the configured bars plus the lines are kept", () => {
  const card = buildCard();
  card._processStatistics();
  assert.deepEqual(Object.keys(card.seen.stats), ["sensor.pac"], "native fed with the configured bar only");
  assert.equal(card._data, ENERGY, "the reactive data is restored after the native computation");
  assert.deepEqual(
    card._chartData.map((s) => s.id),
    ["compare-placeholder", "sensor.pac-0", "sensor.ext"],
    "TV dropped, the temperature line appended"
  );
  assert.deepEqual(
    card._legendData.map((item) => item.id),
    ["sensor.pac-0", "sensor.ext"]
  );
});

test("lines get a hidden second scale and their own unit in the tooltip, grouped under their period", () => {
  const card = buildCard();
  card._processStatistics();
  const options = card._createOptions();
  assert.equal(options.yAxis.length, 2);
  assert.equal(options.yAxis[1].show, false);
  const out = card._tooltip(
    [
      { seriesId: "sensor.pac-0", componentSubType: "bar", value: [0, 1, 0] },
      { seriesId: "sensor.ext", seriesName: "Outside", componentSubType: "line", value: [0, 5.25, 0], color: "#e53935" },
    ],
    (bars) => `native:${bars.map((b) => b.seriesId).join(",")}`
  );
  assert.equal(out[0], "native:sensor.pac-0", "bars keep the native tooltip");
  assert.match(out[1].innerHTML, /Outside: <div[^>]*>5.25 °C<\/div>/);
});

test("hidden series are kept per card, not in the storage the native devices card shares", () => {
  const a = new Card();
  const b = new Card();
  a._hiddenStats = ["sensor.pac"];
  assert.deepEqual(a._hiddenStats, ["sensor.pac"]);
  assert.deepEqual(b._hiddenStats, []);
});

test("energySources reads solar, grid, battery and the grid price in both grid formats", () => {
  const flat = energySources({
    energy_sources: [
      { type: "solar", stat_energy_from: "sensor.solar" },
      { type: "grid", stat_energy_from: "sensor.import", stat_energy_to: "sensor.export", entity_energy_price: "sensor.price" },
      { type: "battery", stat_energy_from: "sensor.bat_out", stat_energy_to: "sensor.bat_in" },
    ],
  });
  assert.deepEqual([flat.solar, flat.gridIn, flat.gridOut, flat.price, flat.batteryIn], [["sensor.solar"], ["sensor.import"], ["sensor.export"], ["sensor.price"], ["sensor.bat_in"]]);
  const legacy = energySources({ energy_sources: [{ type: "grid", flow_from: [{ stat_energy_from: "sensor.import", number_energy_price: 0.2 }], flow_to: [] }] });
  assert.deepEqual([legacy.gridIn, legacy.fixedPrice], [["sensor.import"], 0.2]);
});

test("costStats splits each hour by its grid share at that hour's price, summed per step", () => {
  const h0 = d(2026, 10, 14, 10).getTime();
  const h1 = h0 + 3600e3;
  const src = { solar: ["sensor.solar"], gridIn: ["sensor.import"], gridOut: [], batteryIn: [], batteryOut: [] };
  // h0: 1 kWh solar + 1 kWh import (50 % grid) at 0.2; h1: grid only at 0.3.
  const changes = {
    "sensor.solar": new Map([[h0, 1], [h1, 0]]),
    "sensor.import": new Map([[h0, 1], [h1, 2]]),
    "sensor.pac": new Map([[h0, 1], [h1, 1]]),
  };
  const shares = hourlyShares(changes, src, (h) => (h === h0 ? 0.2 : 0.3));
  assert.deepEqual(shares.get(h0), { share: 0.5, price: 0.2 });
  const hourly = costStats(changes, shares, ["sensor.pac"], "hour", 0.25);
  assert.deepEqual(hourly["sensor.pac"].map((r) => r.change), [0.1, 0.3]);
  assert.deepEqual(hourly["sensor.pac" + SOLAR_SUFFIX].map((r) => r.change), [0.1, 0]);
  const daily = costStats(changes, shares, ["sensor.pac"], "day", 0.25);
  assert.equal(daily["sensor.pac"].length, 1);
  assert.equal(daily["sensor.pac"][0].start, d(2026, 10, 14).getTime());
  assert.ok(Math.abs(daily["sensor.pac"][0].change - 0.4) < 1e-9);
  assert.equal(daily["sensor.pac"][0].end, d(2026, 10, 15).getTime());
});

test("sumCostStats adds the meters' grid costs and solar savings per step", () => {
  const perMeter = {
    "sensor.pac": [{ start: 1, change: 0.4 }, { start: 2, change: 0.1 }],
    "sensor.pac#solar": [{ start: 1, change: 0.2 }],
    "sensor.stove": [{ start: 1, change: 0.05 }],
    "sensor.stove#solar": [],
  };
  const total = sumCostStats(perMeter, ["sensor.pac", "sensor.stove"], "hour");
  assert.deepEqual(total[COST_GRID].map((r) => [r.start, Math.round(r.change * 100) / 100]), [[1, 0.45], [2, 0.1]]);
  assert.deepEqual(total[COST_SOLAR].map((r) => [r.start, r.change]), [[1, 0.2]]);
});

test("costEnergyData hands the native card two pseudo-devices in currency, no sources", () => {
  const cost = { stats: { [COST_GRID]: [{ start: 1, change: 0.45 }], [COST_SOLAR]: [{ start: 1, change: 0.2 }] } };
  const data = costEnergyData(ENERGY, cost, { grid: "Réseau", solar: "Solaire" });
  assert.deepEqual(
    data.prefs.device_consumption.map((dev) => [dev.stat_consumption, dev.name]),
    [[COST_GRID, "Réseau"], [COST_SOLAR, "Solaire"]]
  );
  assert.deepEqual(data.prefs.energy_sources, []);
  assert.equal(data.stats, cost.stats);
});

test("cost mode keeps the grid and solar series in the Energy colors, legend totals in currency", () => {
  const card = new Card();
  card.hass = { states: {}, config: { currency: "EUR" }, locale: { language: "fr", number_format: "language" }, localize: (key, args) => `${args.num} ${args.unit}` };
  card.setConfig({ mode: "cost", series: [{ entity: "sensor.pac" }] });
  card._series = resolveSeries(card._config, METADATA, (id) => id);
  card._data = ENERGY;
  card._extra = { data: ENERGY, period: "day", stats: {}, cost: { stats: { [COST_GRID]: [{ start: 1, change: 0.4 }], [COST_SOLAR]: [{ start: 1, change: 0.1 }] } } };
  const process = FakeNative.prototype._processStatistics;
  FakeNative.prototype._processStatistics = function () {
    process.call(this);
    this._chartData = this._chartData.map((s) => ({ ...s, color: "#0000007F", itemStyle: { borderColor: "#000000" }, data: s.data.length ? [[1, s.id.startsWith(COST_GRID) ? 0.4 : 0.1]] : [] }));
  };
  try {
    card._processStatistics();
  } finally {
    FakeNative.prototype._processStatistics = process;
  }
  assert.deepEqual(
    card._chartData.filter((s) => s.id !== "compare-placeholder").map((s) => [s.id, s.color, s.itemStyle.borderColor]),
    [
      [`${COST_GRID}-0`, "#488fc27F", "#488fc2"],
      [`${COST_SOLAR}-0`, "#ff98007F", "#ff9800"],
    ]
  );
  assert.deepEqual(
    card._legendData.map((item) => [item.name, item.value]),
    [["Réseau", "0,40 €"], ["Solaire", "0,10 €"]]
  );
  assert.equal(card._formatTotal(0.5), "Total 0,50 €");
});

test("compensationStat uses the configured compensation statistic, else HA's own compensation sensor if it exists", () => {
  const flat = energySources({ energy_sources: [{ type: "grid", stat_energy_to: "sensor.export", stat_compensation: "sensor.my_income" }] });
  assert.equal(compensationStat(flat, {}), "sensor.my_income");
  const legacy = energySources({ energy_sources: [{ type: "grid", flow_from: [], flow_to: [{ stat_energy_to: "sensor.export", stat_compensation: "sensor.legacy_income" }] }] });
  assert.equal(compensationStat(legacy, {}), "sensor.legacy_income");
  const auto = energySources({ energy_sources: [{ type: "grid", stat_energy_to: "sensor.export" }] });
  assert.equal(compensationStat(auto, { "sensor.export_compensation": { state: "1" } }), "sensor.export_compensation");
  assert.equal(compensationStat(auto, {}), null, "no export price set: HA records no income");
});

test("exportStats turns the hourly compensation changes into negative income per step", () => {
  const h0 = d(2026, 10, 14, 12).getTime();
  const changes = { "sensor.export_compensation": new Map([[h0, 0.2], [h0 + 3600e3, 0.05]]) };
  const rows = exportStats(changes, "sensor.export_compensation", "day");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].start, d(2026, 10, 14).getTime());
  assert.ok(Math.abs(rows[0].change + 0.25) < 1e-9, "income below the axis");
  assert.deepEqual(exportStats({}, "sensor.export_compensation", "day"), [], "a period before the export price has no income");
});

test("costEnergyData adds the export pseudo-device only when export income was loaded", () => {
  const words = { grid: "Grid", solar: "Solar", export: "Export" };
  const without = costEnergyData(ENERGY, { stats: { [COST_GRID]: [], [COST_SOLAR]: [] } }, words);
  assert.equal(without.prefs.device_consumption.length, 2);
  const withExport = costEnergyData(ENERGY, { stats: { [COST_GRID]: [], [COST_SOLAR]: [], [COST_EXPORT]: [{ start: 1, change: -0.2 }] } }, words);
  assert.deepEqual(withExport.prefs.device_consumption.map((dev) => [dev.stat_consumption, dev.name]).at(-1), [COST_EXPORT, "Export"]);
});

test("the mode switch flips kWh / currency in place and reloads the extra statistics", () => {
  const card = buildCard();
  let loads = 0;
  card._loadExtra = () => loads++;
  assert.equal(card._isCost(), false);
  card._setMode("cost");
  assert.equal(card._isCost(), true);
  assert.equal(card._extra, null, "stale kWh extras dropped");
  assert.equal(loads, 1);
  card._setMode("cost");
  assert.equal(loads, 1, "no reload when the mode doesn't change");
  card.setConfig({ mode: "cost", series: [{ entity: "sensor.pac" }] });
  assert.equal(card._isCost(), true, "a new config starts from its own mode");
});

test("the net bill line is grid cost minus export income per step, dashed on the currency axis, only with export", () => {
  const cost = {
    stats: {
      [COST_GRID]: [{ start: 1, change: 0.5 }, { start: 2, change: 0.2 }],
      [COST_EXPORT]: [{ start: 1, change: -0.1 }, { start: 2, change: -0.3 }],
    },
  };
  const [line] = netBillSeries(cost, ENERGY, "day", "Net bill", "#616161");
  assert.equal(line.id, COST_NET);
  assert.equal(line.yAxisIndex, 0, "same axis as the currency bars");
  assert.equal(line.lineStyle.type, "dashed");
  assert.deepEqual(line.data.map((p) => [p[0], Math.round(p[1] * 100) / 100]), [[1, 0.4], [2, -0.1]], "can go below zero");
  assert.deepEqual(netBillSeries({ stats: { [COST_GRID]: [] } }, ENERGY, "day", "Net bill", "#616161"), []);
});

test("cost series keep a fixed stack order whatever their totals, with the rounded ends moved to match", () => {
  const statOf = (id) => id.replace(/^compare-/, "").replace(/-\d+$/, "");
  // Native order by period total: solar first here, rounded end on grid (then the top).
  const native = [
    { id: "compare-placeholder", data: [] },
    { id: `${COST_SOLAR}-0`, data: [[1, 2, 1], [2, 0.5, 2]] },
    { id: `${COST_GRID}-1`, data: [{ value: [1, 1, 1], itemStyle: { borderRadius: [4, 4, 0, 0] } }, [2, 3, 2]] },
    { id: `${COST_EXPORT}-2`, data: [[1, -0.4, 1], [2, 0, 2]] },
  ];
  const ordered = orderCostSeries(native, statOf);
  assert.deepEqual(ordered.map((series) => statOf(series.id)), ["placeholder", COST_GRID, COST_SOLAR, COST_EXPORT]);
  const [, grid, solar, exported] = ordered;
  assert.deepEqual(grid.data, [[1, 1, 1], [2, 3, 2]], "grid at the bottom: square ends");
  assert.deepEqual(solar.data.map((p) => p.itemStyle?.borderRadius), [[4, 4, 0, 0], [4, 4, 0, 0]], "solar on top: rounded");
  assert.deepEqual(exported.data[0].itemStyle.borderRadius, [0, 0, 4, 4], "export below the axis: rounded bottom");
  assert.deepEqual(exported.data[1], [2, 0, 2], "zero export left alone");
});
