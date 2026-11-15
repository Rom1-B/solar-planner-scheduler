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
 * With mode: cost, the bar meters are summed into two stacked native bars in currency: their grid cost
 * and their solar savings, from an hourly split of each meter's consumption by that hour's grid share
 * (grid import / house consumption) priced at the Energy dashboard's grid price, summed per step.
 *
 * type: custom:energy-stats-graph-card
 *   title: <optional>
 *   mode: <optional: energy | cost, defaults to energy>
 *   export: <optional, cost mode: true adds the grid export income (the Energy dashboard's compensation
 *            statistic, recorded from when an export price is set) as a negative series, and a dashed
 *            net bill line per period (grid cost - income)>
 *   mode_toggle: <optional, true shows a "kWh | €" switch in the card corner, starting from mode>
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

export const SOLAR_SUFFIX = "#solar";
export const COST_GRID = "cost_grid";
export const COST_SOLAR = "cost_solar";
export const COST_EXPORT = "cost_export";
export const COST_NET = "cost_net";
const COST_STRINGS = {
  en: { grid: "Grid", solar: "Solar", export: "Export", net: "Net bill", total: "Total" },
  fr: { grid: "Réseau", solar: "Solaire", export: "Revente", net: "Facture nette", total: "Total" },
};

/** Source statistic ids and grid price from the Energy preferences, flat (2025+) or flow_from/flow_to grid format. */
export function energySources(prefs) {
  const out = { solar: [], gridIn: [], gridOut: [], batteryOut: [], batteryIn: [], price: [], compensation: [] };
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
      if (src.stat_compensation) out.compensation.push(src.stat_compensation);
      for (const f of src.flow_from || []) {
        out.gridIn.push(f.stat_energy_from);
        if (f.entity_energy_price) out.price.push(f.entity_energy_price);
        if (f.number_energy_price != null) out.fixedPrice ??= f.number_energy_price;
      }
      for (const f of src.flow_to || []) {
        out.gridOut.push(f.stat_energy_to);
        if (f.stat_compensation) out.compensation.push(f.stat_compensation);
      }
    }
  }
  for (const k of Object.keys(out)) if (Array.isArray(out[k])) out[k] = out[k].filter(Boolean);
  out.price = out.price.slice(0, 1);
  out.compensation = out.compensation.slice(0, 1);
  return out;
}

/**
 * The export income statistic HA keeps, like its own cost cards: the configured stat_compensation, else
 * the "<export meter>_compensation" sensor it creates once an export price is set (never retroactive).
 */
export function compensationStat(src, states) {
  if (src.compensation[0]) return src.compensation[0];
  const auto = src.gridOut[0] && `${src.gridOut[0]}_compensation`;
  return auto && states?.[auto] ? auto : null;
}

/** Export income per step from the hourly compensation changes, negative like the native returned-energy bars. */
export function exportStats(changes, compensationId, period) {
  const sums = new Map();
  for (const [h, income] of changes[compensationId] || []) {
    const b = bucketStart(h, period);
    sums.set(b, (sums.get(b) || 0) - income);
  }
  return [...sums].sort((a, b) => a[0] - b[0]).map(([start, change]) => ({ start, end: nextBucket(start, period), change }));
}

/** Hourly values of one statistic type: { id: Map(hourStartMs -> value) }. */
export function hourlyValues(raw, type) {
  return Object.fromEntries(
    Object.entries(raw || {}).map(([id, rows]) => [id, new Map(rows.filter((r) => r[type] != null).map((r) => [rowTime(r), r[type]]))])
  );
}

/** Per hour: grid share of the house consumption in [0, 1] (energy-split-card allocation) and the grid price. */
export function hourlyShares(changes, src, priceOf) {
  const sum = (ids, h) => ids.reduce((acc, id) => acc + (changes[id]?.get(h) || 0), 0);
  const hours = new Set();
  for (const id of [...src.solar, ...src.gridIn]) for (const h of changes[id]?.keys() || []) hours.add(h);
  const shares = new Map();
  for (const h of hours) {
    const imp = sum(src.gridIn, h);
    const house = sum(src.solar, h) + imp - sum(src.gridOut, h) + sum(src.batteryOut, h) - sum(src.batteryIn, h);
    shares.set(h, { share: house > 0 ? Math.min(1, Math.max(0, imp / house)) : imp > 0 ? 1 : 0, price: priceOf(h) });
  }
  return shares;
}

