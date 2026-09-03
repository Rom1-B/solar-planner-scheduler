"""The Solar Planner Scheduler integration.

Every other homeassistant import here is deferred into function bodies; `CONFIG_SCHEMA` is the one
exception, required at module scope by hassfest for any integration defining `async_setup`.
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
CARD_VERSION = "13"


async def async_setup(hass: "HomeAssistant", config: dict) -> bool:
    """Serve the bundled Lovelace card and register it as a Lovelace resource."""
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
        # Matches by reconstructed unique_id, not string-splitting: names may contain "_".
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

    # Refreshes should_run/locked every minute without re-running the search (both are pure
    # functions of "now"). Must be @callback, not a bare lambda, or HA's thread-safety check
    # silently drops the update.
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
