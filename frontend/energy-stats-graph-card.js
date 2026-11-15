/**
 * energy-stats-graph-card
 *
 * Extends HA's native "Individual devices detail" Energy card (hui-energy-devices-detail-graph-card)
 * so the period, steps, axis, compare modes (previous period, previous year), bar colors, tooltip and
 * legend are the native ones: the native card computes the bars from the shared energy period, fed
 * with only the configured meters. Meters missing from the Energy dashboard get their statistics
 * fetched here and injected the same way. Measurement series (temperature, humidity) are added as
 * lines on a hidden second scale, compare included, their values read in the tooltip.
 *
 * Relies on the native card's internals (_data, _processStatistics, _createOptions, _chartData,
 * _legendData, _hiddenStats), as of the 2026.9 frontend: a frontend update may require a card update.
 *
 * type: custom:energy-stats-graph-card
 *   title: <optional>
 *   collection_key: <optional, same value as the energy-date-selection card's collection_key>
 *   expand_legend: <optional, native option>
 *   series:
 *     - entity: <statistic id>
 *       name: <optional>
 *       type: <optional: bar | line, defaults to bar for energy meters, line otherwise>
 *       stat: <optional for lines: change | mean | min | max, defaults to change for meters, mean otherwise>
 *       color: <optional hex color, lines only: bars take the native Energy palette>
 *       dashed: <optional, true draws a line series dashed>
 */

const CARD_TAG = "energy-stats-graph-card";
const BASE_TAG = "hui-energy-devices-detail-graph-card";
const BASE_TYPE = "energy-devices-detail-graph";
const PALETTE = ["#e53935", "#43a047", "#8e24aa", "#00acc1", "#6d4c41", "#f06292"];
const HOUR = 3600e3;
const DAY = 24 * HOUR;

// Date helpers mirroring the date-fns calls of the native frontend (local time).
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n, d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds());
const addMonths = (d, n) => {
  const r = new Date(d.getFullYear(), d.getMonth() + n, 1, d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds());
  r.setDate(Math.min(d.getDate(), new Date(r.getFullYear(), r.getMonth() + 1, 0).getDate()));
  return r;
};
const timeOfDay = (d) => d - startOfDay(d);
export function differenceInDays(a, b) {
  const calendar = Math.round((Date.UTC(a.getFullYear(), a.getMonth(), a.getDate()) - Date.UTC(b.getFullYear(), b.getMonth(), b.getDate())) / DAY);
  // Full days only: a later time of day on the earlier date removes the incomplete day.
  if (calendar > 0 && timeOfDay(a) < timeOfDay(b)) return calendar - 1;
  if (calendar < 0 && timeOfDay(a) > timeOfDay(b)) return calendar + 1;
  return calendar;
}
export function differenceInMonths(a, b) {
  const months = (a.getFullYear() - b.getFullYear()) * 12 + a.getMonth() - b.getMonth();
  const rest = (x) => x - new Date(x.getFullYear(), x.getMonth(), 1);
  if (months > 0 && rest(a) < rest(b)) return months - 1;
  if (months < 0 && rest(a) > rest(b)) return months + 1;
  return months;
}
const differenceInYears = (a, b) => Math.trunc(differenceInMonths(a, b) / 12);
const isStartOfYear = (d) => d.getTime() === new Date(d.getFullYear(), 0, 1).getTime();
const isStartOfMonth = (d) => d.getTime() === new Date(d.getFullYear(), d.getMonth(), 1).getTime();
const isStartOfDay = (d) => d.getTime() === startOfDay(d).getTime();
const isFirstDayOfMonth = (d) => d.getDate() === 1;
const isLastDayOfMonth = (d) => d.getDate() === new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();

/** The native energy step for a period (getSuggestedPeriod without 5-minute data). */
export function suggestedPeriod(start, end) {
  const days = differenceInDays(end || new Date(), start);
  if (isFirstDayOfMonth(start) && (!end || isLastDayOfMonth(end)) && days > 35) return "month";
  return days > 2 ? "day" : "hour";
}

