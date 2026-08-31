# Solar Planner Scheduler

Home Assistant integration (HACS) that schedules devices around your solar forecast, server-side,
and displays them via a bundled Lovelace card. Scheduling math lives in `scheduling.py`, a
line-for-line Python port of the card's own JS functions, tested against the same scenarios.

![Solar Planner card](docs/card.png)

## Requirements

A solar forecast entity already set up in Home Assistant (e.g. the
[Solcast](https://github.com/BJReplay/ha-solcast-solar) integration). Solar Planner Scheduler
doesn't produce forecasts itself, it schedules around one you already have.

## Installation

Via HACS (custom repository, not yet in the default store):

1. HACS -> the three-dot menu (top right) -> Custom repositories.
2. URL: `https://github.com/Rom1-B/solar-planner-scheduler`, category: Integration.
3. Install "Solar Planner Scheduler", restart Home Assistant.
4. Settings -> Devices & services -> Add integration -> Solar Planner Scheduler.

Or manually: copy `custom_components/solar_planner_scheduler/` into your HA `config/custom_components/`, restart, then add the integration the same way.

## Configuration

Initial setup asks for the shared entities (forecast, forecast tomorrow, production, consumption,
max simultaneous power). Devices, programs and fixed loads are then managed via
"Configure", which returns to its own menu after each action so several changes can be made in one
session:

- **Device**: name + optional power sensor.
- **Program**: pick a device, name it, then enter its phases, one per line, e.g. `20min@150W` or
  `1.5h@800W` (multi-phase supported), and optionally check which days of the week it should keep
  auto-repeating on its own, unattended. A device can have several programs; `select.<device>_program`
  picks the active one ("None" disables it). "Edit a program's phases" replaces both the phases and
  the auto-schedule days in place, pre-filled with the current ones.
- **Fixed load**: a recurring non-schedulable load (start time + phases, same syntax as programs),
  subtracted from available solar capacity. Also editable in place.

A device is scheduled once per program selection: picking a program (or switching a device from
manual to auto) always searches for today's best slot immediately, regardless of auto-schedule
days. Once that committed slot has run (its window has elapsed), it won't be proposed a second slot
that day, and won't keep rescheduling itself on later days unless the auto-schedule days are checked
for one of them — no separate "reset" needed, picking the program again (or a new day landing on an
auto-schedule day) naturally triggers a fresh search. Leaving auto-schedule days unchecked is a
legitimate choice for on-demand devices with no fixed rhythm (e.g. a dishwasher run whenever it's
loaded): it just means the device won't repeat on its own, not that it can't be scheduled. Manual
mode (`switch.<device>_manual_mode`) is a separate, unrestricted override: it pins the start time to
`datetime.<device>_manual_start` regardless of the program's auto-schedule days. When today is good
enough but tomorrow could be better, the sensor exposes a pending choice
(`today_coverage_pct`/`tomorrow_coverage_pct`); resolve it with the `accept_today`/`accept_tomorrow`
services.

Only declared consumers (fixed loads, scheduled devices) are subtracted from available solar —
there's no live "background consumption" estimate, since a single instantaneous reading spikes
whenever any large load happens to be running right at update time and that spike would otherwise
get stretched across the whole scheduling horizon.

The config flow UI is available in English and French, following your Home Assistant language.

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

When the forecast entity's `detailedForecast` includes Solcast's `pv_estimate10`/`pv_estimate90`
percentiles, the chart also shows a shaded confidence band around the forecast line (and the
P10-P90 range on hover) — a narrow band means a stable sky (predictable forecast), a wide one means
variable cloud cover (the actual solar window may shift). Absent on forecast sources without these
percentiles.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/      # Python (scheduling.py, config_flow.py/coordinator.py via pytest-homeassistant-custom-component)
cd frontend && node --test   # JS
```

CI runs both suites plus `hassfest` and HACS validation on every push/PR. `./scripts/check-ci.sh`
reproduces all four locally (same Docker images CI uses) if you want to check before pushing; it's
slow, so it's opt-in, not part of the regular loop.

## Releasing

Bump `version` in `custom_components/solar_planner_scheduler/manifest.json` and push to `main`.
Once `Tests`, `Hassfest` and `HACS` have all passed on that commit, `.github/workflows/release.yml`
creates a matching GitHub release (tag + auto-generated notes) if one doesn't already exist for that
version — HACS needs a release to track (otherwise it falls back to a commit SHA, which fails to
download; see the project's CLAUDE.local.md for why).

For visually testing UI-facing changes (config flow forms, entities, the card) without pushing to
a real Home Assistant instance, run a throwaway local one with Docker:

```bash
docker compose up -d
```

Open `http://localhost:8123`, finish onboarding, and add the integration. After each code change:
`docker compose restart`, then reload the page (`.dev-config/` is gitignored, safe to delete to
start over).
