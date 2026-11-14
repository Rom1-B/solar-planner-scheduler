/**
 * energy-stats-graph-card
 *
 * Long-term statistics graph following the energy-date-selection period, drawn with HA's own
 * chart element (ha-chart-base) and the Energy cards' options so it looks like them: meter series
 * (state with a sum, e.g. kWh) as bars stacked per unit, measurement series (e.g. temperatures)
 * as lines. Up to two units: the second one gets its own hidden scale, its values read in the tooltip.
 *
 * ha-chart-base is not a public API: its .data/.options/.height properties follow the 2026.9
 * frontend (echarts series and options).
 *
 * type: custom:energy-stats-graph-card
 *   title: <optional>
 *   collection_key: <optional, same value as the energy-date-selection card's collection_key>
 *   period: <optional: auto | hour | day | month, defaults to auto>
 *   fallback_period: <optional, without energy-date-selection: today | yesterday | 7d | 30d | month, defaults to 7d>
 *   series:
 *     - entity: <statistic id>
 *       name: <optional>
 *       stat: <optional: change | mean | min | max, defaults to change for meters, mean otherwise>
 *       type: <optional: bar | line, defaults to bar for change, line otherwise>
 *       color: <optional hex color>
 *       dashed: <optional, true draws a line series dashed>
 */

const CARD_TAG = "energy-stats-graph-card";

const STRINGS = {
  en: {
    presets: { today: "Today", yesterday: "Yesterday", "7d": "7 days", "30d": "30 days", month: "Month" },
    loading: "Loading",
    noSeries: "Add at least one series.",
    noChart: "Home Assistant's chart component could not be loaded.",
    tooManyUnits: (units) => `At most two units per card, found: ${units}.`,
    total: (v) => `Total: ${v}`,
  },
  fr: {
    presets: { today: "Aujourd'hui", yesterday: "Hier", "7d": "7 jours", "30d": "30 jours", month: "Mois" },
    loading: "Chargement",
    noSeries: "Ajouter au moins une série.",
    noChart: "Le composant graphique de Home Assistant n'a pas pu être chargé.",
    tooManyUnits: (units) => `Deux unités au maximum par carte, trouvées : ${units}.`,
    total: (v) => `Total : ${v}`,
  },
};

export const strings = (language) => STRINGS[String(language || "").split("-")[0]] || STRINGS.en;

const PRESETS = ["today", "yesterday", "7d", "30d", "month"];
const PALETTE = ["#488fc2", "#ff9800", "#43a047", "#e53935", "#8e24aa", "#00acc1", "#6d4c41", "#f06292"];

// The energy-date-selection card may be created after this one: retry this many times before falling back to local presets.
const LINK_RETRIES = 10;
const LINK_RETRY_MS = 300;
const CHART_TAG = "ha-chart-base";
const CHART_LOAD_TIMEOUT_MS = 10000;

const HOUR = 3600e3;
const DAY = 24 * HOUR;

/** Shared energy period collection, keyed like the HA frontend's getEnergyDataCollection. */
export function energyCollection(hass, key) {
  if (key) return hass.connection[`_${key}`];
  return (hass.panelUrl && hass.connection[`_energy_${hass.panelUrl}`]) || hass.connection._energy;
}

/** ha-chart-base is lazy-loaded by the native chart cards: build a hidden statistics-graph to pull it in if needed. */
let chartReady;
function loadChart(hass) {
  chartReady ??= (async () => {
    if (customElements.get(CHART_TAG)) return true;
    const helpers = await window.loadCardHelpers?.();
    const card = await helpers?.createCardElement({ type: "statistics-graph", entities: [] });
    if (card) {
      card.hass = hass;
      card.style.display = "none";
      document.body.appendChild(card);
    }
    const timeout = new Promise((resolve) => setTimeout(() => resolve(false), CHART_LOAD_TIMEOUT_MS));
    const ok = await Promise.race([customElements.whenDefined(CHART_TAG).then(() => true), timeout]);
    card?.remove();
    return ok;
  })();
  return chartReady;
}

const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);

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

/** Statistics period for a range, mirroring the energy dashboard's hour / day / month steps. */
export function autoPeriod(start, end) {
  const span = end - start;
  if (span <= 2 * DAY) return "hour";
  if (span <= 62 * DAY) return "day";
  return "month";
}

const floorTo = (d, period) =>
  period === "hour"
    ? new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours())
    : period === "day"
      ? startOfDay(d)
      : new Date(d.getFullYear(), d.getMonth(), 1);

