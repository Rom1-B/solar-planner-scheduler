"""Options flow tests for the program-phases editor and the menu-loop/validation fixes around it.

Needs pytest-homeassistant-custom-component (see requirements-dev.txt); the `hass` and
`enable_custom_integrations` fixtures come from that harness, `hass_config_dir` is overridden in
conftest.py to point at this repo instead of the harness's bundled testing_config.

Each step now applies its change immediately (`async_update_entry`) and loops back to the "init"
menu instead of closing the flow, so tests assert on `entry.options` / `entry.data` rather than on
a final `create_entry` result.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler.config_flow import (
    _base_schema,
    _device_schema,
    _parse_tariff_bands,
    _tariff_schema,
    _TariffParseError,
)
from custom_components.solar_planner_scheduler.const import (
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_FIXED_LOADS,
    CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR,
    CONF_FORECAST_CONFIG_ENTRY_HELIOS,
    CONF_FORECAST_CONFIG_ENTRY_SOLCAST,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_PROVIDERS_ENABLED,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_PHASE_CALIBRATION_RUNS,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_PRICE_TRACKING_ENABLED,
    CONF_PROGRAMS,
    CONF_START_TIME,
    CONF_TARIFF_BANDS,
    DEFAULT_PHASE_CALIBRATION_RUNS,
    DOMAIN,
    FORECAST_PROVIDER_FORECAST_SOLAR,
    FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_SOLCAST,
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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    assert result["step_id"] == "add_program_phases"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W\n45min@1800W"}
    )
    # Loops back to the device's own menu, not the top-level menu.
    assert result["type"] == "menu"
    assert result["step_id"] == "device_detail"

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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W", CONF_AUTO_DAYS: ["mon", "wed", "fri"]}
    )

    assert result["type"] == "menu"
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_AUTO_DAYS] == ["mon", "wed", "fri"]


async def test_add_program_phases_stores_the_calibration_runs_value(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W", CONF_PHASE_CALIBRATION_RUNS: 0}
    )

    assert result["type"] == "menu"
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_PHASE_CALIBRATION_RUNS] == 0


async def test_add_program_phases_defaults_the_calibration_runs_value_when_omitted(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "20min@150W"})

    assert result["type"] == "menu"
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_PHASE_CALIBRATION_RUNS] == DEFAULT_PHASE_CALIBRATION_RUNS


async def test_add_program_phases_accepts_hours(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Conso de base"})
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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": "20 minutes at 150W"})

    assert result["type"] == "form"
    assert result["step_id"] == "add_program_phases"
    assert result["errors"] == {"phases": "invalid_phase_line"}


async def test_add_program_phases_rejects_empty_input(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_program"})
    assert result["step_id"] == "edit_program"

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


async def test_edit_program_phases_prefills_and_replaces_calibration_runs(hass, enable_custom_integrations):
    devices = [
        {
            CONF_NAME: "lave_linge",
            CONF_POWER_SENSOR: "",
            CONF_PROGRAMS: [
                {
                    CONF_NAME: "Eco coton",
                    CONF_POWER_PROFILE: [{"minutes": 20, "power_w": 150.0}],
                    CONF_AUTO_DAYS: [],
                    CONF_PHASE_CALIBRATION_RUNS: 3,
                }
            ],
        }
    ]
    entry = _entry(hass, devices)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})
    # Pre-filled with the program's current value, not the field's own default.
    prefilled = result["data_schema"]({})
    assert prefilled[CONF_PHASE_CALIBRATION_RUNS] == 3

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phases": "20min@150W", CONF_PHASE_CALIBRATION_RUNS: 0}
    )
    assert result["type"] == "menu"

    programs = entry.options[CONF_DEVICES][0][CONF_PROGRAMS]
    assert programs[0][CONF_PHASE_CALIBRATION_RUNS] == 0


async def test_edit_program_aborts_when_no_device_has_a_program(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_program"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_programs"


async def test_add_device_rejects_a_duplicate_name(hass, enable_custom_integrations):
    entry = _entry(hass, [{CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_PROGRAMS: []}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})

    assert result["type"] == "menu"
    assert entry.options[CONF_DEVICES][0][CONF_POWER_SENSOR] == ""


async def test_edit_base_writes_max_power_and_resolves_the_provider_selection(hass, enable_custom_integrations):
    entry = _entry(hass, [])
    MockConfigEntry(domain="solcast_solar").add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_base"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_FORECAST_PROVIDERS_ENABLED: [FORECAST_PROVIDER_SOLCAST], CONF_MAX_SIMULTANEOUS_POWER: 3000},
    )

    assert result["type"] == "menu"
    assert entry.data[CONF_MAX_SIMULTANEOUS_POWER] == 3000
    assert entry.data[CONF_FORECAST_CONFIG_ENTRY_SOLCAST] is not None
    assert CONF_FORECAST_PROVIDERS_ENABLED not in entry.data


async def test_edit_tariff_writes_price_tracking_config_to_entry_data(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_tariff"})
    assert result["step_id"] == "edit_tariff"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PRICE_TRACKING_ENABLED: True,
            "tariff_bands_text": "06:00@0.1892\n22:00@0.1589",
        },
    )

    assert result["type"] == "menu"
    assert entry.data[CONF_PRICE_TRACKING_ENABLED] is True
    assert entry.data[CONF_TARIFF_BANDS] == [
        {"start": "06:00", "price": 0.1892},
        {"start": "22:00", "price": 0.1589},
    ]


async def test_edit_tariff_rejects_an_invalid_band_line(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_tariff"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PRICE_TRACKING_ENABLED: True, "tariff_bands_text": "garbage"},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"tariff_bands_text": "invalid_tariff_line"}
    assert CONF_TARIFF_BANDS not in entry.data


async def test_editing_base_settings_does_not_wipe_previously_configured_tariff_data(hass, enable_custom_integrations):
    """Regression test: async_step_edit_base() used to overwrite entry.data wholesale
    (data=user_input), which would have silently dropped price_tracking_enabled/tariff_bands the
    moment base settings were re-edited after configuring tariffs, since edit_base's own form
    never carries those fields. It must merge instead.
    """
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_tariff"})
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PRICE_TRACKING_ENABLED: True, "tariff_bands_text": "00:00@0.22"},
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_base"})
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_FORECAST_PROVIDERS_ENABLED: [FORECAST_PROVIDER_SOLCAST], CONF_MAX_SIMULTANEOUS_POWER: 5000},
    )

    assert entry.data[CONF_MAX_SIMULTANEOUS_POWER] == 5000
    assert entry.data[CONF_PRICE_TRACKING_ENABLED] is True
    assert entry.data[CONF_TARIFF_BANDS] == [{"start": "00:00", "price": 0.22}]


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
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "lave_linge"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_program"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"program_name": "Eco coton"})

    assert result["type"] == "menu"
    device = entry.options[CONF_DEVICES][0]
    assert device[CONF_PROGRAMS] == []


async def test_add_fixed_load_parses_a_schedule_with_an_implicit_midnight_close(hass, enable_custom_integrations):
    """`07:00@100W` alone means 100W from 07:00 to midnight (no explicit end marker needed)."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})
    assert result["step_id"] == "add_fixed_load_schedule"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"schedule": "07:00@100W"})
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "07:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 1020, "power_w": 100.0}]


