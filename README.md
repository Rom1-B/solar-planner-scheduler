# Solar Planner Scheduler

Home Assistant integration that schedules devices around your solar forecast and shows them on a
bundled Lovelace card. Example: tell it your washing machine takes 2h, and it picks the best
2h window today to run it on solar surplus (or, with tariff tracking on, whichever window is
cheapest, solar or off-peak grid).

![Solar Planner card](docs/card.png)

## Requirements

A solar forecast entity already set up in Home Assistant (e.g.
[Solcast](https://github.com/BJReplay/ha-solcast-solar)), unless you use the built-in
self-computed forecast instead (see "Forecast source" below).

## Installation

Via HACS (custom repository, not yet in the default store):

1. HACS -> the three-dot menu (top right) -> Custom repositories.
2. URL: `https://github.com/Rom1-B/solar-planner-scheduler`, category: Integration.
3. Install "Solar Planner Scheduler", restart Home Assistant.
4. Settings -> Devices & services -> Add integration -> Solar Planner Scheduler.

Or manually: copy `custom_components/solar_planner_scheduler/` into your HA
`config/custom_components/`, restart, then add the integration the same way.

## Configuration

Initial setup asks for the shared entities (forecast, forecast tomorrow, production, consumption,
max simultaneous power). Devices, programs and fixed loads are managed via "Configure":

- **Forecast source**: "Entity" (default, unchanged) uses a forecast entity like today. "Computed"
  calculates production itself from your panels' capacity (kWc), azimuth, tilt and a system loss %
  (your position is read from Home Assistant's own configured location), fetching an irradiance
  forecast from [Open-Meteo](https://open-meteo.com/) (free, no API key; pick a weather model,
  e.g. Météo-France's AROME for high resolution in France). Filling in the panel parameters starts
  this computation regardless of which source is actually selected, so you can compare it (via
  `sensor.solar_planner_scheduler_computed_forecast`, a plain History graph card next to your
  existing forecast entity and `production_entity`) before switching over.
- **Device**: name + optional power sensor.
- **Program**: pick a device, name it, then list its phases, i.e. its power draw over time, one
  line per step (e.g. a washing machine: `20min@150W` to heat, then `1.5h@800W` to spin), and
  optionally check which days it should auto-repeat on. A device can have several programs active
  at once; `switch.<device>_<program>_active` turns each one on or off.
- **Fixed load**: something that also draws power but that this integration can't move or
  control (a pool pump, a fridge cycle), so it's just subtracted from available solar capacity
  when scheduling everything else.
- **Tariffs** (optional): enable tariff tracking, set a monthly subscription price and price bands
  (`HH:MM@price`, one per line, e.g. `22:00@0.1589`). Slot selection always minimizes estimated
  cost, falling back to solar coverage when tracking is off; the real price only shows once enabled.

Turning a program on searches for today's best slot immediately, extending up to 6h past midnight
to reach an overnight tariff band. Once it has run, it repeats on a later day only if that day is
checked in its auto-schedule days. Programs with no auto-schedule days stay off until you turn them
on; programs with auto-schedule days turn on by default.

`datetime.<device>_<program>_start` shows the next start time. Drag its bar on the card, or edit
the entity directly, to force a time. Click "Auto" to cancel a forced time and search again.

## Entities

Per (device, program) pair: `datetime.<device>_<program>_start`,
`binary_sensor.<device>_<program>_should_run`, `switch.<device>_<program>_active`.

`sensor.solar_planner_scheduler_current_price` exposes the live €/kWh price (when tariff tracking
is enabled), usable as the Energy dashboard's "current price" source for grid-consumption cost.

`sensor.solar_planner_scheduler_computed_forecast` exposes the self-computed forecast (see
"Forecast source" above) whenever panel parameters are configured: current power plus the full
hourly curve as a `detailedForecast` attribute, in the same shape a Solcast entity exposes it.

The integration never turns a device on/off itself: react to `should_run` in an automation.

```yaml
automation:
  - alias: "Water heater ON"
    trigger:
      - platform: state
        entity_id: binary_sensor.water_heater_should_run
        to: "on"
    action:
      - service: switch.turn_on
        target: { entity_id: switch.water_heater_boost }
```

## The bundled card

Served and registered automatically, no separate install.

```yaml
type: custom:solar-planner-card
devices:
  - lave_linge
  - lave_vaisselle
chart_expanded: true    # optional, default true (the forecast chart/gantt/device rows)
table_expanded: false   # optional, default false (the summary table)
table_show_energy: true # optional, default true (the table's Energy column)
table_show_cost: true   # optional, default true (the table's Cost column)
```

Each section has its own toggle icon in the card itself; `chart_expanded`/`table_expanded`
only set which state it starts in. The table's Window column shows a countdown to a future start
(e.g. `08:30 - 10:00 (in 2h15m)`).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/      # Python
cd frontend && node --test   # JS
```

CI runs both suites plus `hassfest` and HACS validation. `./scripts/check-ci.sh` reproduces them
locally.

## Releasing

Bump `version` in `custom_components/solar_planner_scheduler/manifest.json` and push to `main`.
CI tags that version and creates a matching GitHub release.

## Local testing

```bash
docker compose up -d
```

Open `http://localhost:8123`, finish onboarding, and add the integration. After each code change:
`docker compose restart`, then reload the page.