const bucketStart = (t, period) => {
  const d = new Date(t);
  if (period === "hour") return new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours()).getTime();
  return (period === "day" ? startOfDay(d) : new Date(d.getFullYear(), d.getMonth(), 1)).getTime();
};
const nextBucket = (t, period) => {
  const d = new Date(t);
  if (period === "hour") return t + HOUR;
  return (period === "day" ? addDays(d, 1) : new Date(d.getFullYear(), d.getMonth() + 1, 1)).getTime();
};

/**
 * Grid cost and solar savings of each device summed per step of the period, as native statistic rows:
 * { id: rows, "id#solar": rows }. Hours without a known share count as grid.
 */
export function costStats(changes, shares, ids, period, fallbackPrice) {
  const out = {};
  for (const id of ids) {
    const grid = new Map();
    const solar = new Map();
    for (const [h, kwh] of changes[id] || []) {
      const share = shares.get(h)?.share ?? 1;
      const price = shares.get(h)?.price ?? fallbackPrice;
      const b = bucketStart(h, period);
      grid.set(b, (grid.get(b) || 0) + kwh * share * price);
      solar.set(b, (solar.get(b) || 0) + kwh * (1 - share) * price);
    }
    const rows = (m) => [...m].sort((a, b) => a[0] - b[0]).map(([start, change]) => ({ start, end: nextBucket(start, period), change }));
    out[id] = rows(grid);
    out[id + SOLAR_SUFFIX] = rows(solar);
  }
  return out;
}

/** Per-meter grid cost and solar savings rows summed across meters: { cost_grid: rows, cost_solar: rows }. */
export function sumCostStats(perMeter, ids, period) {
  const total = (keys) => {
    const sums = new Map();
    for (const key of keys) for (const r of perMeter?.[key] || []) sums.set(r.start, (sums.get(r.start) || 0) + r.change);
    return [...sums].sort((a, b) => a[0] - b[0]).map(([start, change]) => ({ start, end: nextBucket(start, period), change }));
  };
  return { [COST_GRID]: total(ids), [COST_SOLAR]: total(ids.map((id) => id + SOLAR_SUFFIX)) };
}

/** Cost mode energy data: grid cost and solar savings of all bar meters (plus export income if loaded), in currency. */
export function costEnergyData(data, cost, words) {
  const exported = cost?.stats && COST_EXPORT in cost.stats;
  return {
    ...data,
    prefs: {
      ...data.prefs,
      energy_sources: [],
      device_consumption: [
        { stat_consumption: COST_GRID, name: words.grid },
        { stat_consumption: COST_SOLAR, name: words.solar },
        ...(exported ? [{ stat_consumption: COST_EXPORT, name: words.export }] : []),
      ],
    },
    stats: cost?.stats || {},
    statsCompare: data.statsCompare ? cost?.statsCompare || {} : data.statsCompare,
  };
}

// Grid cost and solar savings take the Energy dashboard's grid and solar colors, with the native bar
// fill and border alphas (compare: fainter, like the native compare bars).
const COST_COLORS = {
  [COST_GRID]: ["--energy-grid-consumption-color", "#488fc2"],
  [COST_SOLAR]: ["--energy-solar-color", "#ff9800"],
  [COST_EXPORT]: ["--energy-grid-return-color", "#8353d1"],
};

/**
 * Dashed net bill line (grid cost + negative export income per step) on the currency axis, with its
 * faded compare line, when export income is loaded.
 */
