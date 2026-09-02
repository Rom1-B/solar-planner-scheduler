"""Options flow tests for the program-phases editor and the menu-loop/validation fixes around it.

Needs pytest-homeassistant-custom-component (see requirements-dev.txt); the `hass` and
`enable_custom_integrations` fixtures come from that harness, `hass_config_dir` is overridden in
conftest.py to point at this repo instead of the harness's bundled testing_config.

Each step now applies its change immediately (`async_update_entry`) and loops back to the "init"
menu instead of closing the flow, so tests assert on `entry.options` / `entry.data` rather than on
a final `create_entry` result.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.config_flow import _base_schema, _device_schema
from custom_components.solar_planner_scheduler.const import (
    CONF_AUTO_DAYS,
    CONF_CONSUMPTION_ENTITY,
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_PRODUCTION_ENTITY,
    CONF_PROGRAMS,
    CONF_START_TIME,
    DOMAIN,
)

BASE_DATA = {
    CONF_FORECAST_ENTITY: "sensor.forecast",
    CONF_MAX_SIMULTANEOUS_POWER: 4000,
}


def _entry(hass, devices, fixed_loads=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data=BASE_DATA,
        options={CONF_DEVICES: devices, CONF_FIXED_LOADS: fixed_loads or []},
    )
    entry.add_to_hass(hass)
    return entry


async def test_add_program_phases_parses_valid_multiline_text(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", "program_name": "Eco coton"}
    )
    assert result["step_id"] == "add_program_phases"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W\n45min@1800W"}
    )
    # Loops back to the menu instead of closing the flow.
    assert result["type"] == "menu"
    assert result["step_id"] == "init"

    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_NAME] == "Eco coton"
    assert program[CONF_POWER_PROFILE] == [
        {"minutes": 20, "power_w": 150.0},
        {"minutes": 45, "power_w": 1800.0},
    ]
    # Nothing checked by default: a new program doesn't inherit "every day" for free.
    assert program[CONF_AUTO_DAYS] == []


async def test_add_program_phases_stores_the_selected_auto_days(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", "program_name": "Eco coton"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W", CONF_AUTO_DAYS: ["mon", "wed", "fri"]}
    )

    assert result["type"] == "menu"
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_AUTO_DAYS] == ["mon", "wed", "fri"]


async def test_add_program_phases_accepts_hours(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", "program_name": "Conso de base"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "24h@110W\n1.5h@800W"})

    assert result["type"] == "menu"
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_POWER_PROFILE] == [
        {"minutes": 1440, "power_w": 110.0},
        {"minutes": 90, "power_w": 800.0},
    ]


async def test_add_program_phases_rejects_a_malformed_line(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", "program_name": "Eco coton"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "20 minutes at 150W"})

    assert result["type"] == "form"
    assert result["step_id"] == "add_program_phases"
    assert result["errors"] == {"phases": "invalid_phase_line"}


async def test_add_program_phases_rejects_empty_input(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", "program_name": "Eco coton"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "   \n  "})

    assert result["type"] == "form"
    assert result["errors"] == {"phases": "empty_phases"}


async def test_edit_program_prefills_and_replaces_phases_in_place(hass, enable_custom_integrations):
    devices = [
        {
            CONF_NAME: "lave_linge",
            CONF_POWER_SENSOR: "",
            CONF_PROGRAMS: [
                {
                    CONF_NAME: "Eco coton",
                    CONF_POWER_PROFILE: [{"minutes": 20, "power_w": 150.0}, {"minutes": 45, "power_w": 1800.0}],
                    CONF_AUTO_DAYS: ["tue", "thu"],
                }
            ],
        }
    ]
    entry = _entry(hass, devices)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    assert result["step_id"] == "edit_program_pick"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    assert result["step_id"] == "edit_program_phases"
    # The field is pre-filled with the program's current phases and auto_days, not left blank.
    prefilled = result["data_schema"]({})
    assert prefilled["phases"] == "20min@150W\n45min@1800W"
    assert prefilled[CONF_AUTO_DAYS] == ["tue", "thu"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "10min@300W", CONF_AUTO_DAYS: ["sat", "sun"]}
    )
    assert result["type"] == "menu"

    programs = entry.options[CONF_DEVICES][0][CONF_PROGRAMS]
    assert len(programs) == 1
    assert programs[0][CONF_NAME] == "Eco coton"
    assert programs[0][CONF_POWER_PROFILE] == [{"minutes": 10, "power_w": 300.0}]
    assert programs[0][CONF_AUTO_DAYS] == ["sat", "sun"]


async def test_edit_program_aborts_when_no_device_has_a_program(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_program"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_programs"


async def test_add_device_rejects_a_duplicate_name(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})

    assert result["type"] == "form"
    assert result["step_id"] == "add_device"
    assert result["errors"] == {CONF_NAME: "duplicate_device"}
    assert len(entry.options[CONF_DEVICES]) == 1


async def test_add_device_without_a_power_sensor_does_not_crash(hass, enable_custom_integrations):
    """Regression test: an omitted optional EntitySelector used to fail its own validation."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})

    assert result["type"] == "menu"
    assert entry.options[CONF_DEVICES][0][CONF_POWER_SENSOR] == ""


