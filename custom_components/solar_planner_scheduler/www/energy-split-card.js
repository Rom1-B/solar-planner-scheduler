/**
 * energy-split-card
 *
 * Shows, over a chosen period, how much of each Energy-dashboard device's
 * consumption came from solar versus from the grid. Devices and their
 * hierarchy (included_in_stat) are read from the Energy preferences; nothing
 * to maintain in the card config.
 *
 * Split method: for every hour, grid share = grid import / house consumption,
 * with house consumption = solar + import - export (+ battery out - battery in).
 * Each device's hourly consumption is split with that hour's share, then summed.
 * This is a proportional allocation, not a measurement.
 *
 * Cost and solar savings use the Energy dashboard grid price for that hour
 * (price entity hourly mean, else fixed price, else the entity's current state).
 * Savings = self-consumed solar kWh x grid price; export income is not counted.
 *
 * Rendered as a table ranked by grid cost: leaf devices plus the unmetered
 * remainder of each circuit, with a total line. Clicking a column header sorts
 * by it; clicking it again reverses the order.
 *
 * When an energy-date-selection card is on the same dashboard, the card follows its
 * period and hides its own period buttons; otherwise it uses them.
 *
 * Base load = 10th percentile of the hourly average power: what the device draws
 * almost all the time, robust to a few off or missing hours. Clicking a line opens
 * its hourly average power curve over the period, with the base load dashed.
 *
 * type: custom:energy-split-card
 *   title: <optional, defaults to the translated "Solar / grid by device">
 *   period: <optional: today | yesterday | 7d | 30d | month, defaults to 7d>
 *   collection_key: <optional, same value as the energy-date-selection card's collection_key>
 *   min_kwh: <optional, hides devices consuming less over the period, defaults to 0.1>
 *   price: <optional fixed price per kWh, overrides the Energy dashboard price>
 */

const CARD_TAG = "energy-split-card";

const STRINGS = {
  en: {
    title: "Solar / grid by device",
    presets: { today: "Today", yesterday: "Yesterday", "7d": "7 days", "30d": "30 days", month: "Month", custom: "Dates" },
    columns: {
      name: "Device",
      cost: "Grid cost",
      saved: "Solar savings",
      solar: "Solar share",
      base: "Base load (always-on consumption)",
    },
    total: "Total",
    house: "House",
    rest: (name) => `${name} (rest)`,
    loading: "Loading",
    refresh: "Refresh",
    noPrice: "No grid price found in the Energy dashboard, set the price option.",
    tip: (name, total, grid, solar) => `${name}: ${total} kWh, ${grid} kWh from the grid and ${solar} kWh from solar`,
    baseLoad: "base load",
  },
  fr: {
    title: "Solaire / réseau par appareil",
    presets: { today: "Aujourd'hui", yesterday: "Hier", "7d": "7 jours", "30d": "30 jours", month: "Mois", custom: "Dates" },
    columns: {
      name: "Appareil",
      cost: "Coût réseau",
      saved: "Économie solaire",
      solar: "Part solaire",
      base: "Talon (consommation permanente)",
    },
    total: "Total",
    house: "Maison",
    rest: (name) => `${name} (reste)`,
    loading: "Chargement",
    refresh: "Rafraîchir",
    noPrice: "Aucun prix réseau trouvé dans le module Énergie, renseigner l'option price.",
    tip: (name, total, grid, solar) => `${name} : ${total} kWh dont ${grid} kWh réseau et ${solar} kWh solaire`,
    baseLoad: "talon",
  },
};

export const strings = (language) => STRINGS[String(language || "").split("-")[0]] || STRINGS.en;

const PRESETS = ["today", "yesterday", "7d", "30d", "month", "custom"];

// The energy-date-selection card may be created after this one: retry this many times before falling back to local presets.
const LINK_RETRIES = 10;
const LINK_RETRY_MS = 300;

/** Shared energy period collection, keyed like the HA frontend's getEnergyDataCollection. */
export function energyCollection(hass, key) {
  if (key) return hass.connection[`_${key}`];
  return (hass.panelUrl && hass.connection[`_energy_${hass.panelUrl}`]) || hass.connection._energy;
}

const UNTRACKED = "__untracked__";

