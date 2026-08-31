"""async_migrate_entry tests: v1 devices -> v2 programs, v2 fixed loads -> v3 power_profile,
v3 programs -> v4 auto_days (backfilled to every day for pre-existing programs), v4 programs/
selection/manual overrides -> v5 (selection moved to the coordinator's own store, power_profile
made mandatory, obsolete option keys stripped)."""

from __future__ import annotations

from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import async_migrate_entry
from custom_components.solar_planner_scheduler.const import (
    CONF_ACCEPTED_DATE,
    CONF_ACCEPTED_DAY,
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_FORECAST_ENTITY,
    CONF_MANUAL,
    CONF_MANUAL_START,
    CONF_MAX_SIMULTANEOUS_POWER,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    DOMAIN,
    WEEKDAYS,
)
from custom_components.solar_planner_scheduler.coordinator import STORAGE_VERSION

BASE_DATA = {
    CONF_FORECAST_ENTITY: "sensor.forecast",
    CONF_MAX_SIMULTANEOUS_POWER: 4000,
}


async def _load_store(hass, entry) -> dict:
    return await Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}").async_load() or {}


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

    assert entry.version == 5
    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_NAME] == "PAC"
    assert load[CONF_START_TIME] == "13:00:00"
    assert load[CONF_POWER_PROFILE] == [{"minutes": 60, "power_w": 1500.0}]


async def test_migrate_v1_to_v5_chains_every_step_in_one_call(hass):
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

    assert entry.version == 5
    device = entry.options[CONF_DEVICES][0]
    assert CONF_SELECTED_PROGRAM not in device  # moved to the coordinator's own store
    program = device[CONF_PROGRAMS][0]
    assert program[CONF_POWER_PROFILE] == [{"minutes": 90, "power_w": 2000.0}]  # v1's flat shape -> a single phase
    assert program[CONF_AUTO_DAYS] == WEEKDAYS  # backfilled: preserve "runs every day" on deploy
    load = entry.options[CONF_FIXED_LOADS][0]
    assert load[CONF_POWER_PROFILE] == [{"minutes": 60, "power_w": 1500.0}]

    state = await _load_store(hass, entry)
    assert state["lave_linge"]["selected"] == "lave_linge"  # v1->v2 names the program after the device


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
    assert entry.version == 5
    assert entry.options[CONF_FIXED_LOADS][0] == fixed_load


async def test_migrate_v3_backfills_auto_days_for_a_pre_existing_program(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "ballon_d_eau_chaude",
                    CONF_POWER_SENSOR: "",
                    CONF_SELECTED_PROGRAM: "Eau chaude",
                    CONF_PROGRAMS: [
                        {CONF_NAME: "Eau chaude", CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 1600.0}]}
                    ],
                }
            ],
            CONF_FIXED_LOADS: [],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 5
    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_AUTO_DAYS] == WEEKDAYS


async def test_migrate_v3_does_not_override_an_already_set_auto_days(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_POWER_SENSOR: "",
                    CONF_SELECTED_PROGRAM: "Eco coton",
                    CONF_PROGRAMS: [
                        {
                            CONF_NAME: "Eco coton",
                            CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 150.0}],
                            CONF_AUTO_DAYS: ["mon"],
                        }
                    ],
                }
            ],
            CONF_FIXED_LOADS: [],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_AUTO_DAYS] == ["mon"]


async def test_migrate_v4_carries_the_current_selection_into_the_store(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_vaisselle",
                    CONF_POWER_SENSOR: "",
                    CONF_SELECTED_PROGRAM: "Eco",
                    CONF_PROGRAMS: [
                        {CONF_NAME: "Eco", CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 100.0}], CONF_AUTO_DAYS: []}
                    ],
                }
            ],
            CONF_FIXED_LOADS: [],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 5
    assert CONF_SELECTED_PROGRAM not in entry.options[CONF_DEVICES][0]
    state = await _load_store(hass, entry)
    assert state["lave_vaisselle"]["selected"] == "Eco"


async def test_migrate_v4_wraps_a_flat_program_into_a_single_phase_profile(hass):
    """A program can still be missing power_profile entirely on a very old migrated config (the
    config UI has only ever produced power_profile programs since the phases editor shipped)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "lave_linge",
                    CONF_POWER_SENSOR: "",
                    CONF_PROGRAMS: [
                        {CONF_NAME: "Coton", CONF_POWER_W: 2000.0, CONF_DURATION_MIN: 90, CONF_AUTO_DAYS: WEEKDAYS}
                    ],
                }
            ],
            CONF_FIXED_LOADS: [],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    program = entry.options[CONF_DEVICES][0][CONF_PROGRAMS][0]
    assert program[CONF_POWER_PROFILE] == [{"minutes": 90, "power_w": 2000.0}]


async def test_migrate_v4_strips_manual_mode_and_accepted_day_without_carrying_them_over(hass):
    """Deliberate: an active manual override is dropped, not migrated into the new "forced start"
    store — see CLAUDE.local.md for why. The device just reverts to automatic scheduling."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data=BASE_DATA,
        options={
            CONF_DEVICES: [
                {
                    CONF_NAME: "ballon_d_eau_chaude",
                    CONF_POWER_SENSOR: "",
                    CONF_SELECTED_PROGRAM: "Eau chaude",
                    CONF_MANUAL: True,
                    CONF_MANUAL_START: "2026-08-30T13:00:00+00:00",
                    CONF_ACCEPTED_DAY: "tomorrow",
                    CONF_ACCEPTED_DATE: "2026-08-30",
                    CONF_PROGRAMS: [
                        {CONF_NAME: "Eau chaude", CONF_POWER_PROFILE: [{"minutes": 30, "power_w": 1600.0}], CONF_AUTO_DAYS: []}
                    ],
                }
            ],
            CONF_FIXED_LOADS: [],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    device = entry.options[CONF_DEVICES][0]
    assert CONF_MANUAL not in device
    assert CONF_MANUAL_START not in device
    assert CONF_ACCEPTED_DAY not in device
    assert CONF_ACCEPTED_DATE not in device
    state = await _load_store(hass, entry)
    assert state["ballon_d_eau_chaude"]["selected"] == "Eau chaude"  # the selection is still carried over
    assert "committed" not in state["ballon_d_eau_chaude"]  # the manual override itself is not
