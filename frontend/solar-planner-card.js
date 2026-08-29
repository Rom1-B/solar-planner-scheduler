const CARD_TAG = "solar-planner-card";
// Duration doubles as the program identifier (no separate catalog); 0 means none selected.
const NONE_DURATION = 0;

const DEFAULTS = {
  history_lookback_days: 30,
  idle_power_threshold: 10,
  duration_tolerance_percent: 20,
  run_gap_tolerance_minutes: 5,
};

const COLORS = {
  light: { forecast: "#2a78d6", actual: "#eb6834", consumption: "#1baf7a" },
  dark: { forecast: "#3987e5", actual: "#d95926", consumption: "#199e70" },
};

// Devices start at color slot 4+ (red-first, not green, to avoid a slot-3 aqua CVD clash) — re-validate with validate_palette.js before reordering.
const DEVICE_COLORS = {
  light: ["#e34948", "#008300", "#4a3aa7", "#eda100", "#e87ba4"],
  dark: ["#e66767", "#008300", "#9085e9", "#c98500", "#d55181"],
};

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
export const BUCKET_MS = 30 * 60 * 1000;
export const SMOOTH_BUCKET_MS = 5 * 60 * 1000;
export const DRAG_SNAP_MS = 5 * 60 * 1000;
// At/above this, today is "good enough" and tomorrow isn't even checked (see _resolveTodaySlot). Not 100 — coveragePct is unbounded above.
export const GOOD_ENOUGH_COVERAGE_PCT = 99;

// Snaps a dragged gantt bar's start to a readable grid instead of an arbitrary pixel-derived ms.
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

// Always kWh, even under 1 (e.g. "0.2 kWh") — simpler than fmtW's W/kW switch.
function fmtWh(wh) {
  if (wh == null || Number.isNaN(wh)) return "-";
  return `${(wh / 1000).toFixed(1)} kWh`;
}

// Y-axis ticks only — wStep is always a multiple of 1000, so every tick is a whole kW (unit shown once, above the axis).
function fmtWTick(w) {
  return String(Math.round(w / 1000));
}

function fmtTime(d) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtHaDatetime(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
}

