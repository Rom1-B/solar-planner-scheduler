"""The Solar Planner Scheduler integration.

Homeassistant imports are deferred into function bodies (not at module level) so that this
package's own `__init__.py` never pulls in `homeassistant` just by being imported — that keeps
`scheduling.py` (zero dependencies, mirroring the JS reference) importable and testable with plain
pytest, no Home Assistant test harness required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_ACCEPTED_DATE,
    CONF_ACCEPTED_DAY,
    CONF_AUTO_DAYS,
    CONF_DEVICES,
    CONF_DURATION_MIN,
    CONF_FIXED_LOADS,
    CONF_MANUAL,
    CONF_MANUAL_START,
    CONF_MINUTES,
    CONF_NAME,
    CONF_POWER_PROFILE,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PROGRAMS,
    CONF_SELECTED_PROGRAM,
    CONF_START_TIME,
    DOMAIN,
    NONE_PROGRAM,
    WEEKDAYS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ["sensor", "binary_sensor", "select", "datetime"]

CARD_URL_BASE = f"/{DOMAIN}_files"
CARD_FILENAME = "solar-planner-card.js"
# Bump manually whenever solar-planner-card.js changes, so the Lovelace resource URL's
# cache-busting query string actually changes and browsers don't keep serving a stale copy.
CARD_VERSION = "8"


async def async_setup(hass: "HomeAssistant", config: dict) -> bool:
    """Serve the bundled Lovelace card and register it in Lovelace's own resource storage.

    `add_extra_js_url` alone does not make the frontend load a module — Lovelace (storage
    mode) only loads modules it finds in its own `resources` storage collection, so this
    writes an entry there directly. Home Assistant only calls this once per domain
    regardless of how many config entries exist, so no "already registered" guard is
    needed for the static path itself.
    """
    from pathlib import Path

    from homeassistant.components.http import StaticPathConfig
    from homeassistant.helpers.event import async_call_later

    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL_BASE, str(www_path), False)])

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return True

    async def _register_resource(_now=None) -> None:
        if not lovelace.resources.loaded:
            async_call_later(hass, 5, _register_resource)
            return
        url = f"{CARD_URL_BASE}/{CARD_FILENAME}"
        existing = next((r for r in lovelace.resources.async_items() if r["url"].split("?")[0] == url), None)
        if existing is None:
            await lovelace.resources.async_create_item({"res_type": "module", "url": f"{url}?v={CARD_VERSION}"})
        elif existing["url"].split("?v=")[-1] != CARD_VERSION:
            await lovelace.resources.async_update_item(existing["id"], {"res_type": "module", "url": f"{url}?v={CARD_VERSION}"})

    await _register_resource()
    _async_register_services(hass)
    return True


def _async_register_services(hass: "HomeAssistant") -> None:
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_registry as er

    schema = vol.Schema({vol.Required("entity_id"): cv.entity_id})

    async def _reset_to_auto(call) -> None:
        entity_id = call.data["entity_id"]
        registry_entry = er.async_get(hass).async_get(entity_id)
        if registry_entry is None or registry_entry.config_entry_id is None:
            return
        entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
        if entry is None:
            return
        device_name = registry_entry.unique_id.removeprefix(f"{entry.entry_id}_").removesuffix("_start")
        await hass.data[DOMAIN][entry.entry_id].async_clear_forced_start(device_name)

    hass.services.async_register(DOMAIN, "reset_to_auto", _reset_to_auto, schema=schema)


async def async_migrate_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """v1->v2: flat devices (power_w/duration_min at the device root) become v2's programs[].

    v2->v3: flat fixed loads (power_w/duration_min at the load root) become v3's power_profile,
    a single-phase list — same shape programs already use, so fixed loads can have several phases
    too via the same "phases" editor in the options flow.

    v3->v4: programs gain auto_days (which weekdays the coordinator is allowed to auto-schedule
    them on). New programs created from now on default to none selected (the user must opt in
    explicitly), but that would silently stop scheduling every *existing* program the moment this
    migration runs — so pre-existing programs are backfilled to all 7 days, preserving their
    current "runs every day" behavior instead of going dark on deploy.

    v4->v5: the mechanic rationalization — CONF_SELECTED_PROGRAM, CONF_MANUAL, CONF_MANUAL_START,
    CONF_ACCEPTED_DAY, CONF_ACCEPTED_DATE move out of config entry options entirely, into the
    coordinator's own internal store (see coordinator.py), so that picking a program or forcing a
    start time never triggers a full entry reload again. The current program *selection* is
    carried over into that store (losing it would silently stop scheduling every device on
    upgrade); an active manual override (CONF_MANUAL on, with a CONF_MANUAL_START) is not carried
    over and is simply dropped — accepted deliberately, see CLAUDE.local.md. Every program without
    a power_profile yet (the flat power_w/duration_min shape, unreachable from the config UI since
    the phases editor shipped, but still possible on a very old migrated config) becomes a
    single-phase profile, so coordinator.py can assume power_profile is always present.

    All blocks use `if`, not `elif`, so an entry sitting at v1 falls through all of them in one call.
    """
    if entry.version == 1:
        migrated_devices = []
        for device in entry.options.get(CONF_DEVICES, []):
            if CONF_PROGRAMS in device:
                migrated_devices.append(device)
                continue
            name = device[CONF_NAME]
            program = {
                CONF_NAME: name,
                CONF_POWER_W: device[CONF_POWER_W],
                CONF_DURATION_MIN: device[CONF_DURATION_MIN],
            }
            migrated_devices.append(
                {
                    CONF_NAME: name,
                    CONF_POWER_SENSOR: device.get(CONF_POWER_SENSOR, ""),
                    CONF_PROGRAMS: [program],
                    CONF_SELECTED_PROGRAM: name,
                }
            )
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DEVICES: migrated_devices}, version=2
        )

    if entry.version == 2:
        migrated_fixed_loads = []
        for load in entry.options.get(CONF_FIXED_LOADS, []):
            if CONF_POWER_PROFILE in load:
                migrated_fixed_loads.append(load)
                continue
            migrated_fixed_loads.append(
                {
                    CONF_NAME: load[CONF_NAME],
                    CONF_START_TIME: load[CONF_START_TIME],
                    CONF_POWER_PROFILE: [{CONF_MINUTES: load[CONF_DURATION_MIN], CONF_POWER_W: load[CONF_POWER_W]}],
                }
            )
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_FIXED_LOADS: migrated_fixed_loads}, version=3
        )

    if entry.version == 3:
        migrated_devices = [
            {
                **device,
                CONF_PROGRAMS: [
                    {**program, CONF_AUTO_DAYS: WEEKDAYS} if CONF_AUTO_DAYS not in program else program
                    for program in device.get(CONF_PROGRAMS, [])
                ],
            }
            for device in entry.options.get(CONF_DEVICES, [])
        ]
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DEVICES: migrated_devices}, version=4
        )

    if entry.version == 4:
        from homeassistant.helpers.storage import Store

        from .coordinator import STORAGE_VERSION

        store_updates: dict[str, dict] = {}
        migrated_devices = []
        for device in entry.options.get(CONF_DEVICES, []):
            selected = device.get(CONF_SELECTED_PROGRAM, NONE_PROGRAM)
            if selected != NONE_PROGRAM:
                store_updates[device[CONF_NAME]] = {"selected": selected}
            programs = [
                {**p, CONF_POWER_PROFILE: [{CONF_MINUTES: p[CONF_DURATION_MIN], CONF_POWER_W: p[CONF_POWER_W]}]}
                if CONF_POWER_PROFILE not in p
                else p
                for p in device.get(CONF_PROGRAMS, [])
            ]
            migrated_devices.append(
                {
                    key: value
                    for key, value in {**device, CONF_PROGRAMS: programs}.items()
                    if key not in (CONF_SELECTED_PROGRAM, CONF_MANUAL, CONF_MANUAL_START, CONF_ACCEPTED_DAY, CONF_ACCEPTED_DATE)
                }
            )

        if store_updates:
            store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
            existing_state = await store.async_load() or {}
            for name, patch in store_updates.items():
                existing_state[name] = {**existing_state.get(name, {}), **patch}
            await store.async_save(existing_state)

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DEVICES: migrated_devices}, version=5
        )

    return True


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    from datetime import timedelta

    from homeassistant.helpers.event import async_track_time_interval

    from .coordinator import SolarPlannerSchedulerCoordinator

    coordinator = SolarPlannerSchedulerCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Pushes should_run/locked to their current value every minute without re-running the search —
    # both are pure functions of "now" against the already-known committed start/end, so this only
    # needs to prompt entities to re-read them, not recompute anything. The 15-minute coordinator
    # poll stays the cadence for the actual search/decision.
    entry.async_on_unload(
        async_track_time_interval(hass, lambda _now: coordinator.async_update_listeners(), timedelta(minutes=1))
    )
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    await hass.config_entries.async_reload(entry.entry_id)
