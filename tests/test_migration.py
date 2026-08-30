"""async_migrate_entry tests: v1 devices -> v2 programs, v2 fixed loads -> v3 power_profile."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import async_migrate_entry
from custom_components.solar_planner_scheduler.const import (
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    CONF_SURPLUS_ENTITY,
    DOMAIN,
)

BASE_DATA = {
    CONF_FORECAST_ENTITY: "sensor.forecast",
    CONF_SURPLUS_ENTITY: "sensor.surplus",
    CONF_MAX_SIMULTANEOUS_POWER: 4000,
}


async def test_migrate_v2_fixed_load_to_v3_wraps_flat_load_in_a_single_phase_profile(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [],
            CONF_FIXED_LOADS: [
                {CONF_NAME: "PAC", CONF_START_TIME: "13:00:00", CONF_POWER_W: 1500.0, CONF_DURATION_MIN: 60}
            ],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 3
    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "13:00:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 60, "power_w": 1500.0}]


async def test_migrate_v1_to_v3_chains_both_steps_in_one_call(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {CONF_NAME: "lave_linge", CONF_POWER_SENSOR: "", CONF_POWER_W: 2000.0, CONF_DURATION_MIN: 90}
            ],
            CONF_FIXED_LOADS: [
                {CONF_NAME: "PAC", CONF_START_TIME: "13:00:00", CONF_POWER_W: 1500.0, CONF_DURATION_MIN: 60}
            ],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 3
    device = entry.options[CONF_DEVICES][0]
    assert device[CONF_SELECTED_PROGRAM] == "lave_linge"
    assert device[CONF_PROGRAMS][0][CONF_POWER_W] == 2000.0
    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_POWER_PROFILE] == [{"minutes": 60, "power_w": 1500.0}]


async def test_migrate_is_a_no_op_for_an_already_v3_fixed_load(hass):
    fixed_load = {CONF_NAME: "PAC", CONF_START_TIME: "13:00:00", CONF_POWER_PROFILE: [{"minutes": 60, "power_w": 1500.0}]}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data=BASE_DATA,
        options={CONF_DEVICES: [], CONF_FIXED_LOADS: [fixed_load]},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.options[CONF_FIXED_LOADS][0] == fixed_load