async def test_add_fixed_load_schedule_with_an_explicit_end_marker(hass, enable_custom_integrations):
    """`07:00@100W` / `22:00@0W` means 100W from 07:00 to 22:00, not until midnight."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"schedule": "07:00@100W\n22:00@0W"}
    )
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_START_TIME] == "07:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 900, "power_w": 100.0}]


async def test_add_fixed_load_schedule_with_a_zero_watt_gap(hass, enable_custom_integrations):
    """A 0W breakpoint that isn't last just models a gap, not an end marker."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"schedule": "07:00@100W\n12:00@0W\n18:00@200W"}
    )
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_START_TIME] == "07:00"
    assert load[CONF_POWER_PROFILE] == [
        {"minutes": 300, "power_w": 100.0},
        {"minutes": 360, "power_w": 0.0},
        {"minutes": 360, "power_w": 200.0},
    ]


async def test_add_fixed_load_schedule_rejects_a_first_line_at_zero_power(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"schedule": "07:00@0W"})
    assert result["errors"] == {"schedule": "fixed_load_must_start_with_power"}


async def test_add_fixed_load_schedule_rejects_duplicate_times(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"schedule": "07:00@100W\n07:00@200W"}
    )
    assert result["errors"] == {"schedule": "duplicate_fixed_load_time"}


async def test_edit_fixed_load_prefills_and_replaces_schedule_in_place(hass, enable_custom_integrations):
    fixed_loads = [
        {
            CONF_NAME: "PAC",
            CONF_START_TIME: "13:00",
            CONF_POWER_PROFILE: [{"minutes": 60, "power_w": 1500.0}],
        }
    ]
    entry = _entry(hass, [], fixed_loads=fixed_loads)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_fixed_load"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})
    assert result["step_id"] == "edit_fixed_load_schedule"
    assert result["data_schema"]({})["schedule"] == "13:00@1500W\n14:00@0W"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"schedule": "08:00@300W"})
    assert result["type"] == "menu"

    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "08:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 960, "power_w": 300.0}]


