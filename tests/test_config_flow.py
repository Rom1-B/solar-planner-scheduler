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

from custom_components.solar_planner_scheduler.const import (
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_SURPLUS_ENTITY,
    DOMAIN,
    NONE_PROGRAM,
)

BASE_DATA = {
    CONF_FORECAST_ENTITY: "sensor.forecast",
    CONF_SURPLUS_ENTITY: "sensor.surplus",
    CONF_MAX_SIMULTANEOUS_POWER: 4000,
}


def _entry(hass, devices):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=BASE_DATA,
        options={CONF_DEVICES: devices, CONF_FIXED_LOADS: []},
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
    # The field is pre-filled with the program's current phases, not left blank.
    assert result["data_schema"]({})["phases"] == "20min@150W\n45min@1800W"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "10min@300W"})
    assert result["type"] == "menu"

    programs = entry.options[CONF_DEVICES][0][CONF_PROGRAMS]
    assert len(programs) == 1
    assert programs[0][CONF_NAME] == "Eco coton"
    assert programs[0][CONF_POWER_PROFILE] == [{"minutes": 10, "power_w": 300.0}]


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
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "sensor.dummy"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "add_device"
    assert result["errors"] == {CONF_NAME: "duplicate_device"}
    assert len(entry.options[CONF_DEVICES]) == 1


async def test_remove_program_allows_removing_a_devices_only_program_and_resets_selection(
    hass, enable_custom_integrations
):
    devices = [
        {
            CONF_NAME: "lave_linge",
            CONF_POWER_SENSOR: "",
            CONF_SELECTED_PROGRAM: "Eco coton",
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
    assert device[CONF_SELECTED_PROGRAM] == NONE_PROGRAM
