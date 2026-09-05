const CARD_TAG = "solar-planner-card";

const COLORS = {
  light: { forecast: "#2a78d6", actual: "#eb6834", consumption: "#1baf7a" },
  dark: { forecast: "#3987e5", actual: "#d95926", consumption: "#199e70" },
};

// Red-first, not green, to avoid a slot-3 aqua CVD clash. Re-validate with validate_palette.js.
const DEVICE_COLORS = {
  light: ["#e34948", "#008300", "#4a3aa7", "#eda100", "#e87ba4"],
  dark: ["#e66767", "#008300", "#9085e9", "#c98500", "#d55181"],
};

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
// A countdown's minute figure would otherwise only advance every REFRESH_INTERVAL_MS (5 min):
// entity states driving _relevantSignature() don't change just from time passing, only _refresh()'s
// own timer forces a re-render. A cheap re-render (no history fetch) keeps it visibly live instead.
const COUNTDOWN_REFRESH_INTERVAL_MS = 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
export const SMOOTH_BUCKET_MS = 5 * 60 * 1000;
export const DRAG_SNAP_MS = 5 * 60 * 1000;

export function snapToGrid(ms, stepMs = DRAG_SNAP_MS) {
  return Math.round(ms / stepMs) * stepMs;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function fmtW(w) {
  if (w == null || Number.isNaN(w)) return "-";
  return w >= 1000 ? `${(w / 1000).toFixed(1)} kW` : `${Math.round(w)} W`;
}

function fmtWh(wh) {
  if (wh == null || Number.isNaN(wh)) return "-";
  return `${(wh / 1000).toFixed(1)} kWh`;
}

function fmtWTick(w) {
  return String(Math.round(w / 1000));
}

function fmtTime(d) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// null once `target` is no longer in the future (started/passed), not just "0m".
function fmtCountdown(target, now) {
  const totalMin = Math.round((target.getTime() - now.getTime()) / 60000);
  if (totalMin <= 0) return null;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h > 0) return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`;
  return `in ${m}m`;
}

function startOfDay(d) {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

// Downsamples raw history into time-weighted buckets.
export function smoothCurve(points, bucketMs, rangeStart, rangeEnd) {
  if (!points.length) return [];
  const result = [];
  for (let t = rangeStart; t < rangeEnd; t = new Date(t.getTime() + bucketMs)) {
    const bEnd = new Date(t.getTime() + bucketMs);
    let sum = 0;
    let dur = 0;
    for (let i = 0; i < points.length; i++) {
      const pStart = points[i].time;
      if (pStart >= bEnd) break;
      const pEnd = i + 1 < points.length ? points[i + 1].time : bEnd;
      const overlapStart = pStart > t ? pStart : t;
      const overlapEnd = pEnd < bEnd ? pEnd : bEnd;
      if (overlapStart < overlapEnd) {
        const ms = overlapEnd.getTime() - overlapStart.getTime();
        sum += ms * points[i].value;
        dur += ms;
      }
    }
    if (dur > 0) result.push({ time: t, value: sum / dur });
  }
  return result;
}

function interpolate(points, t) {
  if (!points.length) return 0;
  const time = t.getTime();
  if (time <= points[0].time.getTime()) return points[0].w;
  const last = points[points.length - 1];
  if (time >= last.time.getTime()) return last.w;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    if (time >= a.time.getTime() && time <= b.time.getTime()) {
      const ratio = (time - a.time.getTime()) / (b.time.getTime() - a.time.getTime());
      return a.w + (b.w - a.w) * ratio;
    }
  }
  return 0;
}

// Sums every overlapping segment, not just the first match.
function powerAt(segments, t) {
  let sum = 0;
  for (const s of segments) {
    if (s.start <= t && t < s.end) sum += s.power;
  }
  return sum;
}

// Sub-buckets at most SMOOTH_BUCKET_MS wide, never straddling an item/other phase boundary. A
// bucket spanning a short phase's boundary used to misattribute the whole bucket to whichever
// side its midpoint landed in, over- or under-counting deficit.
function* instantSteps(itemSegments, otherSegments, start, end) {
  const startMs = start.getTime();
  const endMs = end.getTime();
  const breakpoints = new Set([startMs, endMs]);
  for (const seg of [...itemSegments, ...otherSegments]) {
    const segStart = seg.start.getTime();
    const segEnd = seg.end.getTime();
    if (startMs < segStart && segStart < endMs) breakpoints.add(segStart);
    if (startMs < segEnd && segEnd < endMs) breakpoints.add(segEnd);
  }
  const sortedBp = [...breakpoints].sort((a, b) => a - b);
  for (let i = 0; i < sortedBp.length - 1; i++) {
    let t = sortedBp[i];
    const t1 = sortedBp[i + 1];
    while (t < t1) {
      const stepEnd = Math.min(t + SMOOTH_BUCKET_MS, t1);
      yield [t, stepEnd, new Date((t + stepEnd) / 2)];
      t = stepEnd;
    }
  }
}

// Solar-coverage deficit (Wh), checked instant-by-instant so a brief spike can't just average out.
export function instantDeficitWh(itemSegments, otherSegments, points, baseLoad, start, end) {
  let deficitWh = 0;
  for (const [t, stepEnd, mid] of instantSteps(itemSegments, otherSegments, start, end)) {
    const itemPower = powerAt(itemSegments, mid);
    const othersPower = powerAt(otherSegments, mid);
    const solarAvailable = Math.max(0, interpolate(points, mid) - baseLoad - othersPower);
    const deficit = Math.max(0, itemPower - solarAvailable);
    deficitWh += (deficit * (stepEnd - t)) / 3600000;
  }
  return deficitWh;
}

// Bounded 0-1 while a shortfall exists; unbounded once fully covered (worst-case margin instead).
function coverageRatio(itemSegments, otherSegments, points, baseLoad, start, end) {
  const deficitWh = instantDeficitWh(itemSegments, otherSegments, points, baseLoad, start, end);
  const totalEnergyWh = itemSegments.reduce((sum, seg) => sum + (seg.power * (seg.end.getTime() - seg.start.getTime())) / 3600000, 0);
  if (deficitWh > 0) return totalEnergyWh > 0 ? Math.max(0, 1 - deficitWh / totalEnergyWh) : 0;

  const avgPowerW = totalEnergyWh / ((end.getTime() - start.getTime()) / 3600000);
  let minRatio = null;
  for (const [, , mid] of instantSteps(itemSegments, otherSegments, start, end)) {
    const itemPower = powerAt(itemSegments, mid);
    if (itemPower <= 0 || itemPower < avgPowerW) continue;
    const othersPower = powerAt(otherSegments, mid);
    const solarAvailable = Math.max(0, interpolate(points, mid) - baseLoad - othersPower);
    const ratio = solarAvailable / itemPower;
    if (minRatio === null || ratio < minRatio) minRatio = ratio;
  }
  return minRatio === null ? 1 : minRatio;
}

export function coveragePercent(itemSegments, otherSegments, points, baseLoad, start, end) {
  return Math.round(coverageRatio(itemSegments, otherSegments, points, baseLoad, start, end) * 100);
}

export function phaseSegments(item) {
  if (!item.profile) return [{ start: item.start, end: item.end, power: item.powerW }];
  let t = item.start;
  return item.profile.map((phase) => {
    const start = t;
    t = new Date(t.getTime() + phase.minutes * 60000);
    return { start, end: t, power: phase.power_w };
  });
}

class SolarPlannerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._actualPoints = [];
    this._actualCurve = [];
    this._consumptionPoints = [];
    this._consumptionCurve = [];
    this._lastRefresh = 0;
    this._lastSignature = null;
    // Drag state, not re-rendered per pointermove (see _bindGanttDrag).
    this._drag = null;
  }

  // config.devices is a list of device slugs; entity_ids are built from each program's own
  // server-computed "slug" (see _baseConfig()), never approximated client-side.
  setConfig(config) {
    if (!Array.isArray(config.devices) || !config.devices.length) {
      throw new Error("solar-planner-card: 'devices' must be a non-empty array of device slugs");
    }
    for (const slug of config.devices) {
      if (typeof slug !== "string" || !slug) {
        throw new Error("solar-planner-card: each entry in 'devices' must be a non-empty slug string");
      }
    }
    this._config = config;
    this._showChart = config.chart_expanded !== false;
    this._showTable = !!config.table_expanded;
    this._lastRefresh = 0;
    this._lastSignature = null;
  }

  // Reads base settings from sensor.solar_planner_scheduler_config, not card config.
  _baseConfig() {
    if (!this._hass) return { fixed_loads: [] };
    const state = this._hass.states["sensor.solar_planner_scheduler_config"];
    const attrs = state?.attributes || {};
    const fixedLoads = (attrs.fixed_loads || []).map((load) => ({
      ...load,
      duration_minutes: load.power_profile.reduce((s, p) => s + p.minutes, 0),
    }));
    return {
      forecast_entity: attrs.forecast_entity,
      forecast_tomorrow_entity: attrs.forecast_tomorrow_entity,
      production_entity: attrs.production_entity,
      consumption_entity: attrs.consumption_entity,
      max_simultaneous_power: state ? parseFloat(state.state) : null,
      fixed_loads: fixedLoads,
      devices: attrs.devices || [],
    };
  }

  // One row per (device, program) pair; a device missing from the config sensor is skipped.
  _programRows() {
    if (!this._hass || !this._config) return [];
    const byDeviceSlug = new Map(this._baseConfig().devices.map((d) => [d.slug, d]));
    const rows = [];
    for (const deviceSlug of this._config.devices) {
      const device = byDeviceSlug.get(deviceSlug);
      if (!device) continue;
      for (const program of device.programs) {
        rows.push({ slug: program.slug, deviceName: device.name, programName: program.name });
      }
    }
    return rows;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (!this._lastRefresh || Date.now() - this._lastRefresh > REFRESH_INTERVAL_MS) {
      this._refresh();
      return;
    }
    const sig = this._relevantSignature();
    if (sig !== this._lastSignature) {
      this._requestRender();
    }
  }

  _requestRender() {
    const active = this.shadowRoot.activeElement;
    if (active instanceof HTMLInputElement) {
      // Don't rebuild the DOM under an active input, retry on blur.
      this._renderDirty = true;
      active.addEventListener(
        "blur",
        () => {
          if (this._renderDirty) {
            this._renderDirty = false;
            this._render();
          }
        },
        { once: true }
      );
      return;
    }
    this._render();
  }

  connectedCallback() {
    this._interval = setInterval(() => this._refresh(), REFRESH_INTERVAL_MS);
    this._countdownInterval = setInterval(() => this._requestRender(), COUNTDOWN_REFRESH_INTERVAL_MS);
  }

  disconnectedCallback() {
    clearInterval(this._interval);
    clearInterval(this._countdownInterval);
  }

  getCardSize() {
    return 4 + this._programRows().length + this._baseConfig().fixed_loads.length;
  }

  // Excludes fast-ticking entities that would wipe focus/hover for no visible change.
  _relevantSignature() {
    const base = this._baseConfig();
    const ids = ["sensor.solar_planner_scheduler_config", base.forecast_entity, base.forecast_tomorrow_entity].filter(Boolean);
    for (const row of this._programRows()) {
      ids.push(`datetime.${row.slug}_start`, `binary_sensor.${row.slug}_should_run`, `switch.${row.slug}_active`);
    }
    const dark = !!this._hass.themes?.darkMode;
    return `${dark}|${ids.map((id) => `${id}:${this._hass.states[id]?.state}`).join("|")}`;
  }

  async _fetchHistory(entityId, start, end) {
    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: [entityId],
        minimal_response: false,
        no_attributes: true,
        significant_changes_only: false,
      });
      const raw = result?.[entityId] || [];
      return raw
        .map((s) => ({ time: new Date(s.last_changed || s.lu * 1000), value: parseFloat(s.state ?? s.s) }))
        .filter((s) => !Number.isNaN(s.value))
        .sort((a, b) => a.time - b.time);
    } catch (e) {
      console.warn(`solar-planner-card: history fetch failed for ${entityId}`, e);
      return [];
    }
  }

  async _refresh() {
    if (!this._hass || !this._config) return;
    this._lastRefresh = Date.now();

    const base = this._baseConfig();
    const now = new Date();
    // Matches the chart's own display window, so the actual/consumption curves don't truncate
    // at midnight while the forecast/gantt already show further back.
    const chartHoursPast = this._config.chart_hours_past ?? 6;
    const historyStart = new Date(now.getTime() - chartHoursPast * 3600000);
    const jobs = [];
    if (base.production_entity) {
      jobs.push(
        this._fetchHistory(base.production_entity, historyStart, now).then((pts) => {
          this._actualPoints = smoothCurve(pts, SMOOTH_BUCKET_MS, historyStart, now);
          this._actualCurve = this._actualPoints.map((p) => ({ time: p.time, w: p.value }));
        })
      );
    } else {
      this._actualPoints = [];
      this._actualCurve = [];
    }
    if (base.consumption_entity) {
      jobs.push(
        this._fetchHistory(base.consumption_entity, historyStart, now).then((pts) => {
          this._consumptionPoints = smoothCurve(pts, SMOOTH_BUCKET_MS, historyStart, now);
          this._consumptionCurve = this._consumptionPoints.map((p) => ({ time: p.time, w: p.value }));
        })
      );
    } else {
      this._consumptionPoints = [];
      this._consumptionCurve = [];
    }
    await Promise.all(jobs);
    this._requestRender();
  }

  _theoreticalPoints() {
    const base = this._baseConfig();
    const state = this._hass.states[base.forecast_entity];
    const detailed = state?.attributes?.detailedForecast;
    if (!Array.isArray(detailed)) return null;
    const tomorrowState = base.forecast_tomorrow_entity ? this._hass.states[base.forecast_tomorrow_entity] : null;
    const tomorrowDetailed = Array.isArray(tomorrowState?.attributes?.detailedForecast) ? tomorrowState.attributes.detailedForecast : [];
    // w10/w90 default to w: no percentiles collapses the confidence band instead of misleading.
    return [...detailed, ...tomorrowDetailed]
      .map((p) => {
        const w = (p.pv_estimate || 0) * 1000;
        return {
          time: new Date(p.period_start),
          w,
          w10: p.pv_estimate10 != null ? p.pv_estimate10 * 1000 : w,
          w90: p.pv_estimate90 != null ? p.pv_estimate90 * 1000 : w,
        };
      })
      .sort((a, b) => a.time - b.time);
  }

  // days: offsets from today, e.g. [0, 1] for today + tomorrow.
  _fixedLoadWindows(days = [0]) {
    const today = startOfDay(new Date());
    const result = [];
    this._baseConfig().fixed_loads.forEach((load, loadIndex) => {
      const [h, m] = load.start_time.split(":").map(Number);
      const powerW = load.power_profile.reduce((s, p) => s + p.minutes * p.power_w, 0) / load.duration_minutes;
      for (const dayOffset of days) {
        const start = new Date(today.getTime() + dayOffset * DAY_MS + (h * 60 + m) * 60000);
        result.push({
          deviceName: load.name,
          programName: load.name,
          durationMin: load.duration_minutes,
          powerW,
          profile: load.power_profile,
          fixed: true,
          loadIndex,
          start,
          end: new Date(start.getTime() + load.duration_minutes * 60000),
          estimatedCost: load.estimated_cost ?? null,
          currency: load.currency ?? null,
        });
      }
    });
    return result;
  }

  // Server-computed state only; the live drag preview (scorePct) is the one exception.
  _readProgramState(row) {
    const start = this._hass.states[`datetime.${row.slug}_start`];
    const shouldRun = this._hass.states[`binary_sensor.${row.slug}_should_run`];
    const active = this._hass.states[`switch.${row.slug}_active`];
    const parseTs = (state) =>
      state && state.state && state.state !== "unknown" && state.state !== "unavailable" ? new Date(state.state) : null;
    const attrs = start?.attributes || {};
    return {
      slug: row.slug,
      name: row.deviceName,
      programName: row.programName,
      start: parseTs(start),
      end: attrs.end ? new Date(attrs.end) : null,
      shouldRun: shouldRun?.state === "on",
      coveragePct: attrs.coverage_pct ?? null,
      locked: !!attrs.locked,
      powerW: attrs.power_w ?? null,
      profile: attrs.profile ?? null,
      active: active?.state === "on",
      estimatedCost: attrs.estimated_cost ?? null,
      currency: attrs.currency ?? null,
    };
  }

  async _onToggleActive(slug, nextActive) {
    await this._hass.callService("switch", nextActive ? "turn_on" : "turn_off", { entity_id: `switch.${slug}_active` });
  }

  // Shared write path for the manual time input and gantt-bar drag.
  async _setForcedStart(slug, date) {
    await this._hass.callService("datetime", "set_value", { entity_id: `datetime.${slug}_start`, datetime: date.toISOString() });
  }

  async _onManualTime(slug, timeValue) {
    if (!timeValue) return;
    const row = this._programRows().find((r) => r.slug === slug);
    const ds = row ? this._readProgramState(row) : {};
    const next = new Date(ds.start || new Date());
    const [h, m] = timeValue.split(":").map(Number);
    next.setHours(h, m, 0, 0);
    await this._setForcedStart(slug, next);
  }

  async _onAutoMode(slug) {
    await this._hass.callService("solar_planner_scheduler", "reset_to_auto", { entity_id: `datetime.${slug}_start` });
  }

  _render() {
    if (!this._hass || !this._config) return;
    // Rebuilds the whole DOM each call, so restore scrollLeft or it snaps back to the start.
    const scrollLeft = this.shadowRoot.querySelector(".chart-scroll")?.scrollLeft;
    this._lastSignature = this._relevantSignature();
    const base = this._baseConfig();
    const points = this._theoreticalPoints();
    const dark = !!this._hass.themes?.darkMode;
    const colors = dark ? COLORS.dark : COLORS.light;
    const deviceColorList = dark ? DEVICE_COLORS.dark : DEVICE_COLORS.light;
    const deviceColor = (index) => deviceColorList[index % deviceColorList.length];

    if (!points) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:16px;color:var(--error-color)">
        Entity ${base.forecast_entity} doesn't expose a "detailedForecast" attribute (Solcast required).
      </div></ha-card>`;
      return;
    }

    const programRows = this._programRows();
    const deviceStates = programRows.map((row) => this._readProgramState(row));
    const fixedLoadColor = (index) => deviceColorList[(deviceStates.length + index) % deviceColorList.length];
    // Generate tomorrow's occurrence too once the view extends there.
    const fixedLoads = this._fixedLoadWindows(base.forecast_tomorrow_entity ? [0, 1] : [0]);
    const fixedLoadsByIndex = new Map();
    fixedLoads.forEach((load) => {
      if (!fixedLoadsByIndex.has(load.loadIndex)) fixedLoadsByIndex.set(load.loadIndex, []);
      fixedLoadsByIndex.get(load.loadIndex).push(load);
    });

    const stackLayers = deviceStates
      .map((ds, i) => {
        const bar =
          ds.start && ds.end
            ? {
                deviceName: ds.name,
                programName: ds.programName,
                durationMin: (ds.end.getTime() - ds.start.getTime()) / 60000,
                powerW: ds.powerW,
                profile: ds.profile,
                start: ds.start,
                end: ds.end,
              }
            : null;
        return { color: deviceColor(i), bars: bar ? [bar] : [] };
      })
      .concat(base.fixed_loads.map((load, i) => ({ color: fixedLoadColor(i), bars: fixedLoadsByIndex.get(i) || [], fixed: true })));

    const dayStart = startOfDay(new Date());
    const now = new Date();
    const height = 220;
    const marginLeft = 28;
    const marginRight = 4;
    const marginTop = 14;
    const marginBottom = 24;
    const innerH = height - marginTop - marginBottom;

    // Fixed window around "now", configurable per card instance (chart_hours_past/chart_hours_future)
    // instead of an auto-widening daylight/scheduled-items heuristic. Always maps to width=600, so the
    // whole window fits without a side-scroll.
    const chartHoursPast = this._config.chart_hours_past ?? 6;
    const chartHoursFuture = this._config.chart_hours_future ?? 24;
    const viewStart = new Date(now.getTime() - chartHoursPast * 3600000);
    const viewEnd = new Date(now.getTime() + chartHoursFuture * 3600000);
    const viewSpanMs = viewEnd.getTime() - viewStart.getTime();

    const todayEnd = new Date(dayStart.getTime() + DAY_MS);
    const innerW = 600 - marginLeft - marginRight;
    const width = innerW + marginLeft + marginRight;
    const chartWidthPercent = 100;

    // Grid cut at exact phase boundaries, not a fixed step: avoids diluting short spikes (see CLAUDE.local.md).
    const stackLayersWithSegments = stackLayers.map((layer) => ({
      ...layer,
      bars: layer.bars.filter((b) => b.start && b.end).map((b) => ({ ...b, segments: phaseSegments(b) })),
    }));
    const stackBreakpoints = new Set([viewStart.getTime(), viewEnd.getTime()]);
    for (const layer of stackLayersWithSegments) {
      for (const bar of layer.bars) {
        for (const seg of bar.segments) {
          const s = Math.max(seg.start.getTime(), viewStart.getTime());
          const e = Math.min(seg.end.getTime(), viewEnd.getTime());
          if (s < e) {
            stackBreakpoints.add(s);
            stackBreakpoints.add(e);
          }
        }
      }
    }
    const sortedStackBreakpoints = [...stackBreakpoints].sort((a, b) => a - b);
    const stackedBuckets = [];
    for (let i = 0; i + 1 < sortedStackBreakpoints.length; i++) {
      const t = sortedStackBreakpoints[i];
      const bEnd = sortedStackBreakpoints[i + 1];
      const mid = (t + bEnd) / 2;
      let total = 0;
      const segments = stackLayersWithSegments.map((layer) => {
        const seg = layer.bars.flatMap((b) => b.segments).find((s) => s.start.getTime() <= mid && mid < s.end.getTime());
        const w = seg ? seg.power : 0;
        total += w;
        return { color: layer.color, fixed: layer.fixed, w };
      });
      stackedBuckets.push({ start: new Date(t), end: new Date(bEnd), segments, total });
    }

    // Today-only forecast points, not the tomorrow-merged ones, so a sunnier tomorrow can't rescale today's curves: tomorrow's curve just clips at this ceiling.
    const maxW =
      [
        ...points.filter((p) => p.time < todayEnd).map((p) => p.w),
        ...points.filter((p) => p.time < todayEnd).map((p) => p.w90),
        base.max_simultaneous_power,
        ...this._actualPoints.map((p) => p.value),
        ...this._consumptionPoints.map((p) => p.value),
        ...stackedBuckets.map((b) => b.total),
      ].reduce((m, v) => Math.max(m, v), 0) * 1.15;

    const x = (t) => marginLeft + ((t.getTime() - viewStart.getTime()) / viewSpanMs) * innerW;
    const y = (w) => marginTop + innerH - (Math.max(0, Math.min(w, maxW)) / maxW) * innerH;

    // Only the visible window needs path segments; `points` stays unfiltered elsewhere (interpolate, maxW).
    const visiblePoints = points.filter((p) => p.time >= viewStart && p.time <= viewEnd);
    const visibleActualPoints = this._actualPoints.filter((p) => p.time >= viewStart && p.time <= viewEnd);
    const visibleConsumptionPoints = this._consumptionPoints.filter((p) => p.time >= viewStart && p.time <= viewEnd);
    // Closed polygon: P90 left-to-right, then P10 back. Zero width draws an invisible sliver, not a gap.
    const hasConfidenceBand = visiblePoints.some((p) => p.w90 > p.w10);
    const confidenceBandPath = hasConfidenceBand
      ? [
          ...visiblePoints.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.w90).toFixed(1)}`),
          ...[...visiblePoints].reverse().map((p) => `L${x(p.time).toFixed(1)},${y(p.w10).toFixed(1)}`),
          "Z",
        ].join(" ")
      : "";
    const forecastPath = visiblePoints.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.w).toFixed(1)}`).join(" ");
    const actualPath = visibleActualPoints
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.value).toFixed(1)}`)
      .join(" ");
    const consumptionPath = visibleConsumptionPoints
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.value).toFixed(1)}`)
      .join(" ");

    const stackedRects = stackedBuckets
      .map((bucket) => {
        const bx = x(bucket.start);
        const bw = Math.max(0, x(bucket.end) - bx);
        const rects = [];
        let cum = 0;
        // Reversed so the first config entry (gantt's top lane) ends up on top of the stack, not the bottom.
        for (const seg of [...bucket.segments].reverse()) {
          if (seg.w > 0) {
            const cls = seg.fixed ? "stack-fixed" : "stack-confirmed";
            rects.push(
              `<rect x="${bx.toFixed(1)}" y="${y(cum + seg.w).toFixed(1)}" width="${bw.toFixed(1)}" height="${(y(cum) - y(cum + seg.w)).toFixed(1)}" style="${seg.color ? `fill:${seg.color}` : ""}" class="stack-segment ${cls}"/>`
            );
          }
          cum += seg.w;
        }
        return rects.join("");
      })
      .join("");

    const hourTicks = [];
    const tickStepHours = Math.max(1, Math.round(viewSpanMs / 3600000 / 8));
    const firstTickHour = Math.ceil(viewStart.getTime() / 3600000 / tickStepHours) * tickStepHours;
    for (let h = firstTickHour; h * 3600000 <= viewEnd.getTime(); h += tickStepHours) {
      const t = new Date(h * 3600000);
      hourTicks.push(`<text x="${x(t).toFixed(1)}" y="${height - 6}" class="axis-label" text-anchor="middle">${t.getHours()}h</text>`);
    }
    // Marks each midnight boundary so repeated hour labels ("8h") aren't ambiguous between days.
    // Labeled by day offset from today, not always "Tomorrow": chart_hours_future can span several days.
    const dayTicks = [];
    let dayOffset = 1;
    for (let d = new Date(todayEnd); d < viewEnd; d = new Date(d.getTime() + DAY_MS), dayOffset++) {
      if (d <= viewStart) continue;
      const label = dayOffset === 1 ? "Tomorrow" : `In ${dayOffset} days`;
      dayTicks.push(
        `<line x1="${x(d).toFixed(1)}" y1="${marginTop}" x2="${x(d).toFixed(1)}" y2="${height - marginBottom}" class="day-line"/>`,
        `<text x="${(x(d) + 4).toFixed(1)}" y="${marginTop + 9}" class="axis-label day-label">${label}</text>`
      );
    }
    const wTicks = [`<text x="${marginLeft - 6}" y="${marginTop - 2}" class="axis-label" text-anchor="end">kW</text>`];
    const wStep = maxW > 4000 ? 2000 : 1000;
    for (let w = 0; w <= maxW; w += wStep) {
      wTicks.push(
        `<line x1="${marginLeft}" y1="${y(w).toFixed(1)}" x2="${width - marginRight}" y2="${y(w).toFixed(1)}" class="grid"/>`,
        `<text x="${marginLeft - 6}" y="${(y(w) + 3).toFixed(1)}" class="axis-label" text-anchor="end">${fmtWTick(w)}</text>`
      );
    }

    const maxLineY = y(base.max_simultaneous_power).toFixed(1);
    const nowX = x(now).toFixed(1);

    // Color carries device identity; lanes stay thin since hover titles (not lane labels) carry the detail.
    const laneHeight = 10;
    const laneGap = 2;
    const ganttTop = 2;
    const laneCount = deviceStates.length + base.fixed_loads.length;
    const ganttHeight = laneCount * (laneHeight + laneGap) + ganttTop;

    const laneBars = [];
    deviceStates.forEach((ds, i) => {
      const laneY = ganttTop + i * (laneHeight + laneGap);
      const barColor = deviceColor(i);
      const bars = [];
      if (ds.start && ds.end) {
        const durationMin = (ds.end.getTime() - ds.start.getTime()) / 60000;
        // Profile-based programs have no flat powerW; sum each phase's own minutes*power_w instead.
        const energyWh = ds.profile ? ds.profile.reduce((s, p) => s + (p.minutes * p.power_w) / 60, 0) : (ds.powerW * durationMin) / 60;
        const powerLabel = ds.profile ? `${fmtWh(energyWh)} · peak ${fmtW(Math.max(...ds.profile.map((p) => p.power_w)))}` : fmtWh(energyWh);
        const label = `${ds.name} · ${ds.programName} · ${fmtTime(ds.start)}-${fmtTime(ds.end)} · ${powerLabel}`;
        const bx = x(ds.start);
        const bw = Math.max(2, x(ds.end) - bx);
        bars.push(
          `<g class="bar-group"><rect x="${bx.toFixed(1)}" y="${laneY}" width="${bw.toFixed(1)}" height="${laneHeight}" rx="2" style="fill:${barColor}" class="bar confirmed bar-draggable" data-device="${ds.slug}"/><title>${label}</title></g>`
        );
      }
      laneBars.push(bars.join(""));
    });
    // Fixed loads are read-only (hatched, "reserved" not "proposed"); a lane can hold today's + tomorrow's occurrence.
    base.fixed_loads.forEach((loadConfig, i) => {
      const laneY = ganttTop + (deviceStates.length + i) * (laneHeight + laneGap);
      const occurrences = fixedLoadsByIndex.get(i) || [];
      const rects = occurrences
        .map((load) => {
          const bx = x(load.start);
          const bw = Math.max(2, x(load.end) - bx);
          const loadPowerLabel = load.profile
            ? `${fmtWh((load.powerW * load.durationMin) / 60)} · peak ${fmtW(Math.max(...load.profile.map((p) => p.power_w)))}`
            : fmtWh((load.powerW * load.durationMin) / 60);
          const label = `${load.programName} (external) · ${fmtTime(load.start)}-${fmtTime(load.end)} · ${loadPowerLabel}`;
          return `<g class="bar-group"><rect x="${bx.toFixed(1)}" y="${laneY}" width="${bw.toFixed(1)}" height="${laneHeight}" rx="2" style="fill:${fixedLoadColor(i)}" class="bar fixed"/><title>${label}</title></g>`;
        })
        .join("");
      laneBars.push(rects);
    });

    const unplaced = deviceStates.filter((ds) => ds.active && !ds.start);

    // Groups consecutive rows sharing a device so the name shows once, not per program.
    const deviceGroups = [];
    deviceStates.forEach((ds, i) => {
      const lastGroup = deviceGroups[deviceGroups.length - 1];
      if (lastGroup && lastGroup.name === ds.name) lastGroup.rows.push({ ds, i });
      else deviceGroups.push({ name: ds.name, rows: [{ ds, i }] });
    });
    const deviceRows = deviceGroups
      .map((group) => {
        const rows = group.rows
          .map(({ ds, i }) => {
            const slot = ds.active
              ? `<input type="time" class="slot-time" data-device="${ds.slug}" value="${ds.start ? fmtTime(ds.start) : ""}">
                ${ds.locked ? `<button class="auto-btn" data-device="${ds.slug}">Auto</button>` : ""}
                ${
                  ds.coveragePct != null
                    ? `<span class="coverage-pct ${ds.coveragePct >= 100 ? "coverage-good" : "coverage-low"}">${ds.coveragePct}% solar</span>`
                    : ""
                }
                ${
                  ds.estimatedCost != null
                    ? `<span class="estimated-cost">~${ds.estimatedCost.toFixed(2)} ${ds.currency ?? ""}</span>`
                    : ""
                }
                ${
                  ds.start && !ds.shouldRun && fmtCountdown(ds.start, now)
                    ? `<span class="countdown">${fmtCountdown(ds.start, now)}</span>`
                    : ""
                }`
              : "";
            return `<div class="program-row">
              <span class="swatch" style="background:${deviceColor(i)}"></span>
              <button class="program-toggle ${ds.active ? "active" : ""}" data-row="${ds.slug}" data-active="${ds.active}">${ds.programName}</button>
              ${ds.shouldRun ? `<ha-icon class="running-icon" icon="mdi:play-circle" title="Currently running"></ha-icon>` : ""}
              ${slot}
            </div>`;
          })
          .join("");
        return `<div class="device-select">
          <div class="device-select-header">${group.name}</div>
          ${rows}
        </div>`;
      })
      .join("");

    // Not selectable, but still need a legend since the gantt carries no text labels.
    const fixedLoadsLegend = base.fixed_loads
      .map(
        (load, i) =>
          `<div class="device-select"><div class="device-select-header"><span class="swatch fixed-swatch" style="background:${fixedLoadColor(i)}"></span><span class="device-name">${load.name} (external)</span></div></div>`
      )
      .join("");

    const deviceTableRows = deviceStates
      .filter((ds) => ds.start && ds.end)
      .map((ds) => {
        const durationMin = (ds.end.getTime() - ds.start.getTime()) / 60000;
        return {
          deviceName: ds.name,
          programName: ds.programName,
          start: ds.start,
          end: ds.end,
          // A profile has no single flat powerW, so sum each phase's own minutes*power_w instead.
          energyWh: ds.profile ? ds.profile.reduce((s, p) => s + (p.minutes * p.power_w) / 60, 0) : (ds.powerW * durationMin) / 60,
          estimatedCost: ds.estimatedCost,
          currency: ds.currency,
        };
      });
    // Distinguishes today's/tomorrow's occurrence of a recurring fixed load.
    const allTableRows = [
      ...deviceTableRows,
      ...fixedLoads.map((p) => ({ ...p, energyWh: (p.powerW * p.durationMin) / 60 })),
    ];
    // A fixed load spanning a full day (or an exact multiple of one) renders an identical row for
    // "today" and "tomorrow" (same clock time, same energy): keep only the first occurrence.
    // A shorter, genuinely daily-recurring load (e.g. an hour at 22:00) must keep both rows: its
    // today/tomorrow occurrences are real, distinct runs, not a rendering artifact.
    const seenRowKeys = new Set();
    const dedupedTableRows = allTableRows.filter((p) => {
      if (!p.start) return true;
      const durationMin = (p.end.getTime() - p.start.getTime()) / 60000;
      if (durationMin % 1440 !== 0) return true;
      const key = `${p.deviceName}|${p.programName}|${fmtTime(p.start)}|${fmtTime(p.end)}|${p.energyWh}`;
      if (seenRowKeys.has(key)) return false;
      seenRowKeys.add(key);
      return true;
    });
    const showEnergyColumn = this._config.table_show_energy !== false;
    const showCostColumn = this._config.table_show_cost !== false;
    const tableRows = dedupedTableRows
      .map((p) => {
        const dayLabel = p.start && p.start >= todayEnd ? "Tomorrow " : "";
        const countdown = p.start ? fmtCountdown(p.start, now) : null;
        const started = p.start && p.start <= now;
        return `<tr class="${started ? "row-started" : ""}">
          <td>${p.deviceName}${p.fixed ? " (external)" : ` - ${p.programName}`}</td>
          <td>${p.start ? `${dayLabel}${fmtTime(p.start)} - ${fmtTime(p.end)}${countdown ? ` (${countdown})` : ""}` : "no window"}</td>
          ${showEnergyColumn ? `<td>${fmtWh(p.energyWh)}</td>` : ""}
          ${showCostColumn ? `<td>${p.estimatedCost != null ? `~${p.estimatedCost.toFixed(2)} ${p.currency ?? ""}` : "-"}</td>` : ""}
        </tr>`;
      })
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; }
        .header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; row-gap: 4px; margin-bottom: 8px; }
        .title { font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); }
        .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.95em; color: var(--secondary-text-color); }
        .legend span { display: inline-flex; align-items: center; gap: 4px; }
        .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
        .fixed-swatch { opacity: 0.6; border: 1px dashed var(--secondary-text-color); }
        .chart-scroll { overflow-x: auto; }
        svg.chart, svg.gantt { height: auto; display: block; }
        .grid { stroke: var(--divider-color); stroke-width: 1; }
        .axis-label { fill: var(--secondary-text-color); font-size: 11px; }
        .forecast-line { fill: none; stroke: ${colors.forecast}; stroke-width: 2; }
        .confidence-band { stroke: none; opacity: 0.15; }
        .confidence-swatch { opacity: 0.35; }
        .actual-line { fill: none; stroke: ${colors.actual}; stroke-width: 2; }
        .consumption-line { fill: none; stroke: ${colors.consumption}; stroke-width: 2; }
        .stack-segment.stack-confirmed { opacity: 0.55; }
        .stack-segment.stack-fixed { opacity: 0.45; }
        .max-line { stroke: var(--warning-color, #fab219); stroke-width: 1.5; stroke-dasharray: 4 3; }
        .now-line { stroke: var(--secondary-text-color); stroke-width: 1; stroke-dasharray: 2 2; }
        .day-line { stroke: var(--divider-color); stroke-width: 1.5; }
        .day-label { font-weight: 500; }
        .hover-line { stroke: var(--secondary-text-color); stroke-width: 1; opacity: 0; }
        .hover-dot { r: 3; opacity: 0; }
        .hover-catch { fill: transparent; cursor: crosshair; }
        .hover-box { opacity: 0; }
        .hover-box rect { fill: var(--card-background-color, #fff); stroke: var(--divider-color); }
        .hover-box text { fill: var(--primary-text-color); font-size: 11px; }
        .gantt { margin-top: 4px; }
        .bar.confirmed { opacity: 0.9; }
        .bar.fixed {
          opacity: 0.55;
          stroke: var(--secondary-text-color);
          stroke-width: 1;
          stroke-dasharray: 3 2;
        }
        .bar-group:hover .bar { opacity: 1; }
        .bar-draggable { cursor: grab; touch-action: none; }
        .bar-draggable:active { cursor: grabbing; }
        .drag-pct-bg { fill: var(--card-background-color, #1c1c1c); opacity: 0.92; }
        .drag-pct { font-size: 9px; fill: var(--primary-text-color); }
        .device-select { margin-top: 12px; }
        .device-select-header { font-size: 0.95em; font-weight: 500; color: var(--primary-text-color); margin-bottom: 4px; }
        .device-select-header .swatch { margin-right: 5px; vertical-align: middle; }
        .program-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 0.85em; flex-wrap: wrap; }
        .program-row .swatch { flex-shrink: 0; }
        .running-icon { --mdc-icon-size: 14px; color: var(--success-color, #4caf50); vertical-align: middle; }
        .program-toggle { border: 1px solid var(--divider-color); background: none; border-radius: 12px; padding: 3px 10px; font-size: 0.95em; cursor: pointer; color: var(--primary-text-color); font-family: inherit; }
        .program-toggle.active { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        .slot-time { border: 1px solid var(--divider-color); border-radius: 4px; background: none; color: var(--primary-text-color); }
        .auto-btn { background: none; border: none; color: var(--primary-color); cursor: pointer; font-size: 0.85em; padding: 0; text-decoration: underline; }
        .coverage-pct { font-size: 0.8em; font-weight: 500; }
        .coverage-pct.coverage-good { color: var(--success-color, #4caf50); }
        .coverage-pct.coverage-low { color: var(--warning-color, #fab219); }
        .estimated-cost { font-size: 0.8em; font-weight: 500; color: var(--secondary-text-color); }
        .countdown { font-size: 0.8em; color: var(--secondary-text-color); }
        .warnings { margin-top: 10px; font-size: 0.85em; color: var(--warning-color, #fab219); }
        .warnings div { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
        .title-row { display: flex; align-items: center; gap: 4px; }
        .icon-toggle { cursor: pointer; color: var(--primary-color); background: none; border: none; padding: 0; display: inline-flex; align-items: center; }
        .icon-toggle ha-icon { --mdc-icon-size: 20px; }
        .table-toggle-row { margin-top: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.85em; }
        th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--divider-color); }
        .row-started { opacity: 0.5; }
      </style>
      <ha-card>
        <div class="header">
          <span class="title-row">
            <span class="title">Solar Planner</span>
            <button class="icon-toggle" id="toggle-chart" title="${this._showChart ? "Hide" : "Show"} planning">
              <ha-icon icon="mdi:chevron-${this._showChart ? "up" : "down"}"></ha-icon>
              <ha-icon icon="mdi:chart-bell-curve"></ha-icon>
            </button>
          </span>
          ${
            this._showChart
              ? `<span class="legend">
            <span><span class="swatch" style="background:${colors.forecast}"></span>Forecast</span>
            ${hasConfidenceBand ? `<span><span class="swatch confidence-swatch" style="background:${colors.forecast}"></span>Confidence (P10-P90)</span>` : ""}
            ${this._actualPoints.length ? `<span><span class="swatch" style="background:${colors.actual}"></span>Real production</span>` : ""}
            ${this._consumptionPoints.length ? `<span><span class="swatch" style="background:${colors.consumption}"></span>Consumption</span>` : ""}
          </span>`
              : ""
          }
        </div>
        ${
          this._showChart
            ? `<div class="chart-scroll">
          <svg class="chart" viewBox="0 0 ${width} ${height}" style="width: ${chartWidthPercent}%">
            ${wTicks.join("")}
            ${hourTicks.join("")}
            ${dayTicks.join("")}
            ${stackedRects}
            <line x1="${marginLeft}" y1="${maxLineY}" x2="${width - marginRight}" y2="${maxLineY}" class="max-line"/>
            <line x1="${nowX}" y1="${marginTop}" x2="${nowX}" y2="${height - marginBottom}" class="now-line"/>
            ${confidenceBandPath ? `<path d="${confidenceBandPath}" class="confidence-band" style="fill:${colors.forecast}"/>` : ""}
            <path d="${forecastPath}" class="forecast-line"/>
            ${actualPath ? `<path d="${actualPath}" class="actual-line"/>` : ""}
            ${consumptionPath ? `<path d="${consumptionPath}" class="consumption-line"/>` : ""}
            <g id="hover-group">
              <line class="hover-line" x1="0" y1="${marginTop}" x2="0" y2="${height - marginBottom}"/>
              <circle class="hover-dot" style="fill:${colors.forecast}"/>
              <circle class="hover-dot" style="fill:${colors.actual}"/>
              <circle class="hover-dot" style="fill:${colors.consumption}"/>
              <g class="hover-box"><rect width="140" height="76" rx="3"/><text class="hover-time" x="8" y="16"></text><text class="hover-forecast" x="8" y="30"></text><text class="hover-confidence" x="8" y="44"></text><text class="hover-actual" x="8" y="58"></text><text class="hover-consumption" x="8" y="72"></text></g>
            </g>
            <rect id="hover-catch" class="hover-catch" x="${marginLeft}" y="${marginTop}" width="${innerW}" height="${innerH}"/>
          </svg>
          <svg class="gantt" viewBox="0 0 ${width} ${ganttHeight}" style="width: ${chartWidthPercent}%">${laneBars.join(
                ""
              )}<line x1="${nowX}" y1="0" x2="${nowX}" y2="${ganttHeight}" class="now-line"/><g class="drag-pct-group" style="opacity:0" pointer-events="none"><rect class="drag-pct-bg" width="60" height="14" rx="2"/><text class="drag-pct" x="30" y="10.5" text-anchor="middle"></text></g></svg>
        </div>
        ${deviceRows}
        ${fixedLoadsLegend}
        ${
          unplaced.length
            ? `<div class="warnings">${unplaced
                .map((ds) => `<div><ha-icon icon="mdi:alert"></ha-icon>No sufficient solar window today for ${ds.name}.</div>`)
                .join("")}</div>`
            : ""
        }`
            : ""
        }
        <div class="table-toggle-row">
          <button class="icon-toggle" id="toggle-table" title="${this._showTable ? "Hide" : "Show"} as table">
            <ha-icon icon="mdi:chevron-${this._showTable ? "up" : "down"}"></ha-icon>
            <ha-icon icon="mdi:table"></ha-icon>
          </button>
        </div>
        ${
          this._showTable
            ? `<table><thead><tr><th>Device</th><th>Window</th>${showEnergyColumn ? "<th>Energy</th>" : ""}${showCostColumn ? "<th>Cost</th>" : ""}</tr></thead><tbody>${tableRows}</tbody></table>`
            : ""
        }
      </ha-card>`;

    // Deferred via double requestAnimationFrame: HA's grid layout may still be settling.
    if (scrollLeft != null) {
      const scrollEl = this.shadowRoot.querySelector(".chart-scroll");
      if (scrollEl) {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            scrollEl.scrollLeft = scrollLeft;
          });
        });
      }
    }

    this.shadowRoot.getElementById("toggle-chart")?.addEventListener("click", () => {
      this._showChart = !this._showChart;
      this._render();
    });
    this.shadowRoot.getElementById("toggle-table")?.addEventListener("click", () => {
      this._showTable = !this._showTable;
      this._render();
    });
    this.shadowRoot.querySelectorAll(".program-toggle").forEach((btn) => {
      btn.addEventListener("click", () => this._onToggleActive(btn.dataset.row, btn.dataset.active !== "true"));
    });
    this.shadowRoot.querySelectorAll(".slot-time").forEach((input) => {
      input.addEventListener("change", () => this._onManualTime(input.dataset.device, input.value));
    });
    this.shadowRoot.querySelectorAll(".auto-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._onAutoMode(btn.dataset.device));
    });

    this._bindGanttDrag({ viewStart, viewSpanMs, marginLeft, marginRight, width });
    // Remapped to interpolate()'s {time, w} shape so the shared helper stays untouched.
    const w10Curve = points.map((p) => ({ time: p.time, w: p.w10 }));
    const w90Curve = points.map((p) => ({ time: p.time, w: p.w90 }));
    this._bindHover({ points, w10Curve, w90Curve, viewStart, viewSpanMs, x, y, marginLeft, marginRight, marginTop, height, width, todayEnd });
  }

  // pointermove moves `x` directly, not via _render() which would drop pointer capture mid-drag.
  _bindGanttDrag({ viewStart, viewSpanMs, marginLeft, marginRight, width }) {
    const svg = this.shadowRoot.querySelector("svg.gantt");
    if (!svg) return;
    const innerW = width - marginLeft - marginRight;
    const timeAt = (clientX, clientY) => {
      const pt = svg.createSVGPoint();
      pt.x = clientX;
      pt.y = clientY;
      const local = pt.matrixTransform(svg.getScreenCTM().inverse());
      return new Date(viewStart.getTime() + ((local.x - marginLeft) / innerW) * viewSpanMs);
    };

    const pctGroup = this.shadowRoot.querySelector(".drag-pct-group");
    const pctText = this.shadowRoot.querySelector(".drag-pct");

    // Same math as the coveragePct badge; cached at pointerdown, only the start is rescored per move.
    const scorePct = (drag, start) => {
      const end = new Date(start.getTime() + drag.durationMin * 60000);
      const segments = phaseSegments({ ...drag.item, start, end });
      return coveragePercent(segments, drag.otherSegments, drag.points, drag.baseLoad, start, end);
    };

    // Label flips above/below and clamps horizontally to stay on-screen near the dragged bar.
    const showPct = (drag, bx, laneY, laneHeight) => {
      const pct = scorePct(drag, drag.currentStart);
      pctText.textContent = `${pct}% solar`;
      // Same green/orange threshold as the device row's badge.
      pctText.style.fill = pct >= 100 ? "var(--success-color, #4caf50)" : "var(--warning-color, #fab219)";
      const above = laneY - 18;
      const labelY = above >= 0 ? above : laneY + laneHeight + 6;
      const labelX = Math.min(Math.max(bx, marginLeft), width - marginRight - 60);
      pctGroup.setAttribute("transform", `translate(${labelX.toFixed(1)}, ${labelY.toFixed(1)})`);
      pctGroup.style.opacity = "1";
    };

    this.shadowRoot.querySelectorAll(".bar-draggable").forEach((rect) => {
      const slug = rect.dataset.device;

      rect.addEventListener("pointerdown", (ev) => {
        const row = this._programRows().find((r) => r.slug === slug);
        if (!row) return;
        const ds = this._readProgramState(row);
        if (!ds.start || !ds.end) return;
        rect.setPointerCapture?.(ev.pointerId);
        const grabOffsetMs = timeAt(ev.clientX, ev.clientY).getTime() - ds.start.getTime();
        const points = this._theoreticalPoints();
        // No live background-consumption estimate: only declared consumers count.
        const baseLoad = 0;
        const otherDeviceBars = this._programRows()
          .filter((r) => r.slug !== slug)
          .map((r) => this._readProgramState(r))
          .filter((o) => o.start && o.end)
          .map((o) => ({ start: o.start, end: o.end, powerW: o.powerW, profile: o.profile }));
        const fixedLoads = this._fixedLoadWindows(this._baseConfig().forecast_tomorrow_entity ? [0, 1] : [0]);
        const otherSegments = [...otherDeviceBars, ...fixedLoads].flatMap((o) => phaseSegments(o));
        this._drag = {
          slug,
          pointerId: ev.pointerId,
          grabOffsetMs,
          durationMin: (ds.end.getTime() - ds.start.getTime()) / 60000,
          currentStart: ds.start,
          item: { powerW: ds.powerW, profile: ds.profile },
          points,
          baseLoad,
          otherSegments,
        };
        showPct(this._drag, parseFloat(rect.getAttribute("x")), parseFloat(rect.getAttribute("y")), parseFloat(rect.getAttribute("height")));
      });

      rect.addEventListener("pointermove", (ev) => {
        const drag = this._drag;
        if (!drag || drag.pointerId !== ev.pointerId || drag.slug !== slug) return;
        const rawStartMs = timeAt(ev.clientX, ev.clientY).getTime() - drag.grabOffsetMs;
        drag.currentStart = new Date(snapToGrid(rawStartMs));
        const bx = marginLeft + ((drag.currentStart.getTime() - viewStart.getTime()) / viewSpanMs) * innerW;
        rect.setAttribute("x", bx.toFixed(1));
        showPct(drag, bx, parseFloat(rect.getAttribute("y")), parseFloat(rect.getAttribute("height")));
      });

      const commit = async (ev) => {
        const drag = this._drag;
        if (!drag || drag.pointerId !== ev.pointerId || drag.slug !== slug) return;
        this._drag = null;
        pctGroup.style.opacity = "0";
        await this._setForcedStart(slug, drag.currentStart);
      };
      rect.addEventListener("pointerup", commit);
      rect.addEventListener("pointercancel", (ev) => {
        if (this._drag?.pointerId === ev.pointerId) this._drag = null;
        pctGroup.style.opacity = "0";
        this._requestRender();
      });
    });
  }

  _bindHover({ points, w10Curve, w90Curve, viewStart, viewSpanMs, x, y, marginLeft, marginRight, marginTop, height, width, todayEnd }) {
    const svg = this.shadowRoot.querySelector("svg.chart");
    const catch_ = this.shadowRoot.getElementById("hover-catch");
    const scrollEl = this.shadowRoot.querySelector(".chart-scroll");
    if (!svg || !catch_) return;
    const group = this.shadowRoot.getElementById("hover-group");
    const line = group.querySelector(".hover-line");
    const [dotForecast, dotActual, dotConsumption] = group.querySelectorAll(".hover-dot");
    const box = group.querySelector(".hover-box");
    const timeText = box.querySelector(".hover-time");
    const forecastText = box.querySelector(".hover-forecast");
    const confidenceText = box.querySelector(".hover-confidence");
    const actualText = box.querySelector(".hover-actual");
    const consumptionText = box.querySelector(".hover-consumption");

    // Dots' opacity is set per-point in mousemove, not here.
    const show = (visible) => {
      line.style.opacity = visible ? "1" : "0";
      box.style.opacity = visible ? "1" : "0";
    };

    catch_.addEventListener("mouseleave", () => {
      show(false);
      dotForecast.style.opacity = "0";
      dotActual.style.opacity = "0";
      dotConsumption.style.opacity = "0";
    });
    catch_.addEventListener("mousemove", (ev) => {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX;
      pt.y = ev.clientY;
      const local = pt.matrixTransform(svg.getScreenCTM().inverse());
      const clampedX = Math.min(Math.max(local.x, marginLeft), width - marginRight);
      const t = new Date(viewStart.getTime() + ((clampedX - marginLeft) / (width - marginLeft - marginRight)) * viewSpanMs);
      // interpolate() clamps past a curve's end; blank each series past its own end instead.
      const inToday = t < todayEnd;
      const inForecastRange = points.length && t <= points[points.length - 1].time;
      const wForecast = inForecastRange ? interpolate(points, t) : null;
      const w10 = inForecastRange ? interpolate(w10Curve, t) : null;
      const w90 = inForecastRange ? interpolate(w90Curve, t) : null;
      const wActual = this._actualCurve.length && inToday ? interpolate(this._actualCurve, t) : null;
      const wConsumption = this._consumptionCurve.length && inToday ? interpolate(this._consumptionCurve, t) : null;

      line.setAttribute("x1", clampedX);
      line.setAttribute("x2", clampedX);
      dotForecast.style.opacity = wForecast != null ? "1" : "0";
      if (wForecast != null) {
        dotForecast.setAttribute("cx", clampedX);
        dotForecast.setAttribute("cy", y(wForecast));
      }
      dotActual.style.opacity = wActual != null ? "1" : "0";
      if (wActual != null) {
        dotActual.setAttribute("cx", clampedX);
        dotActual.setAttribute("cy", y(wActual));
      }
      dotConsumption.style.opacity = wConsumption != null ? "1" : "0";
      if (wConsumption != null) {
        dotConsumption.setAttribute("cx", clampedX);
        dotConsumption.setAttribute("cy", y(wConsumption));
      }
      timeText.textContent = fmtTime(t);
      forecastText.textContent = wForecast != null ? `Forecast: ${fmtW(wForecast)}` : "";
      confidenceText.textContent = w10 != null && w90 != null && w90 > w10 ? `Confidence: ${fmtW(w10)} - ${fmtW(w90)}` : "";
      actualText.textContent = wActual != null ? `Real production: ${fmtW(wActual)}` : "";
      consumptionText.textContent = wConsumption != null ? `Consumption: ${fmtW(wConsumption)}` : "";
      // Flips against the scrolled viewport's edge, not the total SVG width.
      let visibleRight = width - marginRight;
      const scrollRect = scrollEl?.getBoundingClientRect();
      if (scrollRect) {
        const rightPt = svg.createSVGPoint();
        rightPt.x = scrollRect.right;
        rightPt.y = ev.clientY;
        visibleRight = rightPt.matrixTransform(svg.getScreenCTM().inverse()).x - marginRight;
      }
      const boxX = clampedX + 140 > visibleRight ? clampedX - 146 : clampedX + 6;
      box.setAttribute("transform", `translate(${boxX}, ${marginTop})`);
      show(true);
    });
  }
}

customElements.define(CARD_TAG, SolarPlannerCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: CARD_TAG,
  name: "Solar Planner Card",
  description: "Displays the solar-based schedule computed by the solar_planner_scheduler integration.",
});
