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
  auto-repeating on its own, unattended. A device can have several programs, and several of a
  device's programs can be active at once (e.g. two different wash cycles the same day) —
  `switch.<device>_<program>_active` turns each one on or off independently. The only constraint is
  that a device's own programs never overlap in time; a different device is only limited by the
  shared power budget. "Edit a program's phases" replaces both the phases and the auto-schedule
  days in place, pre-filled with the current ones.
- **Fixed load**: a recurring non-schedulable load (start time + phases, same syntax as programs),
  subtracted from available solar capacity. Also editable in place.

A program is scheduled once per activation: turning its switch on always searches for today's best
slot immediately, regardless of auto-schedule days. Once that committed slot has run (its window has
elapsed), it won't be proposed a second slot that day, and won't keep rescheduling itself on later
days unless the auto-schedule days are checked for one of them, no separate "reset" needed,
toggling the switch off then on again (or a new day landing on an auto-schedule day) naturally
triggers a fresh search. Leaving auto-schedule days unchecked is a legitimate choice for on-demand
programs with no fixed rhythm (e.g. a dishwasher run whenever it's loaded): it just means the
program won't repeat on its own, not that it can't be scheduled — and it stays off by default until
you turn it on. A program with auto-schedule days, by contrast, defaults to on the first time it's
ever seen (no need to turn it on by hand after installing or updating).

`datetime.<device>_<program>_start` shows the computed next start and doubles as a manual override:
dragging the bar on the card, or editing the entity directly, forces that time (its `locked`
attribute turns `true`). Click the "Auto" button (or call the `reset_to_auto` service) to cancel a
forced time and let the coordinator search again immediately. The coordinator always commits the
best slot it finds for today, however mediocre; if you'd rather wait for a better day, the forecast
curve and drag-and-drop preview already show whether that's worth it, force a time yourself if so.
Dragging a program's bar onto a window that overlaps another active program of the *same* device is
not blocked — the same-device exclusion only applies to the automatic search, not to an explicit
manual override.

Only declared consumers (fixed loads, scheduled devices) are subtracted from available solar —
there's no live "background consumption" estimate, since a single instantaneous reading spikes
whenever any large load happens to be running right at update time and that spike would otherwise
get stretched across the whole scheduling horizon.

The config flow UI is available in English and French, following your Home Assistant language.

## Entities

Per (device, program) pair: `datetime.<device>_<program>_start` (computed or forced start time,
`coverage_pct`/`profile`/`locked` attributes), `binary_sensor.<device>_<program>_should_run`,
`switch.<device>_<program>_active`.

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
