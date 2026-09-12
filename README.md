# 🔆 Solar Planner Scheduler

Home Assistant integration that schedules devices around your solar forecast and shows them on a
bundled Lovelace card. Example: tell it your washing machine takes 2h, and it picks the best
2h window today to run it on solar surplus (or, with tariff tracking on, whichever window is
cheapest, solar or off-peak grid).

![Solar Planner card](docs/card.png)

## 📋 Requirements

A solar forecast already set up in Home Assistant, from one of these:

- [Solcast](https://github.com/BJReplay/ha-solcast-solar)
- [Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast)
- the built-in [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)

You can set up several: the card then lets you switch between them, or blend them ("Average",
"Min", and "Weighted" if you have both Solcast and Helios Forecast).

## 📦 Installation

Via HACS (custom repository, not yet in the default store):

1. HACS -> the three-dot menu (top right) -> Custom repositories.
2. URL: `https://github.com/Rom1-B/solar-planner-scheduler`, category: Integration.
3. Install "Solar Planner Scheduler", restart Home Assistant.
4. Settings -> Devices & services -> Add integration -> Solar Planner Scheduler.

Or manually: copy `custom_components/solar_planner_scheduler/` into your HA
`config/custom_components/`, restart, then add the integration the same way.

## ⚙️ Configuration

Initial setup asks for the shared entities (forecast, forecast tomorrow, max simultaneous power).
Devices, programs and fixed loads are managed via "Configure":

- **Device**: name plus an optional power sensor, used to detect when a program starts and to
  fine-tune its phases automatically over time. "Manage a device" opens a menu to edit it or
  add/edit/remove its programs.
- **Program**: list its phases (power draw over time), one per line, e.g. `20min@150W` then
  `1.5h@800W` for a washing machine's heat-then-spin cycle. Rough values are fine: with a power
  sensor set, the integration corrects them automatically from real runs. Pick which days it
  should auto-run on. A device can have several programs active at once, each with its own
  `switch.<device>_<program>_active`.
- **Fixed load**: something that draws power but that you don't control (a pool pump, a fridge).
  It's just subtracted from available solar when scheduling everything else.
- **Tariffs** (optional): enable tracking and set price bands (`HH:MM@price`, one per line). Once
  enabled, scheduling always picks the cheapest window instead of just the sunniest one.

Turning a program on searches for today's best slot right away. It repeats on later days only if
you checked those days in its auto-schedule; without any, it turns itself off after that one run.

`datetime.<device>_<program>_start` shows the next start time. Drag its bar on the card, or edit
the entity, to force a time. Click "Auto" to cancel a forced time and search again.

## 🔌 Entities

Per (device, program) pair: `datetime.<device>_<program>_start`,
`binary_sensor.<device>_<program>_should_run`, `switch.<device>_<program>_active`.

`select.solar_planner_scheduler_forecast_source` picks which forecast drives scheduling. With two
or more providers configured, it also offers blended options ("Average", "Min", and "Weighted" for
Solcast + Helios Forecast).

`sensor.solar_planner_scheduler_current_price` exposes the live €/kWh price (with tariff tracking
on), usable as the Energy dashboard's cost source for grid consumption.

The integration never turns a device on or off itself. Pair it with an automation:

1. A device HA controls directly: start it when `should_run` turns on.

```yaml
automation:
  - alias: "Water heater - force heat"
    trigger:
      - platform: state
        entity_id: binary_sensor.water_heater_should_run
        to: "on"
    action:
      - service: switch.turn_on
        target: { entity_id: switch.water_heater_boost }
```

2. A device you start by hand (not controlled by HA): notify 15 minutes before, so there's time to
   load the washing machine and press its own start button.

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

## 📊 The bundled card

Served and registered automatically, no separate install.

```yaml
type: custom:solar-planner-card
devices:                # optional, omit for a forecast-only card with no device rows
  - lave_linge          # or use "*" (a plain string, not a list) to show every device the
  - lave_vaisselle      # integration reports, in its own config order
production_entity: sensor.solar_power     # optional, real production curve on the chart
consumption_entity: sensor.house_power    # optional, real consumption curve on the chart
chart_expanded: true    # optional, default true (the forecast chart/gantt/device rows)
table_expanded: false   # optional, default false (the summary table)
table_show_energy: true # optional, default true (the table's Energy column)
table_show_cost: true   # optional, default true (the table's Cost and/or Savings column, see cost_display)
table_show_total: false # optional, default false (a totals row summing Cost/Savings across all rows)
cost_display: cost      # optional, default "cost": "cost", "savings", or "both" (badge + table column)
chart_hours_past: 6     # optional, default 6 (hours shown before now)
chart_hours_future: 24  # optional, default 24 (hours shown after now)
chart_visible_hours: 30 # optional, default chart_hours_past + chart_hours_future (no scroll)
```

`production_entity`/`consumption_entity` only affect what's drawn on the chart, not scheduling:
set them per card instance if you want those curves.

Each section (chart, table) has its own toggle in the card; `chart_expanded`/`table_expanded` just
set the starting state. `chart_hours_past`/`chart_hours_future` control the visible time window;
`chart_visible_hours` limits how much of it fits on screen before scrolling.