async def test_edit_base_accepts_omitted_optional_entities(hass, enable_custom_integrations):
    """Regression test: same default="" bug as add_device, in the base-settings form."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_base"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_FORECAST_ENTITY: "sensor.forecast", CONF_MAX_SIMULTANEOUS_POWER: 3000},
    )

    assert result["type"] == "menu"
    assert entry.data[CONF_MAX_SIMULTANEOUS_POWER] == 3000
    assert entry.data.get("production_entity") is None


async def test_remove_program_removes_it_from_the_devices_only_program_list(hass, enable_custom_integrations):
    """The current selection itself lives in the coordinator's own store, not in these options
    (see test_coordinator.py's test_forget_program_resets_the_selection_only_if_it_matches for the
    store-reset behavior) — this test only covers the options-side removal.
    """
    devices = [
        {
            CONF_NAME: "lave_linge",
            CONF_POWER_SENSOR: "",
            CONF_PROGRAMS: [{CONF_NAME: "Eco coton", CONF_POWER_PROFILE: [{"minutes": 20, "power_w": 150.0}]}],
        }
    ]
    entry = _entry(hass, devices)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})

    assert result["type"] == "menu"
    device = entry.options[CONF_DEVICES][0]
    assert device[CONF_PROGRAMS] == []


async def test_add_fixed_load_parses_a_multi_phase_profile(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "PAC", CONF_START_TIME: "13:00:00"}
    )
    assert result["step_id"] == "add_fixed_load_phases"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "60min@1500W\n30min@800W"}
    )
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "13:00:00"
    assert load[CONF_POWER_PROFILE] == [
        {"minutes": 60, "power_w": 1500.0},
        {"minutes": 30, "power_w": 800.0},
    ]


async def test_edit_fixed_load_prefills_and_replaces_phases_in_place(hass, enable_custom_integrations):
    fixed_loads = [
        {
            CONF_NAME: "PAC",
            CONF_START_TIME: "13:00:00",
            CONF_POWER_PROFILE: [{"minutes": 60, "power_w": 1500.0}],
        }
    ]
    entry = _entry(hass, [], fixed_loads=fixed_loads)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})
    assert result["step_id"] == "edit_fixed_load_phases"
    assert result["data_schema"]({})["phases"] == "60min@1500W"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "10min@300W"})
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "13:00:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 10, "power_w": 300.0}]


async def test_edit_fixed_load_aborts_when_none_exist(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_fixed_load"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_fixed_loads"


# --- entity picker filters ---------------------------------------------------------------------


def _selector_for(schema, field_name):
    for key, validator in schema.schema.items():
        if str(key) == field_name:
            return validator
    raise KeyError(field_name)


def test_forecast_entity_pickers_filter_to_energy_sensors():
    """Regression test for the device_class filter added to the entity pickers — Solcast's
    forecast sensors report device_class "energy" (confirmed against a real instance, see
    CLAUDE.local.md), not "power": filtering to the wrong class would silently empty the picker.
    """
    schema = _base_schema()
    for field in (CONF_FORECAST_ENTITY, CONF_FORECAST_TOMORROW_ENTITY):
        selector = _selector_for(schema, field)
        assert selector.config["domain"] == ["sensor"]
        assert selector.config["device_class"] == ["energy"]


def test_production_and_consumption_pickers_filter_to_power_sensors():
    schema = _base_schema()
    for field in (CONF_PRODUCTION_ENTITY, CONF_CONSUMPTION_ENTITY):
        selector = _selector_for(schema, field)
        assert selector.config["domain"] == ["sensor"]
        assert selector.config["device_class"] == ["power"]


def test_device_power_sensor_picker_filters_to_power_sensors():
    selector = _selector_for(_device_schema(), CONF_POWER_SENSOR)
    assert selector.config["domain"] == ["sensor"]
    assert selector.config["device_class"] == ["power"]