export function netBillSeries(cost, data, period, name, color) {
  if (!cost?.stats?.[COST_EXPORT]) return [];
  const offset = pointOffset(period);
  const net = (stats) => {
    const sums = new Map();
    for (const key of [COST_GRID, COST_EXPORT]) for (const r of stats?.[key] || []) sums.set(r.start, (sums.get(r.start) || 0) + r.change);
    return [...sums].sort((a, b) => a[0] - b[0]);
  };
  const line = (id, rows, map, alpha) => ({
    id,
    name,
    type: "line",
    yAxisIndex: 0,
    showSymbol: false,
    color: color + alpha,
    lineStyle: { width: 1.5, type: "dashed", color: color + alpha },
    data: rows.map(([start, value]) => [map(new Date(start)).getTime() + offset, value, start]),
  });
  const out = [];
  if (data.statsCompare && cost.statsCompare?.[COST_EXPORT]) {
    const shift = compareShift(new Date(data.start), new Date(data.startCompare));
    out.push(line(`compare-${COST_NET}`, net(cost.statsCompare), shift, "7F"));
  }
  out.push(line(COST_NET, net(cost.stats), (d) => d, ""));
  return out;
}

const COST_ORDER = [COST_GRID, COST_SOLAR, COST_EXPORT];
const BAR_RADIUS = 4;

/**
 * Fixed stack order for the cost series (grid at the bottom, solar on top, export below the axis) in
 * each period group, instead of the native order by period total, which differs between cards and
 * periods; the native rounded stack ends are then recomputed point by point for the new order.
 */