/** Maps a compare-period date onto the current period's axis, like the native compare charts. */
export function compareShift(start, compareStart) {
  if (!compareStart) return (d) => d;
  const days = differenceInDays(start, compareStart);
  const years = differenceInYears(start, compareStart);
  const sameDayOr = (shifted, d) => (shifted.getDate() === d.getDate() ? shifted : addDays(d, days));
  if (years !== 0 && isStartOfYear(start)) return (d) => sameDayOr(addMonths(d, 12 * years), d);
  const months = differenceInMonths(start, compareStart);
  if (months !== 0 && isStartOfMonth(start)) return (d) => sameDayOr(addMonths(d, months), d);
  if (days !== 0 && isStartOfDay(start)) return (d) => addDays(d, days);
  const ms = start - compareStart;
  return (d) => new Date(d.getTime() + ms);
}

/** Native bar position: hourly points sit mid-hour, other steps at their start. */
export const pointOffset = (period) => (period === "hour" ? HOUR / 2 : 0);

/** Resolved series: bar (native, energy meters only) or line, with statistic, unit and color. */
export function resolveSeries(config, metadata, nameOf) {
  const meta = Object.fromEntries(metadata.map((m) => [m.statistic_id, m]));
  let lineIndex = 0;
  return (config.series || []).map((s) => {
    const m = meta[s.entity] || {};
    const energy = m.unit_class === "energy";
    const type = s.type === "line" || !energy || !m.has_sum ? "line" : "bar";
    const stat = type === "bar" ? "change" : s.stat || (m.has_sum ? "change" : "mean");
    return {
      id: s.entity,
      name: s.name || m.name || nameOf(s.entity),
      customName: s.name,
      type,
      stat,
      dashed: !!s.dashed,
      color: s.color || (type === "line" ? PALETTE[lineIndex++ % PALETTE.length] : undefined),
      unit: energy ? "kWh" : m.statistics_unit_of_measurement || "",
    };
  });
}

/**
 * Energy data for the native computation: every Energy dashboard device kept in place (native colors
 * follow the device position, native names its label), meters outside it appended, but statistics
 * only for the bar series, so the others come out empty; no sources, so no "untracked" series.
 */
export function restrictEnergyData(data, bars, extra) {
  const wanted = new Map(bars.map((s) => [s.id, s]));
  const known = new Set(data.prefs.device_consumption.map((d) => d.stat_consumption));
  const named = (d) => (wanted.get(d.stat_consumption)?.customName ? { ...d, name: wanted.get(d.stat_consumption).customName } : d);
  const pick = (stats, more) =>
    Object.fromEntries(bars.filter((s) => stats?.[s.id] || more?.[s.id]).map((s) => [s.id, stats?.[s.id] || more[s.id]]));
  return {
    ...data,
    prefs: {
      ...data.prefs,
      energy_sources: [],
      device_consumption: [
        ...data.prefs.device_consumption.map(named),
        ...bars.filter((s) => !known.has(s.id)).map((s) => ({ stat_consumption: s.id, name: s.name })),
      ],
    },
    stats: pick(data.stats, extra?.stats),
    statsCompare: data.statsCompare ? pick(data.statsCompare, extra?.statsCompare) : data.statsCompare,
  };
}

const rowTime = (r) => (typeof r.start === "number" ? r.start : Date.parse(r.start));

/** echarts line series (current and compare) for the measurement series. */
export function lineSeries(lines, extra, data, axisIndex) {
  if (!extra) return [];
  const offset = pointOffset(extra.period);
  const shift = compareShift(new Date(data.start), data.startCompare ? new Date(data.startCompare) : undefined);
  const out = [];
  for (const s of lines) {
    const common = { type: "line", name: s.name, yAxisIndex: axisIndex, showSymbol: false, smooth: false };
    const style = (alpha) => ({ width: 1.5, type: s.dashed ? "dashed" : "solid", color: s.color + alpha });
    const points = (rows, map) =>
      (rows || []).filter((r) => r[s.stat] != null).map((r) => [map(new Date(rowTime(r))).getTime() + offset, r[s.stat], rowTime(r)]);
    if (data.statsCompare && extra.statsCompare) {
      out.push({ ...common, id: `compare-${s.id}`, color: `${s.color}7F`, lineStyle: style("7F"), data: points(extra.statsCompare[s.id], shift) });
    }
    out.push({ ...common, id: s.id, color: s.color, lineStyle: style(""), data: points(extra.stats[s.id], (d) => d) });
  }
  return out;
}

