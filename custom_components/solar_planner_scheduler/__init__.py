"""The Solar Planner Scheduler integration.

Every other homeassistant import here is deferred into function bodies (not at module level) —
`CONFIG_SCHEMA` below is the one deliberate exception, required at module scope so hassfest can see
it (integrations that define `async_setup`/`setup` must declare one of CONFIG_SCHEMA/
PLATFORM_SCHEMA/PLATFORM_SCHEMA_BASE, checked 2026-09-02). This one `homeassistant.helpers.
config_validation` import means this package's `__init__.py` now requires `homeassistant` to be
installed to import at all (so `from custom_components.solar_planner_scheduler.scheduling import
...` would too, despite scheduling.py itself having zero dependencies) — accepted as a real
constraint imposed by hassfest, not worth working around with a lazier trick, since this
integration only ever runs inside real Home Assistant or its own test harness (which always has
`homeassistant` installed) anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = ["sensor", "binary_sensor", "switch", "datetime"]

CARD_URL_BASE = f"/{DOMAIN}_files"
CARD_FILENAME = "solar-planner-card.js"
# Bump manually whenever solar-planner-card.js changes, so the Lovelace resource URL's
# cache-busting query string actually changes and browsers don't keep serving a stale copy.
CARD_VERSION = "11"


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
        from .const import CONF_DEVICES, CONF_NAME, CONF_PROGRAMS

        entity_id = call.data["entity_id"]
        registry_entry = er.async_get(hass).async_get(entity_id)
        if registry_entry is None or registry_entry.config_entry_id is None:
            return
        entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
        if entry is None:
            return
        # Resolve (device, program) by reconstructing each candidate's unique_id and matching it,
        # rather than splitting the string apart — device/program names are free text and may
        # themselves contain "_", which would make a split ambiguous.
        for device in entry.options.get(CONF_DEVICES, []):
            device_name = device[CONF_NAME]
            for program in device.get(CONF_PROGRAMS, []):
                program_name = program[CONF_NAME]
                if registry_entry.unique_id == f"{entry.entry_id}_{device_name}_{program_name}_start":
                    await hass.data[DOMAIN][entry.entry_id].async_clear_forced_start(device_name, program_name)
                    return

    hass.services.async_register(DOMAIN, "reset_to_auto", _reset_to_auto, schema=schema)


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    from datetime import datetime, timedelta

    from homeassistant.core import callback
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
    #
    # Must be @callback: an undecorated lambda made HA's thread-safety frame-check misidentify
    # this as running off the event loop, raising on every tick inside async_update_listeners()
    # (caught and logged there, so entities silently never got the update — should_run stayed
    # stale for up to several minutes past its actual start/end, observed live 2026-09-02).
    @callback
    def _refresh_listeners(_now: datetime) -> None:
        coordinator.async_update_listeners()

    entry.async_on_unload(async_track_time_interval(hass, _refresh_listeners, timedelta(minutes=1)))
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    await hass.config_entries.async_reload(entry.entry_id)