export function orderCostSeries(chartData, statOf) {
  const rank = (series) => COST_ORDER.indexOf(statOf(String(series.id)));
  const isCompare = (series) => String(series.id).startsWith("compare-");
  const sorted = (compare) => chartData.filter((series) => rank(series) >= 0 && isCompare(series) === compare).sort((a, b) => rank(a) - rank(b));
  const others = chartData.filter((series) => rank(series) < 0);
  const groups = [sorted(true), sorted(false)];
  for (const group of groups) {
    const value = (point) => (Array.isArray(point) ? point : point?.value);
    const rows = group.map((series) => (series.data || []).map(value));
    const ends = new Map();
    rows.forEach((points, i) =>
      points.forEach((point) => {
        if (!point || !point[1]) return;
        const end = ends.get(point[0]) || {};
        if (point[1] > 0) end.top = i;
        else end.bottom = i;
        ends.set(point[0], end);
      })
    );
    group.forEach((series, i) => {
      series.data = rows[i].map((point) => {
        const end = point && ends.get(point[0]);
        if (end?.top === i && point[1] > 0) return { value: point, itemStyle: { borderRadius: [BAR_RADIUS, BAR_RADIUS, 0, 0] } };
        if (end?.bottom === i && point[1] < 0) return { value: point, itemStyle: { borderRadius: [0, 0, BAR_RADIUS, BAR_RADIUS] } };
        return point;
      });
    });
  }
  // Native layout: compare series first, then the placeholder, then the current ones.
  return [...groups[0], ...others, ...groups[1]];
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
        // Cost mode: the native options take the unit as 5th argument (axis name, tooltip values).
        if (this._isCost()) args[4] = this._currency();
        const options = nativeOptions(...args);
        const lines = this._lineCount ?? 0;
        if (cache.options !== options || cache.lines !== lines) cache = { options, lines, result: this._extendOptions(options) };
        return cache.result;
      };
      // The native "total consumed" translation has "kWh" written in, whatever unit it is given.
      const nativeTotal = this._formatTotal;
      this._formatTotal = (value) => (this._isCost() ? `${this._costWords().total} ${this._money(value)} ${this._currency()}` : nativeTotal(value));
    }

    _isCost() {
      return (this._mode ?? this._config?.mode) === "cost";
    }

    /** Switches kWh / currency in place: cost data is fetched on demand, the native card redraws. */
    _setMode(mode) {
      if ((this._mode ?? this._config?.mode ?? "energy") === mode) return;
      this._mode = mode;
      this._extra = null;
      this._processStatistics();
      if (this._data && this._series) this._loadExtra(this._data);
      this.requestUpdate("_mode");
    }

    updated(changed) {
      super.updated?.(changed);
      this._renderModeToggle();
    }

    /** The native template owns the card content: the switch is added next to it and kept across its renders. */
    _renderModeToggle() {
      const card = this.shadowRoot?.querySelector("ha-card");
      const existing = this.shadowRoot?.querySelector(".mode-toggle");
      if (!this._config?.mode_toggle || !card) return existing?.remove();
      let toggle = existing;
      if (!toggle) {
        const style = document.createElement("style");
        style.textContent = `
          ha-card { position: relative; }
          .mode-toggle { position: absolute; top: 16px; right: 16px; z-index: 1; display: flex; border: 1px solid var(--divider-color); border-radius: 16px; overflow: hidden; }
          .mode-toggle button { font: inherit; font-size: 12px; line-height: 20px; padding: 2px 10px; border: none; background: none; color: var(--secondary-text-color); cursor: pointer; }
          .mode-toggle button.active { background: var(--primary-color); color: var(--text-primary-color); }`;
        toggle = document.createElement("div");
        toggle.className = "mode-toggle";
        for (const mode of ["energy", "cost"]) {
          const button = document.createElement("button");
          button.dataset.mode = mode;
          button.addEventListener("click", () => this._setMode(mode));
          toggle.appendChild(button);
        }
        card.append(style, toggle);
      }
      for (const button of toggle.querySelectorAll("button")) {
        const cost = button.dataset.mode === "cost";
        button.textContent = cost ? this._currency() : "kWh";
        button.classList.toggle("active", cost === this._isCost());
      }
    }

    _currency() {
      const code = this.hass?.config?.currency || "EUR";
      return code === "EUR" ? "€" : code;
    }

    _money(value) {
      return value.toLocaleString(this._numberLocale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    setConfig(config) {
      if (!Array.isArray(config?.series) || !config.series.length) throw new Error("energy-stats-graph-card: add at least one series");
      super.setConfig(config);
      this._series = null;
      this._extra = null;
      this._mode = undefined;
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
      const [stats, statsCompare, cost] = await Promise.all([
        fetch(data.start, data.end),
        data.startCompare ? fetch(data.startCompare, data.endCompare) : null,
        this._isCost() ? this._loadCost(data, period) : null,
      ]);
      if (data !== this._data) return;
      const old = this._extra;
      this._extra = { data, period, stats, statsCompare, cost };
      this.requestUpdate("_extra", old);
    }

    /** Hourly consumption, sources and price, split into grid cost and solar savings per step of the period. */
    async _loadCost(data, period) {
      const src = energySources(data.prefs);
      const bars = this._series.filter((s) => s.type === "bar").map((s) => s.id);
      // Export income only on request, and only when HA records it (a compensation statistic exists).
      const compensation = this._config.export ? compensationStat(src, this.hass.states) : null;
      const ids = [
        ...new Set([...bars, ...src.solar, ...src.gridIn, ...src.gridOut, ...src.batteryIn, ...src.batteryOut, ...src.price, ...(compensation ? [compensation] : [])]),
      ];
      const current = parseFloat(this.hass.states[src.price[0]]?.state);
      const fallbackPrice = src.fixedPrice ?? (Number.isFinite(current) ? current : 0);
      const range = async (start, end) => {
        const raw = await this.hass.callWS({
          type: "recorder/statistics_during_period",
          start_time: new Date(start).toISOString(),
          end_time: new Date(end || Date.now()).toISOString(),
          statistic_ids: ids,
          period: "hour",
          types: ["change", "mean"],
          units: { energy: "kWh" },
        });
        const changes = hourlyValues(raw, "change");
        const means = src.price.length ? hourlyValues(raw, "mean")[src.price[0]] || new Map() : new Map();
        const shares = hourlyShares(changes, src, (h) => means.get(h) ?? fallbackPrice);
        const stats = sumCostStats(costStats(changes, shares, bars, period, fallbackPrice), bars, period);
        if (compensation) stats[COST_EXPORT] = exportStats(changes, compensation, period);
        return stats;
      };
      const [stats, statsCompare] = await Promise.all([range(data.start, data.end), data.startCompare ? range(data.startCompare, data.endCompare) : null]);
      return { stats, statsCompare };
    }

    _processStatistics() {
      const real = this._data;
      if (!real || !this._series) return;
      const extra = this._extra?.data === real ? this._extra : null;
      const bars = this._series.filter((s) => s.type === "bar");
      const lines = this._series.filter((s) => s.type === "line");
      // Shadows the reactive accessor for the native computation only, without triggering an update.
      const own = Object.getOwnPropertyDescriptor(this, "_data");
      const nativeData = this._isCost() ? costEnergyData(real, extra?.cost, this._costWords()) : restrictEnergyData(real, bars, extra);
      Object.defineProperty(this, "_data", { value: nativeData, configurable: true, writable: true });
      try {
        super._processStatistics();
      } finally {
        if (own) Object.defineProperty(this, "_data", own);
        else delete this._data;
      }
      // Only the configured bars: other devices were passed for their colors and come out empty.
      const keep = new Set(this._isCost() ? [COST_GRID, COST_SOLAR, COST_EXPORT] : bars.map((s) => s.id));
      const isKept = (id) => id === "compare-placeholder" || keep.has(this._getStatIdFromId(String(id)));
      this._chartData = this._chartData.filter((series) => isKept(series.id));
      this._legendData = (this._legendData || []).filter((item) => isKept(item.id));
      if (this._isCost()) this._styleCostParts();
      const netData = this._isCost() ? netBillSeries(extra?.cost, real, extra?.period, this._costWords().net, this._netColor()) : [];
      const lineData = [...lineSeries(lines, extra, real, bars.length ? 1 : 0), ...netData];
      this._lineCount = lineData.length;
      this._lineUnits = new Map([...lines.map((s) => [s.id, s.unit]), [COST_NET, this._currency()]]);
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
        ...(netData.length
          ? [
              {
                id: COST_NET,
                secondaryIds: [`compare-${COST_NET}`],
                name: this._costWords().net,
                itemStyle: { color: this._netColor(), borderColor: this._netColor() },
                noLabelClick: true,
                value: `${this._money(netData.at(-1).data.reduce((acc, p) => acc + p[1], 0))} ${this._currency()}`,
              },
            ]
          : []),
      ];
    }

    _netColor() {
      const css = typeof window.getComputedStyle === "function" ? window.getComputedStyle(this).getPropertyValue("--primary-text-color").trim() : "";
      return /^#[0-9a-f]{6}$/i.test(css) ? css : "#616161";
    }

    _costWords() {
      return COST_STRINGS[String(this.hass?.locale?.language || "").split("-")[0]] || COST_STRINGS.en;
    }

    /** Grid and solar series in the Energy grid and solar colors, legend totals in currency. */
    _styleCostParts() {
      this._chartData = orderCostSeries(this._chartData, (id) => this._getStatIdFromId(id));
      const legendRank = (item) => COST_ORDER.indexOf(this._getStatIdFromId(String(item.id)));
      this._legendData = [...this._legendData].sort((a, b) => legendRank(a) - legendRank(b));
      const style = typeof window.getComputedStyle === "function" ? window.getComputedStyle(this) : null;
      const colorOf = (stat) => {
        const [cssVar, fallback] = COST_COLORS[stat];
        const css = style?.getPropertyValue(cssVar).trim();
        return /^#[0-9a-f]{6}$/i.test(css || "") ? css : fallback;
      };
      for (const series of this._chartData) {
        const stat = this._getStatIdFromId(String(series.id));
        if (!COST_COLORS[stat]) continue;
        const color = colorOf(stat);
        const compare = String(series.id).startsWith("compare-");
        series.color = `${color}${compare ? "32" : "7F"}`;
        series.itemStyle = { ...series.itemStyle, borderColor: compare ? `${color}7F` : color };
      }
      const current = new Map(this._chartData.filter((series) => !String(series.id).startsWith("compare-")).map((series) => [series.id, series]));
      this._legendData = this._legendData.map((item) => {
        const series = current.get(item.id);
        if (!series) return item;
        const sum = (series.data || []).reduce((acc, p) => acc + ((Array.isArray(p) ? p : p?.value)?.[1] || 0), 0);
        return { ...item, itemStyle: { ...item.itemStyle, color: series.color, borderColor: series.itemStyle?.borderColor }, value: `${this._money(sum)} ${this._currency()}` };
      });
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
