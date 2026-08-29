# Solar Planner Scheduler

Home Assistant custom integration (HACS, category "Integration") that computes, server-side and
unattended, the best time of day to run a device given a solar forecast — no dashboard needs to be
open for the schedule to be computed or acted on. It bundles its own Lovelace card (auto-registered,
no separate install) as a pure display layer over the integration's entities.

The scheduling math (`custom_components/solar_planner_scheduler/scheduling.py`) is a line-for-line
Python port of the card's own pure JS functions, verified against the same test scenarios
(`frontend/tests/scheduling.test.js` -> `tests/test_scheduling.py`).

## What it does, and what it deliberately doesn't

- Every `update_interval` (default 15 min), it reads your forecast/surplus/production entities and
  recomputes the best start time for each device's selected program, exposing it as a `sensor` (the
  timestamp), a `binary_sensor` (on for the scheduled window), a `select` (which program is active),
  a `switch` (manual mode) and a `datetime` (the manual start time).
- It does **not** turn any device on or off itself. That choice is left to a plain Home Assistant
  automation reacting to the `binary_sensor` — this keeps the integration usable for any device
  type (a switch, a climate entity, a script) without hardcoding how to control any of them.

## Configuration

Initial setup (`Settings -> Devices & services -> Add integration -> Solar Planner Scheduler`)
asks for the base entities, shared by every device:

- Forecast entity (e.g. `sensor.solcast_pv_forecast_forecast_today`)
- Forecast entity for tomorrow (optional)
- Grid surplus/return entity
- Solar production entity (optional — preferred over the forecast for "now" when available)
- House consumption entity (currently unused, reserved)
- Max simultaneous power (W) — a hard breaker-safety ceiling, never exceeded regardless of solar coverage

Devices, their programs, and fixed loads are managed afterwards via the integration's "Configure"
button (options flow menu):

- **Add/remove a device** — just a name and an optional power sensor. A new device has no programs
  until one is added.
- **Add a program** — a step-by-step wizard: pick the device, name the program, then add one or
  more phases (duration in minutes + power in W). Multi-phase programs (e.g. a washing machine
  cycle with a spin-dry spike) are fully supported; a program can also be a single phase.
- **Remove a program** — a device can hold several programs at once; the `select` entity lets you
  choose which one (or "None") is currently active.
- **Add/remove a fixed load** — a recurring, non-schedulable consumption (e.g. a baseline load)
  with a start time, power, and duration; it's subtracted from available solar capacity when
  scheduling devices, and also exposed read-only to the card so it doesn't need its own config.

## Manual mode and pending choices

Each device has a `switch.<device>_manual_mode`: off (default), the coordinator searches for the
best slot automatically; on, it uses `datetime.<device>_manual_start` instead. When today's
forecast is good enough on its own but tomorrow's could be better, the sensor exposes a "pending
choice" (`today_coverage_pct` / `tomorrow_coverage_pct` attributes) and the `accept_today` /
`accept_tomorrow` services commit one of the two.

## Wiring up a real device

Example automation that actually turns a switch on/off for the scheduled window, using the
`should_run` binary sensor:

```yaml
automation:
  - alias: "Solar Planner Scheduler - Water heater ON"
    trigger:
      - platform: state
        entity_id: binary_sensor.water_heater_should_run
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.water_heater_boost

  - alias: "Solar Planner Scheduler - Water heater OFF"
    trigger:
      - platform: state
        entity_id: binary_sensor.water_heater_should_run
        to: "off"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.water_heater_boost
```

## The bundled card

The Lovelace card (`custom_components/solar_planner_scheduler/www/solar-planner-card.js`) is served
and registered automatically by the integration — no separate HACS install or manual resource
registration needed. Its YAML config only lists which devices to display; everything else
(forecast/surplus/production/consumption entities, fixed loads) is read from the integration's own
`sensor.solar_planner_scheduler_config` entity, so there's nothing to duplicate:

```yaml
type: custom:solar-planner-card
devices:
  - lave_linge
  - lave_vaisselle
  - eau_chaude
```

The card is a pure display/interaction layer: it renders the forecast, the scheduled windows and
fixed loads, and drag-to-reschedule / program selection / manual mode controls — all scheduling
math stays server-side in `scheduling.py`.

## Development

```bash
pytest tests/            # Python (scheduling.py, coordinator helpers)
cd frontend && node --test   # JS (card rendering, mirrored scheduling functions)
```

CI runs both suites plus `hassfest` and HACS validation on every push/PR; Dependabot keeps the
GitHub Actions and `requirements-dev.txt` pins up to date.
