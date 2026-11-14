import "./dom-shim.js";
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { getCardClass } from "./dom-shim.js";

const { strings, sourceStats, split, buildTree, leaves, hourlyHouse, energyCollection } = await import("../energy-split-card.js");
const Card = getCardClass("energy-split-card");

const H0 = Date.UTC(2026, 10, 10, 10);
const H1 = H0 + 3600e3;

const PREFS = {
  energy_sources: [
    { type: "solar", stat_energy_from: "sensor.solar" },
    { type: "grid", stat_energy_from: "sensor.import", stat_energy_to: "sensor.export", number_energy_price: 0.2 },
  ],
  device_consumption: [
    { stat_consumption: "sensor.circuit", name: "Circuit" },
    { stat_consumption: "sensor.washer", name: "Washer", included_in_stat: "sensor.circuit" },
  ],
};

const row = (start, change) => ({ start, change });
// H0: 1 kWh solar + 1 kWh import (50% grid); H1: 2 kWh import only (100% grid).
const STATS = {
  "sensor.solar": [row(H0, 1), row(H1, 0)],
  "sensor.import": [row(H0, 1), row(H1, 2)],
  "sensor.export": [row(H0, 0), row(H1, 0)],
  "sensor.circuit": [row(H0, 1), row(H1, 1)],
  "sensor.washer": [row(H0, 0.5), row(H1, 0.5)],
};

function fakeHass({ language = "en", connection = {}, panelUrl = "lovelace" } = {}) {
  const calls = [];
  return {
    calls,
    panelUrl,
    connection,
    locale: { language },
    config: { currency: "EUR" },
    states: {},
    callWS: async (msg) => {
      calls.push(msg);
      return msg.type === "energy/get_prefs" ? PREFS : STATS;
    },
  };
}

function fakeCollection() {
  const collection = { listeners: [] };
  collection.subscribe = (cb) => {
    collection.listeners.push(cb);
    return () => (collection.listeners = collection.listeners.filter((l) => l !== cb));
  };
  return collection;
}

function mountCard(config, hass) {
  const card = new Card();
  card.isConnected = true;
  card.setConfig(config);
  card.hass = hass;
  return card;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("strings fall back to English for an unknown language and match a regional variant", () => {
  assert.equal(strings("fr-FR").columns.name, "Appareil");
  assert.equal(strings("de").columns.name, "Device");
  assert.equal(strings(undefined).columns.name, "Device");
});

test("sourceStats reads both the flat and the flow_from/flow_to grid formats", () => {
  const legacy = sourceStats({
    energy_sources: [
      {
        type: "grid",
        flow_from: [{ stat_energy_from: "sensor.import", entity_energy_price: "sensor.price" }],
        flow_to: [{ stat_energy_to: "sensor.export" }],
      },
    ],
  });
  assert.deepEqual(legacy.gridIn, ["sensor.import"]);
  assert.deepEqual(legacy.gridOut, ["sensor.export"]);
  assert.deepEqual(legacy.price, ["sensor.price"]);
  assert.equal(sourceStats(PREFS).fixedPrice, 0.2);
});

test("split prices each hour's grid share as cost and its solar share as savings", () => {
  const hours = new Map([
    [H0, { share: 0.5, price: 0.2 }],
    [H1, { share: 1, price: 0.2 }],
  ]);
  const out = split(new Map([[H0, 1], [H1, 1]]), hours, 0.2);
  assert.equal(out.total, 2);
  assert.equal(out.grid, 1.5);
  assert.ok(Math.abs(out.cost - 0.3) < 1e-9);
  assert.ok(Math.abs(out.saved - 0.1) < 1e-9);
});

test("buildTree adds an unmetered remainder for a circuit and for the house", () => {
  const changes = Object.fromEntries(Object.entries(STATS).map(([id, rows]) => [id, new Map(rows.map((r) => [r.start, r.change]))]));
  const hours = hourlyHouse(changes, sourceStats(PREFS), () => 0.2);
  const tree = buildTree(PREFS, changes, hours, 0.2, (id) => id);
  const byId = Object.fromEntries(leaves(tree).map((n) => [n.id, n]));
  assert.equal(byId["sensor.washer"].total, 1);
  assert.equal(byId["sensor.circuit__untracked__"].total, 1);
  assert.equal(byId["sensor.circuit__untracked__"].name, "Circuit");
  // House = 2 kWh (H0) + 2 kWh (H1); circuit = 2 kWh, so 2 kWh unmetered at the root.
  assert.equal(byId["__house____untracked__"].total, 2);
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

test("a linked card follows the energy-date-selection period and hides its own presets", async () => {
  const collection = fakeCollection();
  const hass = fakeHass({ connection: { _energy_maison: collection } });
  const card = mountCard({ collection_key: "energy_maison" }, hass);
  assert.equal(collection.listeners.length, 1);

  const start = new Date(2026, 9, 1);
  collection.listeners[0]({ start, end: new Date(2026, 9, 31, 23, 59, 59) });
  await flush();

  const query = hass.calls.find((m) => m.type === "recorder/statistics_during_period");
  assert.equal(query.start_time, start.toISOString());
  assert.equal(query.end_time, new Date(2026, 9, 31, 23, 59, 59).toISOString());
  const html = card.shadowRoot.innerHTML;
  assert.doesNotMatch(html, /data-preset/);
  assert.match(html, /Washer/);
  assert.match(html, /Circuit \(rest\)/);
  assert.match(html, /House \(rest\)/);

  card.disconnectedCallback();
  assert.equal(collection.listeners.length, 0);
});

test("a period ending in the future is clipped to now", async () => {
  const collection = fakeCollection();
  const hass = fakeHass({ connection: { _energy: collection }, panelUrl: undefined });
  mountCard({}, hass);
  const before = Date.now();
  collection.listeners[0]({ start: new Date(before - 3600e3), end: new Date(before + 86400e3) });
  await flush();
  const query = hass.calls.find((m) => m.type === "recorder/statistics_during_period");
  assert.ok(Date.parse(query.end_time) <= Date.now());
});

test("without an energy-date-selection card, the card falls back to its own presets", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const hass = fakeHass();
    const card = mountCard({}, hass);
    assert.doesNotMatch(card.shadowRoot.innerHTML, /data-preset/);
    // Each retry schedules the next one, so advance one retry interval at a time.
    for (let i = 0; i <= 10; i++) mock.timers.tick(300);
    await flush();
    assert.match(card.shadowRoot.innerHTML, /data-preset="7d"/);
    assert.ok(hass.calls.some((m) => m.type === "recorder/statistics_during_period"));
  } finally {
    mock.timers.reset();
  }
});

test("the card renders in the HA user's language", async () => {
  const collection = fakeCollection();
  const card = mountCard({}, fakeHass({ language: "fr", connection: { _energy_lovelace: collection } }));
  collection.listeners[0]({ start: new Date(H0), end: new Date(H1 + 3600e3) });
  await flush();
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /Solaire \/ réseau par appareil/);
  assert.match(html, /title="Appareil"/);
  assert.match(html, /Circuit \(reste\)/);
  assert.match(html, /Maison \(reste\)/);
});