/** Defines the card class on top of the native one; exported for tests with a stand-in base. */
export function defineCard(Base) {
  class EnergyStatsGraphCard extends Base {
    static getStubConfig() {
      return { series: [] };
    }

    // The native editor would only edit the native options: YAML only.
    static async getConfigElement() {
      return undefined;
    }

    constructor() {
      super();
      const nativeOptions = this._createOptions;
      let cache = {};
      this._createOptions = (...args) => {
        const options = nativeOptions(...args);
        const lines = this._lineCount ?? 0;
        if (cache.options !== options || cache.lines !== lines) cache = { options, lines, result: this._extendOptions(options) };
        return cache.result;
      };
    }

    setConfig(config) {
      if (!Array.isArray(config?.series) || !config.series.length) throw new Error("energy-stats-graph-card: add at least one series");
      super.setConfig(config);
      this._series = null;
      this._extra = null;
    }

    willUpdate(changed) {
      // The config can arrive before hass: metadata loads on the first update that has both.
      if (this.hass && this._config && (changed.has("_config") || (!this._series && this._metaFor !== this._config))) this._loadMetadata();
      if (changed.has("_data") && this._data && this._series) this._loadExtra(this._data);
      super.willUpdate(changed);
      if (changed.has("_series") || changed.has("_extra")) this._processStatistics();
    }

    async _loadMetadata() {
      const config = this._config;
      this._metaFor = config;
      const ids = config.series.map((s) => s.entity);
      const metadata = await this.hass.callWS({ type: "recorder/get_statistics_metadata", statistic_ids: ids });
      if (config !== this._config) return;
      const old = this._series;
      this._series = resolveSeries(config, metadata, (id) => this.hass.states[id]?.attributes?.friendly_name || id);
      this.requestUpdate("_series", old);
      if (this._data) this._loadExtra(this._data);
    }

    /** Statistics the native data lacks: bars outside the Energy dashboard, and every line series. */
    async _loadExtra(data) {
      const period = suggestedPeriod(new Date(data.start), data.end ? new Date(data.end) : undefined);
      const missingBars = this._series.filter((s) => s.type === "bar" && !(s.id in (data.stats || {})));
      const lines = this._series.filter((s) => s.type === "line");
      const ids = [...missingBars, ...lines].map((s) => s.id);
      const fetch = (start, end) =>
        ids.length
          ? this.hass.callWS({
              type: "recorder/statistics_during_period",
              start_time: new Date(start).toISOString(),
              end_time: new Date(end || Date.now()).toISOString(),
              statistic_ids: ids,
              period,
              types: ["change", "mean", "min", "max"],
              units: { energy: "kWh" },
            })
          : Promise.resolve({});
      const [stats, statsCompare] = await Promise.all([fetch(data.start, data.end), data.startCompare ? fetch(data.startCompare, data.endCompare) : null]);
      if (data !== this._data) return;
      const old = this._extra;
      this._extra = { data, period, stats, statsCompare };
      this.requestUpdate("_extra", old);
    }

    _processStatistics() {
      const real = this._data;
      if (!real || !this._series) return;
      const extra = this._extra?.data === real ? this._extra : null;
      const bars = this._series.filter((s) => s.type === "bar");
      const lines = this._series.filter((s) => s.type === "line");
      // Shadows the reactive accessor for the native computation only, without triggering an update.
      const own = Object.getOwnPropertyDescriptor(this, "_data");
      Object.defineProperty(this, "_data", { value: restrictEnergyData(real, bars, extra), configurable: true, writable: true });
      try {
        super._processStatistics();
      } finally {
        if (own) Object.defineProperty(this, "_data", own);
        else delete this._data;
      }
      // Only the configured bars: other devices were passed for their colors and come out empty.
      const keep = new Set(bars.map((s) => s.id));
      const isKept = (id) => id === "compare-placeholder" || keep.has(this._getStatIdFromId(String(id)));
      this._chartData = this._chartData.filter((series) => isKept(series.id));
      this._legendData = (this._legendData || []).filter((item) => isKept(item.id));
      const lineData = lineSeries(lines, extra, real, bars.length ? 1 : 0);
      this._lineCount = lineData.length;
      this._lineUnits = new Map(lines.map((s) => [s.id, s.unit]));
      this._chartData = [...this._chartData, ...lineData];
      this._legendData = [
        ...(this._legendData || []),
        ...lines.map((s) => ({
          id: s.id,
          secondaryIds: [`compare-${s.id}`],
          name: s.name,
          itemStyle: { color: s.color, borderColor: s.color },
          noLabelClick: !this.hass.states[s.id],
        })),
      ];
    }

    _extendOptions(options) {
      if (!this._lineCount) return options;
      const hasBars = this._series.some((s) => s.type === "bar");
      const base = Array.isArray(options.yAxis) ? options.yAxis[0] : options.yAxis;
      const lineUnit = this._series.find((s) => s.type === "line")?.unit;
      const yAxis = hasBars ? [base, { type: "value", show: false, scale: true }] : [{ ...base, name: lineUnit, scale: true }];
      const nativeFormatter = options.tooltip?.formatter;
      return { ...options, yAxis, tooltip: { ...options.tooltip, formatter: (params) => this._tooltip(params, nativeFormatter) } };
    }

    _numberLocale() {
      const locale = this.hass?.locale;
      const formats = { comma_decimal: "en-US", decimal_comma: "de-DE", space_comma: "fr-FR", system: undefined };
      return locale?.number_format in formats ? formats[locale.number_format] : locale?.language;
    }

    /**
     * Native tooltip for the bars of each period (current, then compare), each followed by its
     * measurement lines with their own unit; the native one would print every value in kWh.
     */
    _tooltip(params, nativeFormatter) {
      const list = Array.isArray(params) ? params : [params];
      const isCompare = (p) => String(p.seriesId).startsWith("compare-");
      const lines = list.filter((p) => p.componentSubType !== "bar" && p.value?.[1] != null);
      if (!lines.length) return nativeFormatter ? nativeFormatter(params) : "";
      const lang = this.hass?.locale?.language;
      const number = (v) => v.toLocaleString(this._numberLocale(), { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 });
      const groups = [false, true].map((compare) => {
        const bars = list.filter((p) => p.componentSubType === "bar" && isCompare(p) === compare);
        const own = lines.filter((p) => isCompare(p) === compare);
        if (!bars.length && !own.length) return null;
        const native = bars.length && nativeFormatter ? nativeFormatter(bars) : null;
        const el = document.createElement("div");
        const rows = own.map((p) => {
          const id = String(p.seriesId);
          const unit = this._lineUnits.get(compare ? id.slice(8) : id) || "";
          return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:4px"></span>${p.seriesName}: <div style="direction:ltr;display:inline">${number(p.value[1])} ${unit}</div>`;
        });
        const title = native ? "" : `<h4 style="text-align:center;margin:0">${new Date(own[0].value[2]).toLocaleString(lang, { dateStyle: "medium", timeStyle: "short" })}</h4>`;
        el.innerHTML = `${title}${native && rows.length ? "<br/>" : ""}${rows.join("<br/>")}`;
        return native ? [native, el] : [el];
      });
      const separator = () => Object.assign(document.createElement("div"), { innerHTML: "<br/>" });
      return groups.filter(Boolean).flatMap((group, i) => (i ? [separator(), ...group] : group));
    }
  }

  // The native card stores hidden series under a key shared with every devices-detail card.
  Object.defineProperty(EnergyStatsGraphCard.prototype, "_hiddenStats", {
    configurable: true,
    get() {
      return this.__hiddenStats ?? [];
    },
    set(value) {
      const old = this.__hiddenStats;
      this.__hiddenStats = value;
      this.requestUpdate?.("_hiddenStats", old);
    },
  });

  return EnergyStatsGraphCard;
}

/** The native card is lazy-loaded with the Energy cards: create one through the card helpers to pull it in. */
async function loadBase() {
  if (!customElements.get(BASE_TAG)) {
    const helpers = await window.loadCardHelpers?.();
    helpers?.createCardElement({ type: BASE_TYPE });
  }
  await customElements.whenDefined(BASE_TAG);
  return customElements.get(BASE_TAG);
}

if (typeof window !== "undefined" && window.loadCardHelpers && !customElements.get(CARD_TAG)) {
  loadBase().then((Base) => {
    if (!customElements.get(CARD_TAG)) customElements.define(CARD_TAG, defineCard(Base));
  });
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TAG,
    name: "Energy stats graph card",
    description: "Native Energy devices graph for chosen meters, with measurement lines (temperature, humidity) and compare",
  });
}
