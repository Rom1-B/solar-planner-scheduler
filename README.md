# Solar Planner Scheduler

Home Assistant custom integration (HACS, category "Integration") that computes, unattended, the
best time of day to run a device given a solar forecast — no dashboard needs to be open. It is the
server-side counterpart to
[solar-planner-card](https://github.com/Rom1-B/solar-planner-card): the scheduling math
(`custom_components/solar_planner_scheduler/scheduling.py`) is a line-for-line Python port of that
project's pure JS functions, verified against the same test scenarios
(`tests/scheduling.test.js` -> `tests/test_scheduling.py`).

## What it does, and what it deliberately doesn't

- Every `update_interval` (default 15 min), it reads your forecast/surplus/production entities and
  recomputes the best start time for each configured device, exposing it as a `sensor` (the
  timestamp) and a `binary_sensor` (on for the scheduled window).
- It does **not** turn any device on or off itself. That choice is left to a plain Home Assistant
  automation reacting to the `binary_sensor` — this keeps the integration usable for any device
  type (a switch, a climate entity, a script) without hardcoding how to control any of them.

## Status

Only `scheduling.py` has been verified (via `pytest`, ported case-by-case from the JS reference).
`config_flow.py`, `coordinator.py`, `sensor.py`, `binary_sensor.py`, and `__init__.py` have **not**
been run against a live Home Assistant instance yet — install via HACS (custom repository) or by
copying `custom_components/solar_planner_scheduler/` into your HA config, then check Settings ->
Devices & services -> Add integration.

## Configuration

Initial setup (`Settings -> Devices & services -> Add integration -> Solar Planner Scheduler`)
asks for:

- Forecast entity (e.g. `sensor.solcast_pv_forecast_forecast_today`)
- Forecast entity for tomorrow (optional)
- Grid surplus/return entity
- Solar production entity (optional — preferred over the forecast for "now" when available)
- House consumption entity (currently unused, reserved)
- Max simultaneous power (W) — a hard breaker-safety ceiling, never exceeded regardless of solar coverage

Devices and fixed loads are added afterwards via the integration's "Configure" button (options
flow): each device needs a name, an optional power sensor, a flat power (W), and a duration
(minutes). `power_profile`-style multi-phase devices (like solar-planner-card supports) aren't
exposed in the config UI yet — `scheduling.py` already supports them, only the form doesn't.

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

## Development

```bash
pytest tests/
```