const solarShare = (n) => (n.total > 0 ? (n.total - n.grid) / n.total : 0);
// [key, sort value, CSS class, header icon]; numeric columns sort descending first, the name ascending.
// Icons keep the numeric columns narrow; the label stays as the header tooltip.
const COLUMNS = [
  ["name", null, "name", null],
  ["cost", (n) => n.cost, "num", "mdi:transmission-tower"],
  ["saved", (n) => n.saved, "num", "mdi:solar-power-variant"],
  ["solar", solarShare, "num", "mdi:white-balance-sunny"],
  ["base", (n) => n.base, "num", "mdi:power-sleep"],
];

const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const toDateInput = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const fromDateInput = (s) => {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
};

export function presetRange(preset, now = new Date()) {
  const today = startOfDay(now);
  switch (preset) {
    case "today":
      return [today, now];
    case "yesterday":
      return [addDays(today, -1), today];
    case "30d":
      return [addDays(today, -29), now];
    case "month":
      return [new Date(now.getFullYear(), now.getMonth(), 1), now];
    default:
      return [addDays(today, -6), now];
  }
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const fmt = (kwh) => (kwh >= 100 ? kwh.toFixed(0) : kwh >= 10 ? kwh.toFixed(1) : kwh.toFixed(2));
const SUMS = ["total", "grid", "cost", "saved"];
const BASE_PERCENTILE = 0.1;

/** Source statistic ids, supporting both the flat (2025+) and flow_from/flow_to grid formats. */
export function sourceStats(prefs) {
  const out = { solar: [], gridIn: [], gridOut: [], batteryOut: [], batteryIn: [], price: [] };
  for (const src of prefs.energy_sources || []) {
    if (src.type === "solar") out.solar.push(src.stat_energy_from);
    else if (src.type === "battery") {
      out.batteryOut.push(src.stat_energy_from);
      out.batteryIn.push(src.stat_energy_to);
    } else if (src.type === "grid") {
      if (src.stat_energy_from) out.gridIn.push(src.stat_energy_from);
      if (src.stat_energy_to) out.gridOut.push(src.stat_energy_to);
      if (src.entity_energy_price) out.price.push(src.entity_energy_price);
      if (src.number_energy_price != null) out.fixedPrice ??= src.number_energy_price;
      for (const f of src.flow_from || []) {
        out.gridIn.push(f.stat_energy_from);
        if (f.entity_energy_price) out.price.push(f.entity_energy_price);
        if (f.number_energy_price != null) out.fixedPrice ??= f.number_energy_price;
      }
      for (const f of src.flow_to || []) out.gridOut.push(f.stat_energy_to);
    }
  }
  for (const k of Object.keys(out)) if (Array.isArray(out[k])) out[k] = out[k].filter(Boolean);
  out.price = out.price.slice(0, 1);
  return out;
}

/** Hourly values per statistic id for one statistic type: { id: Map(startMs -> value) }. */
export function hourly(raw, type) {
  const result = {};
  for (const [id, rows] of Object.entries(raw)) {
    const m = new Map();
    for (const r of rows) {
      const start = typeof r.start === "number" ? r.start : Date.parse(r.start);
      if (r[type] != null) m.set(start, r[type]);
    }
    result[id] = m;
  }
  return result;
}

/** Per hour: house consumption, its grid share in [0, 1] and the grid price. */
export function hourlyHouse(changes, src, priceOf) {
  const sum = (ids, h) => ids.reduce((acc, id) => acc + (changes[id]?.get(h) || 0), 0);
  const hours = new Set();
  for (const id of [...src.solar, ...src.gridIn]) for (const h of changes[id]?.keys() || []) hours.add(h);
  const shares = new Map();
  for (const h of hours) {
    const imp = sum(src.gridIn, h);
    const house = sum(src.solar, h) + imp - sum(src.gridOut, h) + sum(src.batteryOut, h) - sum(src.batteryIn, h);
    shares.set(h, { house, share: house > 0 ? Math.min(1, Math.max(0, imp / house)) : imp > 0 ? 1 : 0, price: priceOf(h) });
  }
  return shares;
}

/**
 * Splits an hourly kWh series into kWh, grid kWh, grid cost and solar savings (hours without a
 * known share count as grid), plus base load and mean power in W.
 */
export function split(series, hours, fallbackPrice) {
  const out = { total: 0, grid: 0, cost: 0, saved: 0, base: 0, mean: 0 };
  for (const [h, kwh] of series) {
    const share = hours.get(h)?.share ?? 1;
    const price = hours.get(h)?.price ?? fallbackPrice;
    out.total += kwh;
    out.grid += kwh * share;
    out.cost += kwh * share * price;
    out.saved += kwh * (1 - share) * price;
  }
  if (series.size) {
    const sorted = [...series.values()].sort((a, b) => a - b);
    out.base = Math.max(sorted[Math.floor(BASE_PERCENTILE * (sorted.length - 1))], 0) * 1000;
    out.mean = (out.total / series.size) * 1000;
  }
  return out;
}

/** Hourly difference parent - sum(children), kept signed so period sums match the meters. */
function remainderSeries(node) {
  const rest = new Map(node.series);
  for (const child of node.children) for (const [h, kwh] of child.series) rest.set(h, (rest.get(h) || 0) - kwh);
  return rest;
}

const node = (id, name, series, hours, fallbackPrice) => ({ id, name, series, ...split(series, hours, fallbackPrice), children: [] });

/**
 * Builds the device tree with an "untracked" child (named after its parent, labelled at render
 * time) for any node whose children don't add up to it, plus a root-level one against house consumption.
 */
export function buildTree(prefs, changes, hours, fallbackPrice, nameOf) {
  const devices = prefs.device_consumption || [];
  const ids = new Set(devices.map((d) => d.stat_consumption));
  const nodes = new Map(
    devices.map((d) => [
      d.stat_consumption,
      node(d.stat_consumption, d.name || nameOf(d.stat_consumption), changes[d.stat_consumption] || new Map(), hours, fallbackPrice),
    ]),
  );
  const roots = [];
  for (const d of devices) {
    const n = nodes.get(d.stat_consumption);
    if (d.included_in_stat && ids.has(d.included_in_stat)) nodes.get(d.included_in_stat).children.push(n);
    else roots.push(n);
  }

  const houseSeries = new Map([...hours].filter(([, v]) => v.house > 0).map(([h, v]) => [h, v.house]));
  const house = { ...node("__house__", null, houseSeries, hours, fallbackPrice), children: roots };

  const addUntracked = (parent) => {
    parent.children.forEach(addUntracked);
    if (!parent.children.length) return;
    const rest = node(`${parent.id}${UNTRACKED}`, parent.name, remainderSeries(parent), hours, fallbackPrice);
    for (const k of SUMS) rest[k] = Math.max(rest[k], 0);
    rest.grid = Math.min(rest.grid, rest.total);
    // Below 1% of the parent it's meter drift, not a missing device.
    if (rest.total > 0.01 * parent.total) parent.children.push({ ...rest, untracked: true });
  };
  addUntracked(house);
  return house;
}

/** Leaf nodes of the tree: real devices plus each circuit's unmetered remainder. */
export const leaves = (n) => (n.children.length ? n.children.flatMap(leaves) : [n]);

const HOUR = 3600e3;

/** Inline SVG of hourly average power (W) over [start, end], base load dashed. */
function powerChart(series, base, start, end, language, baseLabel) {
  const [w, h, left, bottom] = [600, 120, 44, 18];
  const plotW = w - left - 4;
  const plotH = h - bottom - 6;
  const points = [...series].map(([t, kwh]) => [t, Math.max(kwh, 0) * 1000]).sort((a, b) => a[0] - b[0]);
  const peak = Math.max(...points.map(([, v]) => v), base, 1);
  const step = 10 ** Math.floor(Math.log10(peak));
  const top = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].map((m) => m * step).find((v) => v >= peak);
  const watts = (v) => (v >= 1000 ? `${(v / 1000).toLocaleString(language)} kW` : `${Math.round(v)} W`);
  const span = Math.max(end - start, HOUR);
  const x = (t) => left + ((t - start) / span) * plotW;
  const y = (v) => 6 + plotH - (v / top) * plotH;
  // Step line: each hourly average spans its whole hour.
  const line = points.map(([t, v]) => `L${x(t).toFixed(1)},${y(v).toFixed(1)} L${x(t + HOUR).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const path = points.length ? `M${x(points[0][0]).toFixed(1)},${y(0)} ${line} L${x(points.at(-1)[0] + HOUR).toFixed(1)},${y(0)}` : "";

  const ticks = [];
  const days = span / (24 * HOUR);
  if (days <= 2) {
    for (let t = Math.ceil(start / (6 * HOUR)) * 6 * HOUR; t <= end; t += 6 * HOUR) {
      ticks.push([t, new Date(t).toLocaleTimeString(language, { hour: "numeric" })]);
    }
  } else {
    const every = Math.ceil(days / 8);
    for (let d = startOfDay(new Date(start + 24 * HOUR - 1)), i = 0; d.getTime() <= end; d = addDays(d, 1), i++) {
      if (i % every === 0) ticks.push([d.getTime(), d.toLocaleDateString(language, { day: "2-digit", month: "2-digit" })]);
    }
  }
  const tickSvg = ticks
    .filter(([t]) => x(t) < left + plotW - 16) // a label on the right edge would be clipped
    .map(([t, label]) => `<line x1="${x(t)}" x2="${x(t)}" y1="6" y2="${6 + plotH}" class="tick"/><text x="${x(t)}" y="${h - 4}" text-anchor="middle">${label}</text>`)
    .join("");
  return `<svg viewBox="0 0 ${w} ${h}" class="chart">
    ${tickSvg}
    <text x="${left - 4}" y="12" text-anchor="end">${watts(top)}</text>
    <text x="${left - 4}" y="${6 + plotH}" text-anchor="end">0</text>
    <path d="${path}" class="area"/>
    <line x1="${left}" x2="${left + plotW}" y1="${y(base)}" y2="${y(base)}" class="base"/>
    <text x="${left + plotW}" y="${y(base) - 3}" text-anchor="end" class="base-label">${baseLabel} ${Math.round(base)} W</text>
  </svg>`;
}

class EnergySplitCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._preset = this._config.period || "7d";
    [this._start, this._end] = presetRange(this._preset);
    this._tree = null;
    this._error = null;
    this._loading = false;
    this._sort ??= { key: "cost", desc: true };
    this._openId = null;
    this._unlink();
    this._mode = "pending"; // pending | linked | local
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    if (this._hass && this.isConnected) this._link();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._link();
  }

  connectedCallback() {
    if (this._hass && this._mode === "pending") this._link();
  }

  disconnectedCallback() {
    this._unlink();
    if (this._mode === "linked") this._mode = "pending";
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return {};
  }

  get _language() {
    return this._hass?.locale?.language || this._hass?.language || "en";
  }

  get _t() {
    return strings(this._language);
  }

  _link(tries = 0) {
    if (this._unsub || this._retry) return;
    const collection = energyCollection(this._hass, this._config.collection_key);
    if (collection) {
      this._mode = "linked";
      this._unsub = collection.subscribe((data) => this._setPeriod(data));
    } else if (tries < LINK_RETRIES) {
      this._retry = setTimeout(() => {
        this._retry = null;
        this._link(tries + 1);
      }, LINK_RETRY_MS);
    } else if (this._mode !== "local") {
      this._mode = "local";
      this._load();
    }
  }

  _unlink() {
    this._unsub?.();
    this._unsub = null;
    clearTimeout(this._retry);
    this._retry = null;
  }

  /** Period from the energy-date-selection card; its end can be missing or in the future. */
  _setPeriod(data) {
    this._linkedPeriod = data;
    this._start = new Date(data.start);
    this._end = new Date(Math.min(data.end ? new Date(data.end).getTime() : Infinity, Date.now()));
    this._load();
  }

  async _load() {
    if (!this._hass) return;
    const token = (this._loadToken = {});
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const prefs = await this._hass.callWS({ type: "energy/get_prefs" });
      const src = sourceStats(prefs);
      const devices = (prefs.device_consumption || []).map((d) => d.stat_consumption);
      const statIds = [...new Set([...devices, ...Object.values(src).flat()])];
      const start = new Date(this._start);
      start.setMinutes(0, 0, 0);
      const raw = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: this._end.toISOString(),
        statistic_ids: statIds,
        period: "hour",
        types: ["change", "mean"],
        units: { energy: "kWh" },
      });
      if (token !== this._loadToken) return;
      const changes = hourly(raw, "change");
      const means = src.price.length ? hourly(raw, "mean")[src.price[0]] || new Map() : new Map();
      const current = parseFloat(this._hass.states[src.price[0]]?.state);
      const fallbackPrice = this._config.price ?? src.fixedPrice ?? (Number.isFinite(current) ? current : 0);
      const priceOf = this._config.price != null ? () => this._config.price : (h) => means.get(h) ?? fallbackPrice;
      const nameOf = (id) => this._hass.states[id]?.attributes?.friendly_name || id;
      this._noPrice = !fallbackPrice && !means.size;
      this._tree = buildTree(prefs, changes, hourlyHouse(changes, src, priceOf), fallbackPrice, nameOf);
    } catch (err) {
      if (token !== this._loadToken) return;
      this._error = err?.message || String(err);
    }
    this._loading = false;
    this._render();
  }

  _setPreset(preset) {
    this._preset = preset;
    if (preset !== "custom") {
      [this._start, this._end] = presetRange(preset);
      this._load();
    } else this._render();
  }

  _setCustom() {
    const from = this.shadowRoot.querySelector("#from")?.value;
    const to = this.shadowRoot.querySelector("#to")?.value;
    if (!from || !to) return;
    this._start = fromDateInput(from);
    this._end = new Date(Math.min(addDays(fromDateInput(to), 1).getTime(), Date.now()));
    if (this._end > this._start) this._load();
  }

  _money(value) {
    // Symbol always after the value, whatever the HA locale's currency placement.
    const currency = this._hass?.config?.currency || "EUR";
    const symbol = currency === "EUR" ? "€" : currency;
    const number = value.toLocaleString(this._language, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `${number} ${symbol}`;
  }

  _label(n) {
    return n.untracked ? this._t.rest(n.name ?? this._t.house) : n.name;
  }

  _row(n, label, cls = "") {
    const t = this._t;
    const solarPct = Math.round(solarShare(n) * 100);
    const tip = t.tip(label, fmt(n.total), fmt(n.grid), fmt(n.total - n.grid));
    const open = n.id === this._openId;
    const start = new Date(this._start).setMinutes(0, 0, 0);
    const chart = open
      ? `<div class="chart-wrap">${powerChart(n.series, n.base, start, this._end.getTime(), this._language, t.baseLoad)}</div>`
      : "";
    return `
      <div class="row clickable ${cls} ${n.untracked ? "untracked" : ""} ${open ? "open" : ""}" data-id="${escapeHtml(n.id)}" title="${escapeHtml(tip)}">
        <div class="name">${escapeHtml(label)}</div>
        <div class="num cost">${this._money(n.cost)}</div>
        <div class="num saved">${this._money(n.saved)}</div>
        <div class="num pct">${solarPct} %</div>
        <div class="num base">${Math.round(n.base)} W</div>
      </div>${chart}`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const t = this._t;
    const title = this._config?.title ?? t.title;
    const presets = PRESETS.map(
      (key) => `<button class="preset ${key === this._preset ? "active" : ""}" data-preset="${key}">${t.presets[key]}</button>`,
    ).join("");
    const custom =
      this._preset === "custom"
        ? `<div class="custom">
            <input type="date" id="from" value="${toDateInput(this._start)}">
            <input type="date" id="to" value="${toDateInput(addDays(this._end, this._end.getHours() || this._end.getMinutes() ? 0 : -1))}">
            <button id="apply">OK</button>
          </div>`
        : "";
    let body;
    if (this._error) body = `<div class="msg error">${escapeHtml(this._error)}</div>`;
    else if (!this._tree) body = `<div class="msg">${t.loading}</div>`;
    else {
      const minKwh = this._config.min_kwh ?? 0.1;
      const [key, value] = COLUMNS.find(([k]) => k === this._sort.key);
      const dir = this._sort.desc ? -1 : 1;
      const rows = leaves(this._tree)
        .filter((n) => n.total >= minKwh)
        .map((n) => [n, this._label(n)])
        .sort(([a, la], [b, lb]) => dir * (key === "name" ? la.localeCompare(lb, this._language) : value(a) - value(b)));
      const head = COLUMNS.map(([k, , cls, icon]) => {
        const label = t.columns[k];
        const arrow = k === this._sort.key ? `<ha-icon class="arrow" icon="mdi:arrow-${this._sort.desc ? "down" : "up"}"></ha-icon>` : "";
        const content = icon ? `<ha-icon icon="${icon}"></ha-icon>` : label;
        return `<div class="${cls} sortable ${k === this._sort.key ? "sorted" : ""}" data-sort="${k}" title="${label}">${arrow}${content}</div>`;
      }).join("");
      body = `
        ${this._noPrice ? `<div class="msg error">${t.noPrice}</div>` : ""}
        <div class="table">
          <div class="row head">${head}</div>
          ${rows.map(([n, label]) => this._row(n, label)).join("")}
          ${this._row(this._tree, t.total, "total")}
        </div>`;
    }

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        .header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
        .title { font-size: 1.2em; font-weight: 500; }
        .presets { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
        button { font: inherit; font-size: 0.85em; padding: 4px 10px; border-radius: 12px; cursor: pointer;
          border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
        button.active { background: var(--primary-color); color: var(--text-primary-color); border-color: var(--primary-color); }
        .custom { display: flex; gap: 4px; margin-bottom: 8px; align-items: center; }
        .custom input { font: inherit; color: var(--primary-text-color); background: none; border: 1px solid var(--divider-color); border-radius: 4px; padding: 2px 4px; }
        /* Rows share the table's columns (subgrid) so numeric columns size to their widest value. */
        .table { display: grid; grid-template-columns: minmax(0, 1fr) repeat(4, max-content); column-gap: 12px; }
        .row { display: grid; grid-column: 1 / -1; grid-template-columns: subgrid; align-items: center; padding: 4px 0; border-bottom: 1px solid var(--divider-color); }
        .row.head { color: var(--secondary-text-color); font-size: 0.8em; }
        .sortable { cursor: pointer; user-select: none; }
        .sortable:hover, .sorted { color: var(--primary-text-color); }
        .sortable ha-icon { --mdc-icon-size: 18px; vertical-align: middle; }
        .sortable ha-icon.arrow { --mdc-icon-size: 12px; }
        .row.total { font-weight: 600; border-bottom: none; border-top: 2px solid var(--divider-color); }
        .clickable { cursor: pointer; }
        .clickable:hover, .row.open { background: var(--secondary-background-color); }
        .chart-wrap { grid-column: 1 / -1; padding: 4px 0 8px; border-bottom: 1px solid var(--divider-color); }
        .chart { width: 100%; height: auto; display: block; }
        .chart text { font-size: 10px; fill: var(--secondary-text-color); }
        .chart .tick { stroke: var(--divider-color); stroke-width: 1; }
        .chart .area { fill: var(--primary-color); fill-opacity: 0.25; stroke: var(--primary-color); stroke-width: 1.2; }
        .chart .base { stroke: var(--error-color, #db4437); stroke-width: 1.2; stroke-dasharray: 5 4; }
        .chart .base-label { fill: var(--error-color, #db4437); }
        .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        ha-icon { --mdc-icon-size: 18px; }
        .untracked .name { font-style: italic; color: var(--secondary-text-color); }
        .num { text-align: right; font-variant-numeric: tabular-nums; font-size: 0.9em; }
        .cost { color: var(--energy-grid-consumption-color, #488fc2); }
        .saved { color: var(--energy-solar-color, #ff9800); }
        .pct { color: var(--secondary-text-color); }
        .msg { color: var(--secondary-text-color); padding: 8px 0; }
        .error { color: var(--error-color); }
        .loading { opacity: 0.5; }
      </style>
      <ha-card>
        <div class="header">
          <div class="title">${escapeHtml(title)}</div>
          <button id="refresh" title="${t.refresh}"><ha-icon icon="mdi:refresh"></ha-icon></button>
        </div>
        ${this._mode === "local" ? `<div class="presets">${presets}</div>${custom}` : ""}
        <div class="${this._loading ? "loading" : ""}">${body}</div>
      </ha-card>`;

    const root = this.shadowRoot;
    root.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => this._setPreset(b.dataset.preset)));
    root.querySelectorAll(".row.clickable").forEach((el) =>
      el.addEventListener("click", () => {
        this._openId = el.dataset.id === this._openId ? null : el.dataset.id;
        this._render();
      }),
    );
    root.querySelectorAll(".sortable").forEach((el) =>
      el.addEventListener("click", () => {
        const k = el.dataset.sort;
        this._sort = k === this._sort.key ? { key: k, desc: !this._sort.desc } : { key: k, desc: k !== "name" };
        this._render();
      }),
    );
    root.querySelector("#refresh")?.addEventListener("click", () => {
      if (this._mode === "linked") return this._linkedPeriod && this._setPeriod(this._linkedPeriod);
      if (this._preset !== "custom") [this._start, this._end] = presetRange(this._preset);
      this._load();
    });
    root.querySelector("#apply")?.addEventListener("click", () => this._setCustom());
  }
}

// A leftover manual /local/ copy of this card would otherwise throw on the second define.
if (!customElements.get(CARD_TAG)) {
  customElements.define(CARD_TAG, EnergySplitCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TAG,
    name: "Energy split card",
    description: "Solar versus grid share and cost of each Energy dashboard device over a period",
  });
}