function parseHaDatetime(state) {
  if (!state || state === "unknown" || state === "unavailable") return null;
  const d = new Date(state.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d;
}

function startOfDay(d) {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

// Downsamples raw event-driven history (thousands of samples by afternoon) into time-weighted buckets for a readable line.
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

function detectRuns(samples, idleThreshold, gapToleranceMin) {
  const raw = [];
  let run = null;
  for (const sample of samples) {
    if (sample.value > idleThreshold) {
      if (!run) run = { start: sample.time, end: sample.time, points: [sample] };
      else {
        run.end = sample.time;
        run.points.push(sample);
      }
    } else if (run) {
      raw.push(run);
      run = null;
    }
  }
  if (run) raw.push(run);

  const merged = [];
  for (const r of raw) {
    const prev = merged[merged.length - 1];
    if (prev && (r.start.getTime() - prev.end.getTime()) / 60000 <= gapToleranceMin) {
      // Mark idle gaps at 0 W, otherwise weightedAvg charges them at the run's last reading.
      prev.points.push({ time: prev.end, value: 0 });
      prev.end = r.end;
      prev.points.push(...r.points);
    } else {
      merged.push({ start: r.start, end: r.end, points: [...r.points] });
    }
  }

  // Time-weighted average: a plain sample mean overweights whichever phase emits the most state changes.
  const weightedAvg = (points, runEnd) => {
    let totalSec = 0;
    let totalWV = 0;
    for (let i = 0; i < points.length; i++) {
      const next = i + 1 < points.length ? points[i + 1].time : runEnd;
      const dt = (next.getTime() - points[i].time.getTime()) / 1000;
      totalSec += dt;
      totalWV += dt * points[i].value;
    }
    return totalSec > 0 ? totalWV / totalSec : points.reduce((s, p) => s + p.value, 0) / points.length;
  };

  return merged
    .map((r) => ({
      durationMin: (r.end.getTime() - r.start.getTime()) / 60000,
      avgW: weightedAvg(r.points, r.end),
    }))
    .filter((r) => r.durationMin >= 1);
}

// Power at time t, summing every overlapping segment — segments concatenates several items, so a plain .find() would silently drop all but one concurrent item (real bug, see CLAUDE.local.md).
function powerAt(segments, t) {
  let sum = 0;
  for (const s of segments) {
    if (s.start <= t && t < s.end) sum += s.power;
  }
  return sum;
}

// Solar-coverage deficit (Wh), checked instant-by-instant at SMOOTH_BUCKET_MS (not a 30-min average) so a brief spike must itself clear the forecast, not just balance out over the half-hour.
export function instantDeficitWh(itemSegments, otherSegments, points, baseLoad, start, end) {
  let deficitWh = 0;
  for (let t = start.getTime(); t < end.getTime(); t += SMOOTH_BUCKET_MS) {
    const stepEnd = Math.min(t + SMOOTH_BUCKET_MS, end.getTime());
    const mid = new Date((t + stepEnd) / 2);
    const itemPower = powerAt(itemSegments, mid);
    const othersPower = powerAt(otherSegments, mid);
    const solarAvailable = Math.max(0, interpolate(points, mid) - baseLoad - othersPower);
    const deficit = Math.max(0, itemPower - solarAvailable);
    deficitWh += (deficit * (stepEnd - t)) / 3600000;
  }
  return deficitWh;
}

// Deficit weighted by energy share while any shortfall exists (bounded 0-1); once fully covered, worst
// ratio among above-average-power instants only (unbounded, see CLAUDE.local.md). Unrounded, for findBestPlacement.
function coverageRatio(itemSegments, otherSegments, points, baseLoad, start, end) {
  const deficitWh = instantDeficitWh(itemSegments, otherSegments, points, baseLoad, start, end);
  const totalEnergyWh = itemSegments.reduce((sum, seg) => sum + (seg.power * (seg.end.getTime() - seg.start.getTime())) / 3600000, 0);
  if (deficitWh > 0) return totalEnergyWh > 0 ? 1 - deficitWh / totalEnergyWh : 0;

  const avgPowerW = totalEnergyWh / ((end.getTime() - start.getTime()) / 3600000);
  let minRatio = null;
  for (let t = start.getTime(); t < end.getTime(); t += SMOOTH_BUCKET_MS) {
    const stepEnd = Math.min(t + SMOOTH_BUCKET_MS, end.getTime());
    const mid = new Date((t + stepEnd) / 2);
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

// Exact max_simultaneous_power check: sweeps real phase boundaries, not 30-min buckets, so near-miss spikes aren't falsely summed (see findPeakConflicts).
function fitsPeakCeiling(itemSegments, otherSegments, maxSimultaneousPower) {
  const itemStart = itemSegments[0].start.getTime();
  const itemEnd = itemSegments[itemSegments.length - 1].end.getTime();
  const all = [...otherSegments, ...itemSegments];
  const breakpoints = new Set([itemStart, itemEnd]);
  for (const seg of all) {
    const s = seg.start.getTime();
    const e = seg.end.getTime();
    if (s > itemStart && s < itemEnd) breakpoints.add(s);
    if (e > itemStart && e < itemEnd) breakpoints.add(e);
  }
  const sorted = [...breakpoints].sort((a, b) => a - b);
  for (let i = 0; i + 1 < sorted.length; i++) {
    const t0 = sorted[i];
    const t1 = sorted[i + 1];
    let sum = 0;
    for (const seg of all) {
      if (seg.start.getTime() <= t0 && t1 <= seg.end.getTime()) sum += seg.power;
    }
    if (sum > maxSimultaneousPower) return false;
  }
  return true;
}

// Maximizes coverageRatio (unrounded — avoids ties from coveragePercent's rounding). max_simultaneous_power
// stays a hard filter via fitsPeakCeiling; candidate step comes from `buckets` itself, not a hardcoded 30 min.
export function findBestPlacement(buckets, item, maxSimultaneousPower, points, baseLoad, others) {
  const stepMs = buckets.length > 1 ? buckets[1].start.getTime() - buckets[0].start.getTime() : DRAG_SNAP_MS;
  const span = Math.max(1, Math.ceil(item.durationMin / (stepMs / 60000)));
  const otherSegments = others.filter((o) => o.start && o.end).flatMap((o) => phaseSegments(o));
  let best = null;
  for (let i = 0; i + span <= buckets.length; i++) {
    const start = buckets[i].start;
    const end = new Date(start.getTime() + item.durationMin * 60000);
    const candidate = { ...item, start, end };
    const itemSegments = phaseSegments(candidate);
    if (!fitsPeakCeiling(itemSegments, otherSegments, maxSimultaneousPower)) continue;

    const ratio = coverageRatio(itemSegments, otherSegments, points, baseLoad, start, end);
    if (!best || ratio > best.ratio) {
      best = { index: i, ratio, coveragePct: Math.round(ratio * 100) };
    }
  }
  return best;
}

// preCommitted seeds already-reserved items; each newly placed item is pushed onto it so later items in the batch see earlier placements.
export function scheduleProposals(buckets, items, maxSimultaneousPower, points, baseLoad, preCommitted = []) {
  const committed = [...preCommitted];
  const sorted = [...items].sort((a, b) => b.powerW - a.powerW);
  const proposals = [];
  for (const item of sorted) {
    const placement = findBestPlacement(buckets, item, maxSimultaneousPower, points, baseLoad, committed);
    if (placement) {
      const start = buckets[placement.index].start;
      const end = new Date(start.getTime() + item.durationMin * 60000);
      const placed = { ...item, start, end };
      committed.push(placed);
      proposals.push(placed);
    } else {
      proposals.push({ ...item, start: null, end: null });
    }
  }
  return proposals;
}

// Breaks an item into absolute-time phase segments; no profile means one flat segment for the whole run.
export function phaseSegments(item) {
  if (!item.profile) return [{ start: item.start, end: item.end, power: item.powerW }];
  let t = item.start;
  return item.profile.map((phase) => {
    const start = t;
    t = new Date(t.getTime() + phase.minutes * 60000);
    return { start, end: t, power: phase.power_w };
  });
}

// Exact conflict check: sweeps real phase boundaries and sums truly concurrent power, not a 30-min bucket approximation.
export function findPeakConflicts(entries, maxSimultaneousPower) {
  const withSegments = entries.filter((e) => e.start && e.end).map((e) => ({ entry: e, segments: phaseSegments(e) }));
  const breakpoints = new Set();
  for (const { segments } of withSegments) {
    for (const seg of segments) {
      breakpoints.add(seg.start.getTime());
      breakpoints.add(seg.end.getTime());
    }
  }
  const sorted = [...breakpoints].sort((a, b) => a - b);
  const conflicted = new Set();
  for (let i = 0; i + 1 < sorted.length; i++) {
    const t0 = sorted[i];
    const t1 = sorted[i + 1];
    const involved = [];
    let sum = 0;
    for (const { entry, segments } of withSegments) {
      const seg = segments.find((s) => s.start.getTime() <= t0 && t1 <= s.end.getTime());
      if (seg) {
        sum += seg.power;
        involved.push(entry);
      }
    }
    if (sum > maxSimultaneousPower) involved.forEach((e) => conflicted.add(e));
  }
  return conflicted;
}

class SolarPlannerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._estimates = new Map();
    // Optimistic overlay — hass.callService resolving doesn't mean `hass` reflects the new state yet.
    this._pending = new Map();
    this._actualPoints = [];
    this._actualCurve = [];
    this._consumptionPoints = [];
    this._consumptionCurve = [];
    this._lastRefresh = 0;
    this._lastSignature = null;
    this._selectionError = null;
    // Set by _resolveTodaySlot when tomorrow beats today — holds both slots/percentages for the "Use today/tomorrow" prompt.
    this._scheduleChoice = null;
    // Live gantt-bar drag state, not re-rendered per pointermove — see _bindGanttDrag.
    this._drag = null;
  }

  // Only shape — duration_minutes is always derived from the profile (see setConfig).
  _validateProfile(profile, label) {
    if (!Array.isArray(profile) || !profile.length) {
      throw new Error(`solar-planner-card: ${label} needs a non-empty 'power_profile'`);
    }
    for (const phase of profile) {
      if (!(phase.minutes > 0) || phase.power_w == null || phase.power_w < 0) {
        throw new Error(`solar-planner-card: ${label} has a power_profile phase needing 'minutes' > 0 and 'power_w' >= 0`);
      }
    }
  }

  setConfig(config) {
    if (!config.forecast_entity) throw new Error("solar-planner-card: 'forecast_entity' is required");
    if (!config.surplus_entity) throw new Error("solar-planner-card: 'surplus_entity' is required");
    if (!config.max_simultaneous_power) throw new Error("solar-planner-card: 'max_simultaneous_power' is required");
    if (!Array.isArray(config.devices) || !config.devices.length) {
      throw new Error("solar-planner-card: 'devices[]' is required");
    }
    const devices = config.devices.map((device) => {
      if (!device.name || !device.power_sensor) {
        throw new Error("solar-planner-card: each device needs 'name' and 'power_sensor'");
      }
      if (!Array.isArray(device.programs) || !device.programs.length) {
        throw new Error(`solar-planner-card: device '${device.name}' needs 'programs[]'`);
      }
      const seenDurations = new Set();
      const programs = device.programs.map((program) => {
        if (!program.name) throw new Error(`solar-planner-card: each program of '${device.name}' needs 'name'`);
        let durationMinutes = program.duration_minutes;
        if (program.power_profile) {
          this._validateProfile(program.power_profile, `program '${program.name}' of '${device.name}'`);
          durationMinutes = program.power_profile.reduce((s, p) => s + p.minutes, 0);
        } else if (!durationMinutes) {
          // No power_profile means "estimate from history" — duration_minutes is required to match past cycles to this program.
          throw new Error(`solar-planner-card: program '${program.name}' of '${device.name}' needs 'power_profile' or 'duration_minutes'`);
        }
        if (seenDurations.has(durationMinutes)) {
          throw new Error(
            `solar-planner-card: device '${device.name}' has two programs with duration_minutes=${durationMinutes} — durations must be unique per device (used as the selection identifier)`
          );
        }
        seenDurations.add(durationMinutes);
        return { ...program, duration_minutes: durationMinutes };
      });
      return { ...device, programs };
    });
    const fixedLoads = (config.fixed_loads || []).map((load) => {
      if (!load.name || !load.start_time || !load.power_profile) {
        throw new Error("solar-planner-card: each fixed_loads[] entry needs 'name', 'start_time' (HH:MM) and 'power_profile'");
      }
      if (!/^\d{1,2}:\d{2}$/.test(load.start_time)) {
        throw new Error(`solar-planner-card: fixed_loads '${load.name}' has an invalid start_time (expected "HH:MM")`);
      }
      this._validateProfile(load.power_profile, `fixed_loads '${load.name}'`);
      const durationMinutes = load.power_profile.reduce((s, p) => s + p.minutes, 0);
      return { ...load, duration_minutes: durationMinutes };
    });
    this._config = { ...DEFAULTS, ...config, devices, fixed_loads: fixedLoads };
    this._estimates = new Map();
    this._lastRefresh = 0;
    this._lastSignature = null;
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
      // Don't rebuild the DOM under an input being actively edited (e.g. the manual time picker) — retry on blur.
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
  }

  disconnectedCallback() {
    clearInterval(this._interval);
  }

  getCardSize() {
    return 4 + (this._config?.devices?.length || 0) + (this._config?.fixed_loads?.length || 0);
  }

  _relevantSignature() {
    // Excludes fast-ticking entities (surplus/power/etc.) that only feed 5-min-refreshed values — rendering on those would wipe focus/hover for nothing.
    const ids = [this._config.forecast_entity, this._config.forecast_tomorrow_entity, this._config.state_entity].filter(Boolean);
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

  async _estimateDevicePrograms(device) {
    // Skip the history fetch if every program already declares a power_profile.
    const needsHistory = device.programs.some((p) => p.power_profile == null);
    let runs = [];
    if (needsHistory) {
      const end = new Date();
      const start = new Date(end.getTime() - this._config.history_lookback_days * DAY_MS);
      const samples = await this._fetchHistory(device.power_sensor, start, end);
      runs = detectRuns(samples, this._config.idle_power_threshold, this._config.run_gap_tolerance_minutes);
    }
    const tolerance = this._config.duration_tolerance_percent / 100;
    for (const program of device.programs) {
      let result;
      if (program.power_profile) {
        const totalMin = program.power_profile.reduce((s, p) => s + p.minutes, 0);
        const avgW = program.power_profile.reduce((s, p) => s + p.minutes * p.power_w, 0) / totalMin;
        result = { powerW: avgW, profile: program.power_profile, approximate: false };
      } else {
        const matching = runs.filter(
          (r) => Math.abs(r.durationMin - program.duration_minutes) / program.duration_minutes <= tolerance
        );
        if (matching.length) {
          result = { powerW: matching.reduce((s, r) => s + r.avgW, 0) / matching.length, approximate: false };
        } else if (runs.length) {
          result = { powerW: runs.reduce((s, r) => s + r.avgW, 0) / runs.length, approximate: true };
        } else {
          result = null;
        }
      }
      this._estimates.set(`${device.power_sensor}::${program.name}`, result);
    }
  }

  async _refresh() {
    if (!this._hass || !this._config) return;
    this._lastRefresh = Date.now();

    const jobs = this._config.devices.map((device) => this._estimateDevicePrograms(device));
    const midnight = startOfDay(new Date());
    const now = new Date();
    if (this._config.production_entity) {
      jobs.push(
        this._fetchHistory(this._config.production_entity, midnight, now).then((pts) => {
          // Smoothed once here, not per mousemove — raw history can hold thousands of samples by afternoon.
          this._actualPoints = smoothCurve(pts, SMOOTH_BUCKET_MS, midnight, now);
          this._actualCurve = this._actualPoints.map((p) => ({ time: p.time, w: p.value }));
        })
      );
    } else {
      this._actualPoints = [];
      this._actualCurve = [];
    }
    if (this._config.consumption_entity) {
      jobs.push(
        this._fetchHistory(this._config.consumption_entity, midnight, now).then((pts) => {
          this._consumptionPoints = smoothCurve(pts, SMOOTH_BUCKET_MS, midnight, now);
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
    const state = this._hass.states[this._config.forecast_entity];
    const detailed = state?.attributes?.detailedForecast;
    if (!Array.isArray(detailed)) return null;
    // Tomorrow's forecast is opt-in — the scheduler only searches it via _resolveTodaySlot's comparison, not by default.
    const tomorrowState = this._config.forecast_tomorrow_entity ? this._hass.states[this._config.forecast_tomorrow_entity] : null;
    const tomorrowDetailed = Array.isArray(tomorrowState?.attributes?.detailedForecast) ? tomorrowState.attributes.detailedForecast : [];
    return [...detailed, ...tomorrowDetailed]
      .map((p) => ({ time: new Date(p.period_start), w: (p.pv_estimate || 0) * 1000 }))
      .sort((a, b) => a.time - b.time);
  }

  // dayOffset: 0 = today (default), 1 = tomorrow (full day) — 1 is only used by _resolveTodaySlot's comparison check.
  _futureSurplusBuckets(points, dayOffset = 0) {
    const now = new Date();
    const today = startOfDay(now);
    // Rounded up to the next DRAG_SNAP_MS mark (not just `now`) so the candidate grid lines up with what a manual drag can reach.
    const rangeStart =
      dayOffset === 0 ? new Date(Math.ceil(now.getTime() / DRAG_SNAP_MS) * DRAG_SNAP_MS) : new Date(today.getTime() + dayOffset * DAY_MS);
    const rangeEnd = new Date(today.getTime() + (dayOffset + 1) * DAY_MS);
    const surplusState = this._hass.states[this._config.surplus_entity];
    const surplusNow =
      surplusState && surplusState.state !== "unavailable" && surplusState.state !== "unknown"
        ? Math.max(0, parseFloat(surplusState.state))
        : 0;
    // Prefer measured production over forecast for "now" so forecast error doesn't get baked into baseLoad.
    const productionState = this._config.production_entity ? this._hass.states[this._config.production_entity] : null;
    const measuredProdNow =
      productionState && productionState.state !== "unavailable" && productionState.state !== "unknown"
        ? parseFloat(productionState.state)
        : NaN;
    const prodNow = !Number.isNaN(measuredProdNow) ? measuredProdNow : interpolate(points, now);
    const baseLoad = Math.max(0, prodNow - surplusNow);
    // Candidate grid at DRAG_SNAP_MS (5 min), not a coarser step, so auto-search can find anything a manual drag could.
    const buckets = [];
    for (let t = new Date(rangeStart); t < rangeEnd; t = new Date(t.getTime() + DRAG_SNAP_MS)) {
      buckets.push({ start: new Date(t) });
    }
    return { buckets, baseLoad, surplusNow };
  }

  // One shared input_text (JSON, keyed by device name); invalid/missing content means "nothing selected".
  _readSharedState() {
    const raw = this._hass.states[this._config.state_entity]?.state;
    if (!raw) return {};
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  _activeSelections() {
    const result = [];
    if (!this._config.state_entity) return result;
    const shared = this._readSharedState();
    for (const device of this._config.devices) {
      const entry = shared[device.name];
      const durationValue = entry?.duration;
      const hassStart = entry?.start ? parseHaDatetime(entry.start) : null;

      const pending = this._pending.get(device.name);
      if (pending) {
        const caughtUp = durationValue === pending.durationMin && hassStart && hassStart.getTime() === pending.start.getTime();
        if (caughtUp) {
          this._pending.delete(device.name);
        } else {
          result.push({ deviceName: device.name, ...pending });
          continue;
        }
      }

      if (!durationValue || Number.isNaN(durationValue)) continue;
      const program = device.programs.find((p) => p.duration_minutes === durationValue);
      if (!program) continue;
      const est = this._estimates.get(`${device.power_sensor}::${program.name}`);
      if (!est) continue;
      const end = hassStart ? new Date(hassStart.getTime() + program.duration_minutes * 60000) : null;
      result.push({
        deviceName: device.name,
        programName: program.name,
        durationMin: program.duration_minutes,
        powerW: est.powerW,
        profile: est.profile,
        approximate: est.approximate,
        start: hassStart,
        end,
      });
    }
    return result;
  }

  // days: daily-occurrence offsets from today (e.g. [0, 1] for today + tomorrow) — fixed loads recur daily.
  _fixedLoadWindows(days = [0]) {
    const today = startOfDay(new Date());
    const result = [];
    (this._config.fixed_loads || []).forEach((load, loadIndex) => {
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
          approximate: false,
          fixed: true,
          loadIndex,
          start,
          end: new Date(start.getTime() + load.duration_minutes * 60000),
        });
      }
    });
    return result;
  }

  // dayOffset forwarded to _futureSurplusBuckets — only ever 1 from _resolveTodaySlot's comparison check.
  _suggestSlot(deviceName, program, dayOffset = 0) {
    const points = this._theoreticalPoints();
    if (!points) return null;
    const est = this._estimates.get(
      `${this._config.devices.find((d) => d.name === deviceName).power_sensor}::${program.name}`
    );
    if (!est) return null;
    const item = { powerW: est.powerW, profile: est.profile, durationMin: program.duration_minutes };
    const { buckets, baseLoad } = this._futureSurplusBuckets(points, dayOffset);
    const others = [...this._activeSelections(), ...this._fixedLoadWindows(dayOffset === 0 ? [0] : [0, 1])].filter(
      (o) => o.deviceName !== deviceName
    );
    const placement = findBestPlacement(buckets, item, this._config.max_simultaneous_power, points, baseLoad, others);
    if (!placement) return null;
    const start = buckets[placement.index].start;
    // findBestPlacement always returns the least-bad start, not null — coveragePct tells the caller how good it actually is.
    return { start, end: new Date(start.getTime() + program.duration_minutes * 60000), powerW: est.powerW, coveragePct: placement.coveragePct };
  }

  _previewSchedule(points) {
    const { buckets, baseLoad } = this._futureSurplusBuckets(points);
    // Selectable ("wired") is all-or-nothing: state_entity applies to every device, so nothing to preview once configured.
    const items = [];
    if (!this._config.state_entity) {
      for (const device of this._config.devices) {
        for (const program of device.programs) {
          const est = this._estimates.get(`${device.power_sensor}::${program.name}`);
          if (!est) continue;
          items.push({
            deviceName: device.name,
            programName: program.name,
            durationMin: program.duration_minutes,
            powerW: est.powerW,
            profile: est.profile,
            approximate: est.approximate,
          });
        }
      }
    }
    const committed = [...this._activeSelections(), ...this._fixedLoadWindows()];
    return scheduleProposals(buckets, items, this._config.max_simultaneous_power, points, baseLoad, committed);
  }

  // Merges in any other device's optimistic _pending write not yet echoed back by hass, so it isn't clobbered.
  async _writeSharedEntry(deviceName, entry) {
    const shared = this._readSharedState();
    for (const [name, pending] of this._pending) {
      if (name !== deviceName) shared[name] = { duration: pending.durationMin, start: fmtHaDatetime(pending.start) };
    }
    if (entry) shared[deviceName] = entry;
    else delete shared[deviceName];
    await this._hass.callService("input_text", "set_value", {
      entity_id: this._config.state_entity,
      value: JSON.stringify(shared),
    });
  }

  // Shared by _onSelectProgram/_onRecalculate/_onUseToday/_onUseTomorrow: optimistic pending write, then the real state_entity write.
  async _commitSlot(device, program, slot) {
    this._selectionError = null;
    this._scheduleChoice = null;
    this._pending.set(device.name, {
      programName: program.name,
      durationMin: program.duration_minutes,
      powerW: slot.powerW,
      profile: this._estimates.get(`${device.power_sensor}::${program.name}`)?.profile,
      approximate: this._estimates.get(`${device.power_sensor}::${program.name}`)?.approximate,
      start: slot.start,
      end: slot.end,
    });
    this._requestRender();
    await this._writeSharedEntry(device.name, { duration: program.duration_minutes, start: fmtHaDatetime(slot.start) });
  }

  // Shared by _onSelectProgram/_onRecalculate. Today alone isn't "best available" (findBestPlacement never fails on a poor match) — compares against tomorrow when configured and today is below GOOD_ENOUGH_COVERAGE_PCT, letting the user pick via _onUseToday/_onUseTomorrow instead of committing blindly. keepingSuffix: appended by _onRecalculate, whose previous window still stands if the prompt isn't acted on.
  _resolveTodaySlot(deviceName, program, keepingSuffix = "") {
    const today = this._suggestSlot(deviceName, program);
    const hasTomorrowOption = !!this._config.forecast_tomorrow_entity;
    // -Infinity (not 0) so a tomorrow with any real candidate counts as strictly better when today has none.
    const todayCoveragePct = today?.coveragePct ?? -Infinity;
    const tomorrow = hasTomorrowOption && todayCoveragePct < GOOD_ENOUGH_COVERAGE_PCT ? this._suggestSlot(deviceName, program, 1) : null;
    const tomorrowIsBetter = !!tomorrow && (!today || tomorrow.coveragePct > todayCoveragePct);

    if (tomorrowIsBetter) {
      const todayPct = today ? today.coveragePct : null;
      const tomorrowPct = tomorrow.coveragePct;
      this._selectionError = today
        ? `${deviceName}: today's best window for "${program.name}" covers ${todayPct}% from solar — tomorrow covers ${tomorrowPct}%.${keepingSuffix}`
        : `${deviceName}: no solar window available today for "${program.name}" — tomorrow covers ${tomorrowPct}%.${keepingSuffix}`;
      this._scheduleChoice = { deviceName, durationMin: program.duration_minutes, todaySlot: today, todayPct, tomorrowSlot: tomorrow, tomorrowPct };
      return null;
    }

    if (!today) {
      this._selectionError = `${deviceName}: no solar window available today${hasTomorrowOption ? " or tomorrow" : ""} for "${program.name}".${keepingSuffix}`;
      return null;
    }

    return today;
  }

  async _onSelectProgram(deviceName, durationValue) {
    const device = this._config.devices.find((d) => d.name === deviceName);
    if (!device || !this._config.state_entity) return;
    if (durationValue === NONE_DURATION) {
      this._pending.delete(deviceName);
      this._selectionError = null;
      this._scheduleChoice = null;
      await this._writeSharedEntry(deviceName, null);
      return;
    }
    const program = device.programs.find((p) => p.duration_minutes === durationValue);
    // Don't select without a committed slot: duration and start must move together, or the reminder fires on a stale time.
    const slot = this._resolveTodaySlot(deviceName, program);
    if (!slot) {
      this._requestRender();
      return;
    }
    await this._commitSlot(device, program, slot);
  }

  async _onRecalculate(deviceName) {
    const device = this._config.devices.find((d) => d.name === deviceName);
    const current = this._activeSelections().find((s) => s.deviceName === deviceName);
    const program = current && device.programs.find((p) => p.duration_minutes === current.durationMin);
    if (!program) return;
    const slot = this._resolveTodaySlot(deviceName, program, " Keeping the previous window.");
    if (!slot) {
      this._requestRender();
      return;
    }
    await this._commitSlot(device, program, slot);
  }

  // Commits _scheduleChoice's precomputed today slot as-is — the counterpart to _onUseTomorrow.
  async _onUseToday(deviceName) {
    const choice = this._scheduleChoice;
    if (!choice || choice.deviceName !== deviceName || !choice.todaySlot) return;
    const device = this._config.devices.find((d) => d.name === deviceName);
    const program = device?.programs.find((p) => p.duration_minutes === choice.durationMin);
    if (!program) {
      this._scheduleChoice = null;
      this._requestRender();
      return;
    }
    await this._commitSlot(device, program, choice.todaySlot);
  }

  // Commits _scheduleChoice's precomputed tomorrow slot as-is, without recomputing — the shown percentage is exactly what gets written.
  async _onUseTomorrow(deviceName) {
    const choice = this._scheduleChoice;
    if (!choice || choice.deviceName !== deviceName || !choice.tomorrowSlot) return;
    const device = this._config.devices.find((d) => d.name === deviceName);
    const program = device?.programs.find((p) => p.duration_minutes === choice.durationMin);
    if (!program) {
      this._scheduleChoice = null;
      this._requestRender();
      return;
    }
    await this._commitSlot(device, program, choice.tomorrowSlot);
  }

  // Neither field clears on its own — without this, an _onRecalculate prompt would sit on screen indefinitely.
  _dismissSelectionError() {
    this._selectionError = null;
    this._scheduleChoice = null;
    this._requestRender();
  }

  async _onManualTime(deviceName, timeValue) {
    const current = this._activeSelections().find((s) => s.deviceName === deviceName);
    if (!this._config.state_entity || !timeValue || !current) return;
    const next = new Date(current.start || new Date());
    const [h, m] = timeValue.split(":").map(Number);
    next.setHours(h, m, 0, 0);
    await this._setManualStart(deviceName, next);
  }

  // Shared write path for the manual time input and gantt-bar drag — both just compute `next` differently.
  async _setManualStart(deviceName, next) {
    const device = this._config.devices.find((d) => d.name === deviceName);
    const current = this._activeSelections().find((s) => s.deviceName === deviceName);
    if (!this._config.state_entity || !current) return;
    const program = device.programs.find((p) => p.duration_minutes === current.durationMin);
    if (program) {
      const est = this._estimates.get(`${device.power_sensor}::${program.name}`);
      this._pending.set(deviceName, {
        programName: program.name,
        durationMin: program.duration_minutes,
        powerW: est?.powerW,
        profile: est?.profile,
        approximate: est?.approximate,
        start: next,
        end: new Date(next.getTime() + program.duration_minutes * 60000),
      });
      this._requestRender();
    }
    await this._writeSharedEntry(deviceName, { duration: current.durationMin, start: fmtHaDatetime(next) });
  }

  _render() {
    if (!this._hass || !this._config) return;
    // _render() rebuilds the whole DOM every call, so .chart-scroll is new each time — restore scrollLeft or a horizontal scroll gets yanked back to the start.
    const scrollLeft = this.shadowRoot.querySelector(".chart-scroll")?.scrollLeft;
    this._lastSignature = this._relevantSignature();
    const points = this._theoreticalPoints();
    const dark = !!this._hass.themes?.darkMode;
    const colors = dark ? COLORS.dark : COLORS.light;
    const deviceColorList = dark ? DEVICE_COLORS.dark : DEVICE_COLORS.light;
    const deviceColor = (deviceName) => {
      const i = this._config.devices.findIndex((d) => d.name === deviceName);
      return deviceColorList[i % deviceColorList.length];
    };
    // Fixed loads continue the same categorical sequence right after devices, not a shared gray.
    const fixedLoadColor = (index) => deviceColorList[(this._config.devices.length + index) % deviceColorList.length];

    if (!points) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:16px;color:var(--error-color)">
        Entity ${this._config.forecast_entity} doesn't expose a "detailedForecast" attribute (Solcast required).
      </div></ha-card>`;
      return;
    }

    const allWired = !!this._config.state_entity;
    const activeSelections = this._activeSelections();
    const preview = this._previewSchedule(points);
    // Fixed loads recur daily — generate tomorrow's occurrence too once the view extends there.
    const fixedLoads = this._fixedLoadWindows(this._config.forecast_tomorrow_entity ? [0, 1] : [0]);
    const fixedLoadsByIndex = new Map();
    fixedLoads.forEach((load) => {
      if (!fixedLoadsByIndex.has(load.loadIndex)) fixedLoadsByIndex.set(load.loadIndex, []);
      fixedLoadsByIndex.get(load.loadIndex).push(load);
    });
    const { baseLoad } = this._futureSurplusBuckets(points);
    const missing = [];
    for (const device of this._config.devices) {
      for (const program of device.programs) {
        if (!this._estimates.get(`${device.power_sensor}::${program.name}`)) {
          missing.push(`${device.name} – ${program.name}`);
        }
      }
    }

    // Stacked layers: each device, then fixed loads — no base-load layer, that's an internal scheduling estimate, not user-configured (use fixed_loads for a real one).
    const stackLayers = this._config.devices
      .map((device) => {
        const bars = allWired
          ? activeSelections.filter((s) => s.deviceName === device.name && s.start)
          : preview.filter((p) => p.deviceName === device.name && p.start);
        return { color: deviceColor(device.name), bars, confirmed: allWired };
      })
      .concat(
        (this._config.fixed_loads || []).map((load, i) => ({ color: fixedLoadColor(i), bars: fixedLoadsByIndex.get(i) || [], fixed: true }))
      );

    const dayStart = startOfDay(new Date());
    const now = new Date();
    const height = 220;
    const marginLeft = 44;
    const marginRight = 8;
    const marginTop = 14;
    const marginBottom = 24;
    const innerH = height - marginTop - marginBottom;

    // Show only the daylight window (+30 min margin), not a fixed 0h-24h axis, to avoid a mostly-empty chart at night.
    const SUN_MARGIN_MS = 30 * 60 * 1000;
    const sunPoints = points.filter((p) => p.w > 0);
    const daylightStart = sunPoints.length ? new Date(sunPoints[0].time.getTime() - SUN_MARGIN_MS) : dayStart;
    const daylightEnd = sunPoints.length
      ? new Date(sunPoints[sunPoints.length - 1].time.getTime() + SUN_MARGIN_MS)
      : new Date(dayStart.getTime() + DAY_MS);
    // Scheduled items can fall outside daylight hours (least-bad fallback) — widen the view or they'd render off the edge.
    const scheduledTimes = stackLayers.flatMap((layer) => layer.bars.flatMap((b) => (b.start && b.end ? [b.start.getTime(), b.end.getTime()] : [])));
    // Floored at now - 6h (never before midnight) — a full-day fixed load otherwise pulls the default view back to midnight regardless of time of day, hiding what's still ahead.
    const PAST_MARGIN_MS = 6 * 60 * 60 * 1000;
    const pastFloor = Math.max(now.getTime() - PAST_MARGIN_MS, dayStart.getTime());
    const viewStart = new Date(Math.max(Math.min(daylightStart.getTime(), ...scheduledTimes), pastFloor));
    const viewEnd = new Date(Math.max(daylightEnd.getTime(), ...scheduledTimes));
    const viewSpanMs = viewEnd.getTime() - viewStart.getTime();

    // Chart width scales with the visible span, not a flat 100%, so forecast_tomorrow_entity's extra day spills into scroll (.chart-scroll) instead of compressing today.
    const todayEnd = new Date(dayStart.getTime() + DAY_MS);
    const todaySunPoints = sunPoints.filter((p) => p.time < todayEnd);
    const todayDaylightStart = todaySunPoints.length ? new Date(todaySunPoints[0].time.getTime() - SUN_MARGIN_MS) : dayStart;
    const todayDaylightEnd = todaySunPoints.length
      ? new Date(todaySunPoints[todaySunPoints.length - 1].time.getTime() + SUN_MARGIN_MS)
      : todayEnd;
    const todayViewSpanMs = todayDaylightEnd.getTime() - todayDaylightStart.getTime();
    // Fixed px-per-ms density, not a widthScale ratio clamped to 1 — that ratio could stretch a short span across a full day's pixels once viewStart got its own floor.
    const baseInnerW = 600 - marginLeft - marginRight;
    const pxPerMs = baseInnerW / todayViewSpanMs;
    const innerW = Math.max(1, viewSpanMs) * pxPerMs;
    const width = innerW + marginLeft + marginRight;
    // Must be width/600 to keep 1 SVG unit a constant real-px size regardless of `width` (see .chart-scroll/svg.chart CSS).
    const chartWidthPercent = ((width / 600) * 100).toFixed(2);

    // Grid cut at exact phase boundaries, not a fixed step — avoids diluting short spikes (see CLAUDE.local.md).
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
        return { color: layer.color, fixed: layer.fixed, ghost: layer.confirmed === false, w };
      });
      stackedBuckets.push({ start: new Date(t), end: new Date(bEnd), segments, total });
    }

    // Today-only forecast points, not the tomorrow-merged ones, so a sunnier tomorrow can't rescale today's curves — tomorrow's curve just clips at this ceiling.
    const maxW =
      [
        ...points.filter((p) => p.time < todayEnd).map((p) => p.w),
        this._config.max_simultaneous_power,
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
            const cls = seg.fixed ? "stack-fixed" : seg.ghost ? "stack-ghost" : "stack-confirmed";
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
    // Stepped from todayViewSpanMs (fixed), not the total span, so tick density doesn't thin out with forecast_tomorrow_entity.
    const todayViewSpanHours = todayViewSpanMs / 3600000;
    const tickStepHours = Math.max(1, Math.round(todayViewSpanHours / 8));
    const firstTickHour = Math.ceil(viewStart.getTime() / 3600000 / tickStepHours) * tickStepHours;
    for (let h = firstTickHour; h * 3600000 <= viewEnd.getTime(); h += tickStepHours) {
      const t = new Date(h * 3600000);
      hourTicks.push(`<text x="${x(t).toFixed(1)}" y="${height - 6}" class="axis-label" text-anchor="middle">${t.getHours()}h</text>`);
    }
    // Marks the midnight boundary so repeated hour labels ("8h") aren't ambiguous between today and tomorrow.
    const dayTicks = [];
    for (let d = new Date(todayEnd); d < viewEnd; d = new Date(d.getTime() + DAY_MS)) {
      if (d <= viewStart) continue;
      dayTicks.push(
        `<line x1="${x(d).toFixed(1)}" y1="${marginTop}" x2="${x(d).toFixed(1)}" y2="${height - marginBottom}" class="day-line"/>`,
        `<text x="${(x(d) + 4).toFixed(1)}" y="${marginTop + 9}" class="axis-label day-label">Tomorrow</text>`
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

    const maxLineY = y(this._config.max_simultaneous_power).toFixed(1);
    const nowX = x(now).toFixed(1);

    // Color carries device identity; lanes stay thin since hover titles (not lane labels) carry the detail.
    const laneHeight = 10;
    const laneGap = 2;
    const ganttTop = 2;
    const laneCount = this._config.devices.length + this._config.fixed_loads.length;
    const ganttHeight = laneCount * (laneHeight + laneGap) + ganttTop;

    const laneBars = [];
    this._config.devices.forEach((device, i) => {
      const laneY = ganttTop + i * (laneHeight + laneGap);
      const barColor = deviceColor(device.name);
      const bars = allWired
        ? activeSelections
            .filter((s) => s.deviceName === device.name && s.start)
            .map((s) => ({ ...s, confirmed: true }))
        : preview.filter((p) => p.deviceName === device.name && p.start).map((p) => ({ ...p, confirmed: false }));

      const rects = bars
        .map((b) => {
          const bx = x(b.start);
          const bw = Math.max(2, x(b.end) - x(b.start));
          const powerLabel = b.profile
            ? `${fmtWh((b.powerW * b.durationMin) / 60)} · peak ${fmtW(Math.max(...b.profile.map((p) => p.power_w)))}`
            : fmtWh((b.powerW * b.durationMin) / 60);
          const label = `${b.programName} · ${fmtTime(b.start)}–${fmtTime(b.end)} · ${powerLabel}${b.approximate ? " ≈" : ""}`;
          // Only a confirmed bar is draggable — a preview ghost has no real selection to move.
          const dragCls = b.confirmed ? " bar-draggable" : "";
          return `<g class="bar-group"><rect x="${bx.toFixed(1)}" y="${laneY}" width="${bw.toFixed(1)}" height="${laneHeight}" rx="2" style="fill:${barColor}" class="bar ${b.confirmed ? "confirmed" : "ghost"}${dragCls}" data-device="${device.name}"/><title>${label}</title></g>`;
        })
        .join("");
      laneBars.push(rects);
    });
    // Fixed loads are read-only (hatched, "reserved" not "proposed"); a lane can hold today's + tomorrow's occurrence.
    this._config.fixed_loads.forEach((loadConfig, i) => {
      const laneY = ganttTop + (this._config.devices.length + i) * (laneHeight + laneGap);
      const occurrences = fixedLoadsByIndex.get(i) || [];
      const rects = occurrences
        .map((load) => {
          const bx = x(load.start);
          const bw = Math.max(2, x(load.end) - x(load.start));
          const loadPowerLabel = load.profile
            ? `${fmtWh((load.powerW * load.durationMin) / 60)} · peak ${fmtW(Math.max(...load.profile.map((p) => p.power_w)))}`
            : fmtWh((load.powerW * load.durationMin) / 60);
          const label = `${load.programName} (external) · ${fmtTime(load.start)}–${fmtTime(load.end)} · ${loadPowerLabel}`;
          return `<g class="bar-group"><rect x="${bx.toFixed(1)}" y="${laneY}" width="${bw.toFixed(1)}" height="${laneHeight}" rx="2" style="fill:${fixedLoadColor(i)}" class="bar fixed"/><title>${label}</title></g>`;
        })
        .join("");
      laneBars.push(rects);
    });

    // Live coverage for every future committed slot — otherSegments (every other selection/fixed load) is subtracted so overlapping peaks can't both read as fully covered.
    const coveragePctByDevice = new Map();
    for (const s of activeSelections) {
      if (!s.start || s.end <= now) continue;
      const otherSegments = [...activeSelections, ...fixedLoads]
        .filter((o) => o.deviceName !== s.deviceName && o.start && o.end)
        .flatMap((o) => phaseSegments(o));
      coveragePctByDevice.set(s.deviceName, coveragePercent(phaseSegments(s), otherSegments, points, baseLoad, s.start, s.end));
    }

    const unplaced = activeSelections.filter((s) => !s.start);

    const sharedState = this._readSharedState();
    const deviceRows = this._config.devices
      .map((device) => {
        if (!allWired) return "";
        const pending = this._pending.get(device.name);
        const rawCurrent = pending ? pending.durationMin : sharedState[device.name]?.duration;
        const current = rawCurrent != null ? Number(rawCurrent) : NONE_DURATION;
        const selection = activeSelections.find((s) => s.deviceName === device.name);
        const coveragePct = coveragePctByDevice.get(device.name);
        const powerNow = Number(this._hass.states[device.power_sensor]?.state);
        const isRunning = !Number.isNaN(powerNow) && powerNow > this._config.idle_power_threshold;
        const buttons = [
          ...device.programs.map((p) => ({ label: p.name, value: p.duration_minutes })),
          { label: "None", value: NONE_DURATION },
        ]
          .map(
            (opt) =>
              `<button class="program-btn ${current === opt.value ? "active" : ""}" data-device="${device.name}" data-duration="${opt.value}">${opt.label}</button>`
          )
          .join("");
        const knownProgram = device.programs.some((p) => p.duration_minutes === current);
        const slotRow =
          current !== NONE_DURATION && !knownProgram
            ? `<div class="slot-row"><span class="warn-badge"><ha-icon icon="mdi:alert"></ha-icon> stored duration "${current}" doesn't match any program duration configured for ${device.name}.</span></div>`
            : current !== NONE_DURATION
            ? `<div class="slot-row">
                <input type="time" class="slot-time" data-device="${device.name}" value="${selection?.start ? fmtTime(selection.start) : ""}">
                <span class="slot-power">${
                  selection
                    ? selection.profile
                      ? `${fmtWh((selection.powerW * selection.durationMin) / 60)} · peak ${fmtW(Math.max(...selection.profile.map((p) => p.power_w)))}`
                      : fmtWh((selection.powerW * selection.durationMin) / 60)
                    : ""
                }${selection?.approximate ? " ≈" : ""}</span>
                <button class="recalc-btn" data-device="${device.name}">Recalculate</button>
                ${
                  coveragePct != null
                    ? `<span class="coverage-pct ${coveragePct >= 100 ? "coverage-good" : "coverage-low"}">${coveragePct}% solar</span>`
                    : ""
                }
              </div>`
            : "";
        return `<div class="device-select">
          <div class="device-select-header"><span class="swatch" style="background:${deviceColor(device.name)}"></span><span class="device-name">${device.name}</span>${
          isRunning ? `<ha-icon class="running-icon" icon="mdi:play-circle" title="Currently running"></ha-icon>` : ""
        }</div>
          <div class="program-buttons">${buttons}</div>
          ${slotRow}
        </div>`;
      })
      .join("");

    // Fixed loads aren't selectable, but still need a name/swatch legend since the gantt carries no text labels.
    const fixedLoadsLegend = this._config.fixed_loads
      .map(
        (load, i) =>
          `<div class="device-select"><div class="device-select-header"><span class="swatch fixed-swatch" style="background:${fixedLoadColor(i)}"></span><span class="device-name">${load.name} (external)</span></div></div>`
      )
      .join("");

    // "Tomorrow " distinguishes today's/tomorrow's occurrence of a recurring fixed load — same HH:MM otherwise reads as an unexplained duplicate.
    const tableRows = [...activeSelections, ...preview, ...fixedLoads]
      .map((p) => {
        const dayLabel = p.start && p.start >= todayEnd ? "Tomorrow " : "";
        return `<tr>
          <td>${p.deviceName}${p.fixed ? " (external)" : ""}</td><td>${p.fixed ? "-" : p.programName}</td>
          <td>${p.start ? `${dayLabel}${fmtTime(p.start)} – ${fmtTime(p.end)}` : "no window"}</td>
          <td>${fmtWh((p.powerW * p.durationMin) / 60)}${p.approximate ? " (rough estimate)" : ""}</td>
        </tr>`;
      })
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; }
        .header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
        .title { font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); }
        .legend { display: flex; gap: 12px; font-size: 0.95em; color: var(--secondary-text-color); }
        .legend span { display: inline-flex; align-items: center; gap: 4px; }
        .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
        .fixed-swatch { opacity: 0.6; border: 1px dashed var(--secondary-text-color); }
        .chart-scroll { overflow-x: auto; }
        svg.chart, svg.gantt { height: auto; display: block; }
        .grid { stroke: var(--divider-color); stroke-width: 1; }
        .axis-label { fill: var(--secondary-text-color); font-size: 11px; }
        .forecast-line { fill: none; stroke: ${colors.forecast}; stroke-width: 2; }
        .actual-line { fill: none; stroke: ${colors.actual}; stroke-width: 2; }
        .consumption-line { fill: none; stroke: ${colors.consumption}; stroke-width: 2; }
        .stack-segment.stack-confirmed { opacity: 0.55; }
        .stack-segment.stack-ghost { opacity: 0.25; }
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
        .bar.ghost { opacity: 0.4; }
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
        .device-select-header { font-size: 0.85em; color: var(--secondary-text-color); margin-bottom: 4px; }
        .device-select-header .swatch { margin-right: 5px; vertical-align: middle; }
        .running-icon { --mdc-icon-size: 14px; color: var(--success-color, #4caf50); margin-left: 5px; vertical-align: middle; }
        .program-buttons { display: flex; flex-wrap: wrap; gap: 6px; }
        .program-btn { border: 1px solid var(--divider-color); background: none; border-radius: 12px; padding: 3px 10px; font-size: 0.8em; cursor: pointer; color: var(--primary-text-color); }
        .program-btn.active { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        .slot-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 0.85em; }
        .slot-time { border: 1px solid var(--divider-color); border-radius: 4px; background: none; color: var(--primary-text-color); }
        .recalc-btn { background: none; border: none; color: var(--primary-color); cursor: pointer; font-size: 0.85em; padding: 0; }
        .use-today-btn, .use-tomorrow-btn, .dismiss-btn { background: none; border: none; color: var(--primary-color); cursor: pointer; font-size: 0.85em; padding: 0; margin-left: 8px; text-decoration: underline; }
        .warn-badge { color: var(--warning-color, #fab219); display: inline-flex; align-items: center; gap: 3px; font-size: 0.8em; }
        .coverage-pct { font-size: 0.8em; font-weight: 500; }
        .coverage-pct.coverage-good { color: var(--success-color, #4caf50); }
        .coverage-pct.coverage-low { color: var(--warning-color, #fab219); }
        .warnings { margin-top: 10px; font-size: 0.85em; color: var(--warning-color, #fab219); }
        .warnings div { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
        .missing { margin-top: 6px; font-size: 0.8em; color: var(--secondary-text-color); }
        .table-toggle { margin-top: 10px; cursor: pointer; font-size: 0.85em; color: var(--primary-color); background: none; border: none; padding: 0; }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.85em; }
        th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--divider-color); }
      </style>
      <ha-card>
        <div class="header">
          <span class="title">Solar Planner</span>
          <span class="legend">
            <span><span class="swatch" style="background:${colors.forecast}"></span>Forecast</span>
            ${this._actualPoints.length ? `<span><span class="swatch" style="background:${colors.actual}"></span>Real production</span>` : ""}
            ${this._consumptionPoints.length ? `<span><span class="swatch" style="background:${colors.consumption}"></span>Consumption</span>` : ""}
          </span>
        </div>
        <div class="chart-scroll">
          <svg class="chart" viewBox="0 0 ${width} ${height}" style="width: ${chartWidthPercent}%">
            ${wTicks.join("")}
            ${hourTicks.join("")}
            ${dayTicks.join("")}
            ${stackedRects}
            <line x1="${marginLeft}" y1="${maxLineY}" x2="${width - marginRight}" y2="${maxLineY}" class="max-line"/>
            <line x1="${nowX}" y1="${marginTop}" x2="${nowX}" y2="${height - marginBottom}" class="now-line"/>
            <path d="${forecastPath}" class="forecast-line"/>
            ${actualPath ? `<path d="${actualPath}" class="actual-line"/>` : ""}
            ${consumptionPath ? `<path d="${consumptionPath}" class="consumption-line"/>` : ""}
            <g id="hover-group">
              <line class="hover-line" x1="0" y1="${marginTop}" x2="0" y2="${height - marginBottom}"/>
              <circle class="hover-dot" style="fill:${colors.forecast}"/>
              <circle class="hover-dot" style="fill:${colors.actual}"/>
              <circle class="hover-dot" style="fill:${colors.consumption}"/>
              <g class="hover-box"><rect width="140" height="62" rx="3"/><text class="hover-time" x="8" y="16"></text><text class="hover-forecast" x="8" y="30"></text><text class="hover-actual" x="8" y="44"></text><text class="hover-consumption" x="8" y="58"></text></g>
            </g>
            <rect id="hover-catch" class="hover-catch" x="${marginLeft}" y="${marginTop}" width="${innerW}" height="${innerH}"/>
          </svg>
          <svg class="gantt" viewBox="0 0 ${width} ${ganttHeight}" style="width: ${chartWidthPercent}%">${laneBars.join(
            ""
          )}<g class="drag-pct-group" style="opacity:0" pointer-events="none"><rect class="drag-pct-bg" width="60" height="14" rx="2"/><text class="drag-pct" x="30" y="10.5" text-anchor="middle"></text></g></svg>
        </div>
        ${deviceRows}
        ${fixedLoadsLegend}
        ${
          this._selectionError
            ? `<div class="warnings"><div><ha-icon icon="mdi:alert"></ha-icon>${this._selectionError}${
                this._scheduleChoice?.todaySlot
                  ? `<button class="use-today-btn" data-device="${this._scheduleChoice.deviceName}">Use today (${this._scheduleChoice.todayPct}%)</button>`
                  : ""
              }${
                this._scheduleChoice?.tomorrowSlot
                  ? `<button class="use-tomorrow-btn" data-device="${this._scheduleChoice.deviceName}">Use tomorrow (${this._scheduleChoice.tomorrowPct}%)</button>`
                  : ""
              }<button class="dismiss-btn">Dismiss</button></div></div>`
            : ""
        }
        ${
          unplaced.length
            ? `<div class="warnings">${unplaced
                .map((p) => `<div><ha-icon icon="mdi:alert"></ha-icon>No sufficient solar window today for ${p.deviceName} – ${p.programName}.</div>`)
                .join("")}</div>`
            : ""
        }
        ${missing.length ? `<div class="missing">Not enough history to estimate: ${missing.join(", ")}.</div>` : ""}
        <button class="table-toggle" id="toggle-table">${this._showTable ? "Hide" : "Show"} as table</button>
        ${this._showTable ? `<table><thead><tr><th>Device</th><th>Program</th><th>Window</th><th>Energy</th></tr></thead><tbody>${tableRows}</tbody></table>` : ""}
      </ha-card>`;

    // Deferred via double requestAnimationFrame, not a synchronous forced reflow — HA's own grid layout can still be settling a frame after this DOM mutation.
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

    this.shadowRoot.getElementById("toggle-table")?.addEventListener("click", () => {
      this._showTable = !this._showTable;
      this._render();
    });
    this.shadowRoot.querySelectorAll(".program-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._onSelectProgram(btn.dataset.device, Number(btn.dataset.duration)));
    });
    this.shadowRoot.querySelectorAll(".slot-time").forEach((input) => {
      input.addEventListener("change", () => this._onManualTime(input.dataset.device, input.value));
    });
    this.shadowRoot.querySelectorAll(".recalc-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._onRecalculate(btn.dataset.device));
    });
    this.shadowRoot.querySelectorAll(".use-tomorrow-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._onUseTomorrow(btn.dataset.device));
    });
    this.shadowRoot.querySelectorAll(".use-today-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._onUseToday(btn.dataset.device));
    });
    this.shadowRoot.querySelectorAll(".dismiss-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._dismissSelectionError());
    });

    this._bindGanttDrag({ viewStart, viewSpanMs, marginLeft, marginRight, width });
    this._bindHover({ points, viewStart, viewSpanMs, x, y, marginLeft, marginRight, marginTop, height, width, todayEnd });
  }

  // pointermove moves the bar's `x` directly (not via _render(), which would drop pointer capture mid-drag); the live % is the one thing computed fresh each move.
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

    // Same math as the device row's coveragePct badge — cached at pointerdown, only the candidate start is rescored per move.
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
      const deviceName = rect.dataset.device;

      rect.addEventListener("pointerdown", (ev) => {
        const current = this._activeSelections().find((s) => s.deviceName === deviceName && s.start);
        if (!current) return;
        rect.setPointerCapture?.(ev.pointerId);
        const grabOffsetMs = timeAt(ev.clientX, ev.clientY).getTime() - current.start.getTime();
        const points = this._theoreticalPoints();
        const { baseLoad } = this._futureSurplusBuckets(points);
        const others = [
          ...this._activeSelections(),
          ...this._fixedLoadWindows(this._config.forecast_tomorrow_entity ? [0, 1] : [0]),
        ].filter((o) => o.deviceName !== deviceName);
        this._drag = {
          deviceName,
          pointerId: ev.pointerId,
          grabOffsetMs,
          durationMin: current.durationMin,
          currentStart: current.start,
          item: { powerW: current.powerW, profile: current.profile, durationMin: current.durationMin },
          points,
          baseLoad,
          otherSegments: others.filter((o) => o.start && o.end).flatMap((o) => phaseSegments(o)),
        };
        showPct(this._drag, parseFloat(rect.getAttribute("x")), parseFloat(rect.getAttribute("y")), parseFloat(rect.getAttribute("height")));
      });

      rect.addEventListener("pointermove", (ev) => {
        const drag = this._drag;
        if (!drag || drag.pointerId !== ev.pointerId || drag.deviceName !== deviceName) return;
        const rawStartMs = timeAt(ev.clientX, ev.clientY).getTime() - drag.grabOffsetMs;
        drag.currentStart = new Date(snapToGrid(rawStartMs));
        const bx = marginLeft + ((drag.currentStart.getTime() - viewStart.getTime()) / viewSpanMs) * innerW;
        rect.setAttribute("x", bx.toFixed(1));
        showPct(drag, bx, parseFloat(rect.getAttribute("y")), parseFloat(rect.getAttribute("height")));
      });

      const commit = async (ev) => {
        const drag = this._drag;
        if (!drag || drag.pointerId !== ev.pointerId || drag.deviceName !== deviceName) return;
        this._drag = null;
        pctGroup.style.opacity = "0";
        await this._setManualStart(deviceName, drag.currentStart);
      };
      rect.addEventListener("pointerup", commit);
      rect.addEventListener("pointercancel", (ev) => {
        if (this._drag?.pointerId === ev.pointerId) this._drag = null;
        pctGroup.style.opacity = "0";
        this._requestRender();
      });
    });
  }

  _bindHover({ points, viewStart, viewSpanMs, x, y, marginLeft, marginRight, marginTop, height, width, todayEnd }) {
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
    const actualText = box.querySelector(".hover-actual");
    const consumptionText = box.querySelector(".hover-consumption");

    // Only line/box here — the dots' opacity is per-point (set in mousemove); setting it here too would clobber that.
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
      // interpolate() clamps past a curve's end instead of returning null — blank each series past its own real end, or hovering past it shows a stale value.
      const inToday = t < todayEnd;
      const wForecast = points.length && t <= points[points.length - 1].time ? interpolate(points, t) : null;
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
      actualText.textContent = wActual != null ? `Real production: ${fmtW(wActual)}` : "";
      consumptionText.textContent = wConsumption != null ? `Consumption: ${fmtW(wConsumption)}` : "";
      // Flips against the visible (scrolled) viewport's right edge, not the total SVG width, so the tooltip can't render into the clipped-away region.
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
  description: "Schedules your appliances based on forecast solar production and available surplus.",
});