async def test_edit_fixed_load_aborts_when_none_exist(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "fixed_loads_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_fixed_load"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_fixed_loads"


async def test_edit_device_prefills_and_replaces_power_sensor_in_place(hass, enable_custom_integrations):
    hass.states.async_set("sensor.old_power", "0")
    hass.states.async_set("sensor.new_power", "0")
    devices = [{CONF_NAME: "PAC", CONF_POWER_SENSOR: "sensor.old_power", CONF_PROGRAMS: []}]
    entry = _entry(hass, devices)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_NAME: "PAC"})
    assert result["step_id"] == "device_detail"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit_device_power_sensor"})
    assert result["step_id"] == "edit_device_power_sensor"
    key = next(k for k in result["data_schema"].schema if str(k) == CONF_POWER_SENSOR)
    assert key.description["suggested_value"] == "sensor.old_power"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_POWER_SENSOR: "sensor.new_power"})
    assert result["type"] == "menu"

    device = entry.options[CONF_DEVICES][0]
    assert device[CONF_NAME] == "PAC"
    assert device[CONF_POWER_SENSOR] == "sensor.new_power"


async def test_manage_device_aborts_when_none_exist(hass, enable_custom_integrations):
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "devices_menu"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manage_device"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_devices"


# --- entity picker filters ---------------------------------------------------------------------


def _selector_for(schema, field_name):
    for key, validator in schema.schema.items():
        if str(key) == field_name:
            return validator
    raise KeyError(field_name)


def test_forecast_providers_enabled_field_is_a_multi_select_of_fixed_provider_keys():
    """A single multi-select, not a config_entry/entity picker per provider: the user just ticks
    which of Solcast/Helios Forecast/Forecast.Solar to use — resolve_forecast_sources() looks up
    "the" config entry for each ticked provider's real HA domain itself, since there is
    realistically only ever one per domain.
    """
    schema = _base_schema()
    field = _selector_for(schema, CONF_FORECAST_PROVIDERS_ENABLED)
    assert field.config["multiple"] is True
    assert {opt["value"] for opt in field.config["options"]} == {
        FORECAST_PROVIDER_SOLCAST,
        FORECAST_PROVIDER_HELIOS,
        FORECAST_PROVIDER_FORECAST_SOLAR,
    }


# --- forecast provider fields --------------------------------------------------------------------


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_setup_with_both_provider_fields_stores_both(hass, enable_custom_integrations):
    MockConfigEntry(domain="solcast_solar").add_to_hass(hass)
    MockConfigEntry(domain="helios_forecast").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_FORECAST_PROVIDERS_ENABLED: [FORECAST_PROVIDER_SOLCAST, FORECAST_PROVIDER_HELIOS],
            CONF_MAX_SIMULTANEOUS_POWER: 4000,
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_FORECAST_CONFIG_ENTRY_SOLCAST] is not None
    assert result["data"][CONF_FORECAST_CONFIG_ENTRY_HELIOS] is not None
    assert CONF_FORECAST_PROVIDERS_ENABLED not in result["data"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_setup_with_only_forecast_solar_enabled_stores_it_alone(hass, enable_custom_integrations):
    MockConfigEntry(domain="forecast_solar").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_FORECAST_PROVIDERS_ENABLED: [FORECAST_PROVIDER_FORECAST_SOLAR], CONF_MAX_SIMULTANEOUS_POWER: 4000},
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR] is not None
    assert result["data"][CONF_FORECAST_CONFIG_ENTRY_SOLCAST] is None
    assert result["data"][CONF_FORECAST_CONFIG_ENTRY_HELIOS] is None


def test_device_power_sensor_picker_filters_to_power_sensors():
    selector = _selector_for(_device_schema(), CONF_POWER_SENSOR)
    assert selector.config["domain"] == ["sensor"]
    assert selector.config["device_class"] == ["power"]


# --- tariff bands parser ------------------------------------------------------------------------


def test_parse_tariff_bands_handles_midnight_wraparound():
    bands = _parse_tariff_bands("22:00@0.1589\n06:00@0.1892")
    # Sorted by start time; the caller (price_at) is what handles the actual wraparound lookup.
    assert bands == [{"start": "06:00", "price": 0.1892}, {"start": "22:00", "price": 0.1589}]


def test_parse_tariff_bands_rejects_invalid_line():
    with pytest.raises(_TariffParseError) as exc_info:
        _parse_tariff_bands("not a band")
    assert exc_info.value.error_key == "invalid_tariff_line"


def test_parse_tariff_bands_rejects_empty_text():
    with pytest.raises(_TariffParseError) as exc_info:
        _parse_tariff_bands("   \n  ")
    assert exc_info.value.error_key == "empty_tariff_bands"


def test_price_tracking_defaults_to_disabled():
    schema = _tariff_schema()
    for key in schema.schema:
        if str(key) == CONF_PRICE_TRACKING_ENABLED:
            assert key.default() is False
            return
    raise AssertionError(f"{CONF_PRICE_TRACKING_ENABLED} not found in schema")
