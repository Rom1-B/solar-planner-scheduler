# Solar Planner Scheduler

Home Assistant integration that schedules devices around your solar forecast and shows them on a
bundled Lovelace card.

![Solar Planner card](docs/card.png)

## Requirements

A solar forecast entity already set up in Home Assistant (e.g.
[Solcast](https://github.com/BJReplay/ha-solcast-solar)).

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

- **Device**: name + optional power sensor.
- **Program**: pick a device, name it, then list its phases, one per line, e.g. `20min@150W` or
  `1.5h@800W`, and optionally check which days it should auto-repeat on. A device can have several
  programs active at once; `switch.<device>_<program>_active` turns each one on or off.
- **Fixed load**: a recurring non-schedulable load (start time + phases), subtracted from
  available solar capacity.
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

Served and registered automatically, no separate install. Its config only lists devices:

```yaml
type: custom:solar-planner-card
devices:
  - lave_linge
  - lave_vaisselle
```

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