const nextBucket = (d, period) =>
  period === "hour"
    ? new Date(d.getTime() + HOUR)
    : period === "day"
      ? addDays(d, 1)
      : new Date(d.getFullYear(), d.getMonth() + 1, 1);

/** Bucket starts (ms) covering [start, end), plus any start present in the data. */
export function buckets(start, end, period, rows = []) {
  const set = new Set();
  for (let d = floorTo(start, period); d < end; d = nextBucket(d, period)) set.add(d.getTime());
  for (const t of rows) set.add(t);
  return [...set].sort((a, b) => a - b);
}

/** Resolved series: statistic, chart type and unit from the recorder metadata. */
export function resolveSeries(config, metadata, nameOf) {
  const meta = Object.fromEntries(metadata.map((m) => [m.statistic_id, m]));
  return (config.series || []).map((s, i) => {
    const m = meta[s.entity] || {};
    const stat = s.stat || (m.has_sum ? "change" : "mean");
    return {
      id: s.entity,
      index: i,
      name: s.name || m.name || nameOf(s.entity),
      stat,
      type: s.type || (stat === "change" ? "bar" : "line"),
      color: s.color || PALETTE[i % PALETTE.length],
      dashed: !!s.dashed,
      unit: m.unit_class === "energy" ? "kWh" : m.statistics_unit_of_measurement || "",
    };
  });
}

/**
 * Time axis bounds of the native Energy charts: hourly views end mid last hour, daily and monthly
 * ones at the start of the last day or month, months padded by a few days on both sides.
 */
const MONTH_PADDING_DAYS = 5;
export function axisBounds(period, start, end) {
  let max = new Date(end);
  if (period === "hour") max.setMinutes(30, 0, 0);
  else {
    if (max.getHours() === 0) max = addDays(max, -1);
    max.setHours(0, 0, 0, 0);
    if (period === "month") max.setDate(1);
  }
  if (period !== "month") return { min: new Date(start), max };
  return { min: addDays(start, -MONTH_PADDING_DAYS), max: addDays(max, MONTH_PADDING_DAYS) };
}

/**
 * echarts series for ha-chart-base: bars stacked per unit, lines broken on missing buckets. Points are
 * placed like the native Energy charts (mid-hour for hourly data, bucket start otherwise), with the
 * bucket start as third value for the tooltip.
 */
