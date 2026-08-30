# Solar Planner Scheduler

Home Assistant integration (HACS) that schedules devices around your solar forecast, server-side,
and displays them via a bundled Lovelace card. Scheduling math lives in `scheduling.py`, a
line-for-line Python port of the card's own JS functions, tested against the same scenarios.

![Solar Planner card](docs/card.png)

## Requirements

A solar forecast entity already set up in Home Assistant (e.g. the
[Solcast](https://github.com/BJReplay/ha-solcast-solar) integration) and a grid surplus/return
entity. Solar Planner Scheduler doesn't produce forecasts itself, it schedules around one you
already have.

## Installation

Via HACS (custom repository, not yet in the default store):

1. HACS -> the three-dot menu (top right) -> Custom repositories.
2. URL: `https://github.com/Rom1-B/solar-planner-scheduler`, category: Integration.
3. Install "Solar Planner Scheduler", restart Home Assistant.
4. Settings -> Devices & services -> Add integration -> Solar Planner Scheduler.

Or manually: copy `custom_components/solar_planner_scheduler/` into your HA `config/custom_components/`, restart, then add the integration the same way.

## Configuration

Initial setup asks for the shared entities (forecast, forecast tomorrow, surplus, production,
consumption, max simultaneous power). Devices, programs and fixed loads are then managed via
"Configure", which returns to its own menu after each action so several changes can be made in one
session:

- **Device**: name + optional power sensor.
- **Program**: pick a device, name it, then enter its phases, one per line, e.g. `20min@150W` or
  `1.5h@800W` (multi-phase supported). A device can have several programs; `select.<device>_program`
  picks the active one ("None" disables it). "Edit a program's phases" replaces them in place,
  pre-filled with the current ones.
- **Fixed load**: a recurring non-schedulable load (start time + phases, same syntax as programs),
  subtracted from available solar capacity. Also editable in place.

Manual mode (`switch.<device>_manual_mode`) pins the start time to `datetime.<device>_manual_start`
instead of the computed slot. When today is good enough but tomorrow could be better, the sensor
exposes a pending choice (`today_coverage_pct`/`tomorrow_coverage_pct`); resolve it with the
`accept_today`/`accept_tomorrow` services.

## Entities

Per device: `sensor.<device>_next_start` (timestamp + `coverage_pct`), `binary_sensor.<device>_should_run`,
`select.<device>_program`, `switch.<device>_manual_mode`, `datetime.<device>_manual_start`.

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

Served and registered automatically, no separate install. Its config only lists devices; everything
else is read from `sensor.solar_planner_scheduler_config`:

```yaml
type: custom:solar-planner-card
devices:
  - lave_linge
  - lave_vaisselle
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/      # Python (scheduling.py, config_flow.py via pytest-homeassistant-custom-component)
cd frontend && node --test   # JS
```

CI runs both suites plus `hassfest` and HACS validation on every push/PR. `./scripts/check-ci.sh`
reproduces all four locally (same Docker images CI uses) if you want to check before pushing; it's
slow, so it's opt-in, not part of the regular loop.

For visually testing UI-facing changes (config flow forms, entities, the card) without pushing to
a real Home Assistant instance, run a throwaway local one with Docker:

```bash
docker compose up -d
```

Open `http://localhost:8123`, finish onboarding, and add the integration. After each code change:
`docker compose restart`, then reload the page (`.dev-config/` is gitignored, safe to delete to
start over).
