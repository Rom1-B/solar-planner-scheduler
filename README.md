# 🔆 Solar Planner Scheduler

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/Rom1-B/solar-planner-scheduler)](https://github.com/Rom1-B/solar-planner-scheduler/releases)
[![CI](https://github.com/Rom1-B/solar-planner-scheduler/actions/workflows/ci.yml/badge.svg)](https://github.com/Rom1-B/solar-planner-scheduler/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/Rom1-B/solar-planner-scheduler)](LICENSE)

**Run your appliances on sunshine, not on the grid.**
Tell Home Assistant your washing machine takes 2 hours: Solar Planner Scheduler finds the best 2-hour window in today's solar forecast, shows it on a bundled card, and flips a sensor on when it's time to start.

![Solar Planner card](https://raw.githubusercontent.com/Rom1-B/solar-planner-scheduler/main/docs/card.png)

## ✨ Features

- **Best slot from your solar forecast**: each program gets the window where the forecast covers the largest share of its consumption, recomputed as the forecast updates.
- **Realistic power profiles**: describe a program as phases (`20min@150W`, then `1.5h@800W`), so a short heating peak is placed under the sun, not just the average draw.
- **Self-learning**: with a power sensor, the integration detects real starts and refines each program's phases from actual runs.
- **Shares the sunshine**: several devices never get scheduled beyond your maximum simultaneous power, and two programs of the same machine never overlap.
- **Off-peak aware** (optional): with your tariff bands, it picks the cheapest window, solar or off-peak grid, and shows the estimated cost or savings.
- **Payback tracking** (optional): a sensor reports what your panels earned since installation and projects the payback date, from your Energy dashboard's data.
- **Bundled Lovelace card**: forecast curve with confidence band, a gantt of planned runs, drag a bar to force a time, summary table. Registered automatically.

## 📋 Requirements

A solar forecast already set up in Home Assistant, from at least one of these:

- [Solcast](https://github.com/BJReplay/ha-solcast-solar)
- [Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast)
- the built-in [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)

With several providers, you can switch between them from the card, or blend them ("Average", "Min").

Optional: a power sensor per device (start detection and phase learning), and an Energy dashboard with a solar source and a grid import price (payback tracking).

## 📦 Installation

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Rom1-B&repository=solar-planner-scheduler&category=integration)

1. In HACS, search for "Solar Planner Scheduler" and install it.
2. Restart Home Assistant.
3. Add the integration:

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=solar_planner_scheduler)

## ⚙️ Configuration

Initial setup asks for the forecast entities and the maximum simultaneous power.
Everything else is managed from Settings > Devices & services > Solar Planner Scheduler > Configure, a menu that stays open so several changes can be made in one go.

| Item | What it is |
|------|------------|
| **Device** | A physical appliance: a name and an optional power sensor, used to detect when a program actually starts and to refine its phases over time. "Manage a device" edits it and its programs. |
| **Program** | One cycle of a device, as phases, one per line: `<duration>min@<watts>W` or `<duration>h@<watts>W` (e.g. `20min@150W` then `1.5h@800W` for heat then spin). Rough values are fine: with a power sensor, real runs correct them. Optionally pick the days it should auto-run on. |
| **Fixed load** | Something drawing power that you don't control (pool pump, heat pump on a timer), with the same phases format. It's subtracted from available solar when scheduling everything else. |
| **Tariffs** (optional) | Price bands, `HH:MM@price`, one per line. Once enabled, scheduling picks the cheapest window instead of just the sunniest one. |
| **Solar savings** (optional) | Installation date, net cost, and optionally the date resale started. Meters and prices come from your Energy dashboard, which needs a solar source and a grid import price. |

### Scheduling behavior

- Turning a program on searches today's best slot immediately.
- It repeats on later days only for the days checked in its auto-run schedule; without any, it turns itself off after that one run.
- Until a slot is about to start, the search keeps chasing a better window as the forecast updates; once imminent or running, the slot is locked.
- Drag a bar on the card, or edit the `datetime` entity, to force a start time. "Auto" cancels it and searches again.
- A device can have several programs active the same day: they're placed one after the other, never in parallel.

## 🔌 Entities

Per (device, program) pair:

| Entity | Description |
|--------|-------------|
| `switch.<device>_<program>_active` | Whether this program is scheduled. |
| `datetime.<device>_<program>_start` | Next start time, computed or forced. Attributes include `coverage_pct` (share covered by solar), `profile`, `locked`, and with tariffs, the estimated cost and savings. |
| `binary_sensor.<device>_<program>_should_run` | On during the scheduled window: trigger your automations from it. |

Integration-wide:

| Entity | Description |
|--------|-------------|
| `select.solar_planner_scheduler_forecast_source` | Which forecast drives scheduling, including "Average"/"Min" blends with several providers. |
| `sensor.solar_planner_scheduler_current_price` | Live €/kWh price (tariff tracking on), usable as the Energy dashboard's grid cost entity. |
| `sensor.solar_planner_scheduler_savings` | Money earned by solar since installation (solar tracking on). See below. |

<details>
<summary>How savings and payback are computed</summary>

Recomputed every hour from Home Assistant's long-term statistics and the Energy dashboard's configuration.
For each hour: (solar minus grid export) at the import price (`self_consumption_savings`), plus grid export at the export price (`resale_revenue`, 0 without an export price, counted only from the optional resale start date, e.g. a resale contract signed after installation).
Payback attributes: `investment`, `remaining`, `progress` (%), `payback_date` (projected from the average daily gains so far) and `estimate_reliable`, false until a full year of data, since a projection made from summer months alone is optimistic.

</details>

## 🤖 Automations

The integration never turns a device on or off itself: pair each program with an automation.

### Blueprint (recommended)

Pick the program's `should_run` sensor, then what to do when the window starts and, optionally, when it ends: turn a plug on and off, send a notification, run a script.

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FRom1-B%2Fsolar-planner-scheduler%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fsolar_planner_scheduler%2Frun_on_schedule.yaml)

### Notify before a manual start

For a device you start by hand (not controlled by Home Assistant), get notified ahead of time, so there's time to load the washing machine and press its own start button.

<details>
<summary>Example: notify 15 minutes before</summary>

```yaml
automation:
  - alias: "Washing machine - notify 15 min before start"
    trigger:
      - platform: template
        value_template: >
          {% set start_ts = as_timestamp(states('datetime.washing_machine_start'), 0) %}
          {% set remaining_s = start_ts - as_timestamp(now()) %}
          {{ 0 <= remaining_s < 900 }}
    action:
      - service: notify.mobile_app_my_phone
        data:
          message: >
            {% set start_ts = as_timestamp(states('datetime.washing_machine_start')) %}
            {% set eta_min = ((start_ts - as_timestamp(now())) / 60) | round(0) %}
            Washing machine starts at {{ start_ts | timestamp_custom('%H:%M') }} (in {{ eta_min }} min)
```

Change `900` (15 minutes, in seconds) to whatever lead time you want.

</details>

## 📊 The card

Minimal example:

```yaml
type: custom:solar-planner-card
devices: "*"
```

| Option | Default | Description |
|--------|---------|-------------|
| `devices` | none | Device slugs to show, in order (e.g. `[lave_linge, lave_vaisselle]`), or `"*"` (a plain string) for every device in config order. Omit for a forecast-only card. |
| `production_entity` | none | Real production sensor, drawn on the chart. |
| `consumption_entity` | none | Real consumption sensor, drawn on the chart. |
| `chart_expanded` | `true` | Starting state of the chart section. |
| `table_expanded` | `false` | Starting state of the summary table. |
| `table_show_energy` | `true` | Energy column in the table. |
| `table_show_cost` | `true` | Cost and/or Savings column in the table, per `cost_display`. |
| `table_show_total` | `false` | Totals row summing Cost/Savings. |
| `cost_display` | `cost` | `cost`, `savings`, or `both`, for the program badge and the table. |
| `chart_hours_past` | `6` | Hours shown before now. |
| `chart_hours_future` | `24` | Hours shown after now. |
| `chart_visible_hours` | past + future | Hours that fit on screen before the chart scrolls. |

## 🛠️ Troubleshooting

- **A device never seems to start**: if its power sensor never shows it running, the integration reschedules it and, after repeated misses, raises a warning in Settings > System > Repairs. Check the automation reacting to `should_run`.
- **Diagnostics**: Settings > Devices & services > Solar Planner Scheduler > Download diagnostics, for the configuration and the scheduler's internal state.
- Found a bug? [Open an issue](https://github.com/Rom1-B/solar-planner-scheduler/issues) with the diagnostics attached.