export function chartSeries(series, values, times, period) {
  const x = (t) => (period === "hour" ? t + HOUR / 2 : t);
  const units = [...new Set(series.map((s) => s.unit))];
  return series.map((s) => {
    const v = values[s.id] || new Map();
    const common = { id: s.id, name: s.name, yAxisIndex: units.indexOf(s.unit) };
    if (s.type === "bar") {
      return {
        ...common,
        type: "bar",
        stack: `bar-${s.unit}`,
        barMaxWidth: 50,
        color: `${s.color}7F`,
        itemStyle: { borderColor: s.color },
        // Every bucket kept so stacks line up by index; a zero becomes null, or its border would draw a line on the stack.
        data: times.map((t) => [x(t), v.get(t) || null, t]),
      };
    }
    return {
      ...common,
      type: "line",
      color: s.color,
      showSymbol: false,
      lineStyle: { width: 1.5, type: s.dashed ? "dashed" : "solid" },
      data: times.map((t) => [x(t), v.get(t) ?? null, t]),
    };
  });
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class EnergyStatsGraphCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._preset = this._config.fallback_period || "7d";
    [this._start, this._end] = presetRange(this._preset);
    this._data = null;
    this._error = null;
    this._loading = false;
    this._unlink();
    this._mode = "pending"; // pending | linked | local
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    if (this._hass && this.isConnected) this._link();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._chart) this._chart.hass = hass;
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
    return 5;
  }

  static getStubConfig() {
    return { series: [] };
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
    this._start = new Date(data.start);
    // Kept unclipped: like the native cards, the axis runs to the period end, the future part empty.
    this._end = data.end ? new Date(data.end) : new Date();
    this._load();
  }

  async _load() {
    if (!this._hass) return;
    const ids = (this._config.series || []).map((s) => s.entity).filter(Boolean);
    if (!ids.length) {
      this._error = this._t.noSeries;
      return this._render();
    }
    const token = (this._loadToken = {});
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const [chartOk, metadata] = await Promise.all([
        loadChart(this._hass),
        this._hass.callWS({ type: "recorder/get_statistics_metadata", statistic_ids: ids }),
      ]);
      if (!chartOk) throw new Error(this._t.noChart);
      const period = !this._config.period || this._config.period === "auto" ? autoPeriod(this._start, this._end) : this._config.period;
      const start = floorTo(this._start, period);
      const raw = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: new Date(Math.min(this._end.getTime(), Date.now())).toISOString(),
        statistic_ids: ids,
        period,
        types: ["change", "mean", "min", "max"],
        units: { energy: "kWh" },
      });
      if (token !== this._loadToken) return;
      const series = resolveSeries(this._config, metadata, (id) => this._hass.states[id]?.attributes?.friendly_name || id);
      const units = [...new Set(series.map((s) => s.unit))];
      if (units.length > 2) throw new Error(this._t.tooManyUnits(units.join(", ")));
      const values = {};
      const starts = [];
      for (const s of series) {
        const m = new Map();
        for (const r of raw[s.id] || []) {
          const t = typeof r.start === "number" ? r.start : Date.parse(r.start);
          if (r[s.stat] != null) m.set(t, r[s.stat]);
          starts.push(t);
        }
        values[s.id] = m;
      }
      this._data = { series, values, units, period, start, end: this._end, times: buckets(start, this._end, period, starts) };
    } catch (err) {
      if (token !== this._loadToken) return;
      this._error = err?.message || String(err);
    }
    this._loading = false;
    this._render();
  }

  _setPreset(preset) {
    this._preset = preset;
    [this._start, this._end] = presetRange(preset);
    this._load();
  }

  /** Number locale from the HA profile's number format, like the native cards. */
  get _numberLocale() {
    const locale = this._hass?.locale;
    const formats = { comma_decimal: "en-US", decimal_comma: "de-DE", space_comma: "fr-FR", system: undefined };
    return locale?.number_format in formats ? formats[locale.number_format] : this._language;
  }

  /** `digits` fixes the decimals (axis labels, like the native "1,0"); otherwise they shrink as the value grows. */
  _num(v, digits) {
    const abs = Math.abs(v);
    const options =
      digits != null
        ? { minimumFractionDigits: digits, maximumFractionDigits: digits }
        : { maximumFractionDigits: abs >= 100 ? 0 : abs >= 10 ? 1 : 2 };
    if (this._hass?.locale?.number_format === "none") options.useGrouping = false;
    return v.toLocaleString(this._numberLocale, options);
  }

  _tooltipTitle(t, period) {
    const d = new Date(t);
    const lang = this._language;
    if (period === "month") return d.toLocaleDateString(lang, { month: "long", year: "numeric" });
    if (period === "day") return d.toLocaleDateString(lang, { weekday: "short", day: "numeric", month: "long" });
    const time = (x) => x.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
    return `${d.toLocaleDateString(lang, { weekday: "short", day: "numeric", month: "short" })} ${time(d)} – ${time(new Date(t + HOUR))}`;
  }

  /**
   * Tooltip like the Energy cards': centered date, one line per series, stacked total when several bars.
   * Returned as an element: ha-chart-base renders the formatter result with lit, which escapes strings.
   */
  _tooltip(params) {
    const list = (Array.isArray(params) ? params : [params]).filter((p) => p.value?.[1] != null);
    if (!list.length) return "";
    const el = document.createElement("div");
    el.innerHTML = this._tooltipHtml(list);
    return el;
  }

  _tooltipHtml(list) {
    const { series, period } = this._data;
    const unitOf = new Map(series.map((s) => [s.id, s.unit]));
    const totals = new Map();
    const bars = new Map();
    const rows = list.map((p) => {
      const unit = unitOf.get(p.seriesId) || "";
      if (p.componentSubType === "bar") {
        totals.set(unit, (totals.get(unit) || 0) + p.value[1]);
        bars.set(unit, (bars.get(unit) || 0) + 1);
      }
      return `${p.marker} ${escapeHtml(p.seriesName)}: <div style="direction:ltr;display:inline">${this._num(p.value[1])} ${escapeHtml(unit)}</div>`;
    });
    // A total only for a unit whose stack has several bars at this time.
    const total = [...totals]
      .filter(([unit]) => bars.get(unit) > 1)
      .map(([unit, v]) => `<br/><b>${this._t.total(`${this._num(v)} ${escapeHtml(unit)}`)}</b>`)
      .join("");
    return `<h4 style="text-align:center;margin:0">${this._tooltipTitle(list[0].value[2], period)}</h4>${rows.join("<br/>")}${total}`;
  }

  _options() {
    const { units, series, start, end, period } = this._data;
    const hasBars = (unit) => series.some((s) => s.unit === unit && s.type === "bar");
    return {
      xAxis: { type: "time", ...axisBounds(period, start, end) },
      // The second unit's axis only scales its series: hidden so the plot lines up with the other Energy cards.
      yAxis: units.map((unit, i) => ({
        type: "value",
        show: i === 0,
        name: unit,
        nameGap: 2,
        nameTextStyle: { align: "left" },
        // Bars start at zero like the Energy cards; lines (temperatures) fit their range.
        scale: !hasBars(unit),
        axisLabel: { formatter: (v) => this._num(v, Number.isInteger(v) ? 0 : 1) },
        splitLine: { show: i === 0 },
      })),
      grid: { top: 15, bottom: 0, left: 1, right: 1, containLabel: true },
      legend: { show: true, type: "custom" },
      tooltip: { trigger: "axis", formatter: (params) => this._tooltip(params) },
    };
  }

  /** Total of the bar series over the period, per unit, like the Energy cards' header chip. */
  _totals() {
    const totals = new Map();
    for (const s of this._data.series.filter((x) => x.type === "bar")) {
      let sum = 0;
      for (const v of this._data.values[s.id]?.values() || []) sum += v;
      totals.set(s.unit, (totals.get(s.unit) || 0) + sum);
    }
    return [...totals].map(([unit, v]) => `${this._num(v)} ${unit}`);
  }

  _skeleton() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { height: 100%; }
        .card-header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 0; }
        .content { padding: 16px; }
        .has-header { padding-top: 0; }
        .chips { display: flex; gap: 4px; }
        .chip { font-size: var(--ha-font-size-m, 14px); font-weight: var(--ha-font-weight-medium, 500); padding: var(--ha-space-1, 4px) var(--ha-space-2, 8px);
          border-radius: var(--ha-border-radius-md, 8px); border: 1px solid var(--divider-color); }
        .presets { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
        .presets:empty { display: none; }
        button { font: inherit; font-size: 0.85em; padding: 4px 10px; border-radius: 12px; cursor: pointer;
          border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
        button.active { background: var(--primary-color); color: var(--text-primary-color); border-color: var(--primary-color); }
        .msg { color: var(--secondary-text-color); padding: 8px 0; }
        .msg:empty { display: none; }
        .error { color: var(--error-color); }
        .chart { height: 300px; }
        .chart[hidden] { display: none; }
        .loading { opacity: 0.5; }
      </style>
      <ha-card>
        <div class="card-header"><span class="title"></span><div class="chips"></div></div>
        <div class="content">
          <div class="presets"></div>
          <div class="msg"></div>
          <div class="chart" hidden></div>
        </div>
      </ha-card>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this.shadowRoot.querySelector("ha-card")) this._skeleton();
    const root = this.shadowRoot;
    const t = this._t;
    const title = this._config?.title || "";
    root.querySelector(".card-header").hidden = !title && !this._data;
    root.querySelector(".content").classList.toggle("has-header", !!title || !!this._data);
    root.querySelector(".title").textContent = title;
    root.querySelector(".chips").innerHTML = this._data && !this._error ? this._totals().map((v) => `<div class="chip">${escapeHtml(v)}</div>`).join("") : "";

    const presets = root.querySelector(".presets");
    presets.innerHTML =
      this._mode === "local"
        ? PRESETS.map((key) => `<button class="${key === this._preset ? "active" : ""}" data-preset="${key}">${t.presets[key]}</button>`).join("")
        : "";
    presets.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => this._setPreset(b.dataset.preset)));

    const msg = root.querySelector(".msg");
    msg.className = `msg ${this._error ? "error" : ""}`;
    msg.textContent = this._error || (!this._data ? t.loading : "");

    const wrap = root.querySelector(".chart");
    wrap.hidden = !this._data || !!this._error;
    wrap.classList.toggle("loading", this._loading);
    if (!this._data || this._error || !customElements.get(CHART_TAG)) return;
    const isNew = !this._chart;
    if (isNew) {
      this._chart = document.createElement(CHART_TAG);
      this._chart.height = "100%";
      this._chart.style.height = "100%";
    }
    // hass and options must be set before the element connects: it builds its chart on connection.
    this._chart.hass = this._hass;
    this._chart.options = this._options();
    this._chart.data = chartSeries(this._data.series, this._data.values, this._data.times, this._data.period);
    if (isNew) wrap.appendChild(this._chart);
  }
}

// A second copy of this card (e.g. /local/ and bundled) would otherwise throw on the second define.
if (!customElements.get(CARD_TAG)) {
  customElements.define(CARD_TAG, EnergyStatsGraphCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TAG,
    name: "Energy stats graph card",
    description: "Statistics graph following the energy date picker: stacked meter bars and measurement lines on two axes",
  });
}
