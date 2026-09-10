"""Forecast-provider layer: parsing, entity discovery and source resolution for Solcast/Helios
Forecast/Forecast.Solar, plus the virtual Average/Min/Weighted combiner modes. Kept separate from
coordinator.py (which owns scheduling/Store/standby-learning) since this half of the module is a
self-contained concern: "given a config entry, get this provider's forecast curve", with no
dependency on the coordinator's own state.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR,
    CONF_FORECAST_CONFIG_ENTRY_HELIOS,
    CONF_FORECAST_CONFIG_ENTRY_SOLCAST,
    DOMAIN,
    FORECAST_PROVIDER_AVERAGE,
    FORECAST_PROVIDER_FORECAST_SOLAR,
    FORECAST_PROVIDER_HELIOS,
    FORECAST_PROVIDER_MIN,
    FORECAST_PROVIDER_SOLCAST,
    FORECAST_PROVIDER_WEIGHTED,
)
from .scheduling import average_forecast_points, min_forecast_points

_LOGGER = logging.getLogger(__name__)


def _parse_solcast_points(state) -> list[dict]:
    detailed = state.attributes.get("detailedForecast")
    if not isinstance(detailed, list):
        return []
    points = []
    for p in detailed:
        try:
            # Solcast's in-memory period_start is a real datetime, only a string over the WS/REST API.
            period_start = p["period_start"]
            if isinstance(period_start, str):
                period_start = dt_util.parse_datetime(period_start)
            if period_start is None:
                continue
            w = float(p.get("pv_estimate", 0)) * 1000
            points.append(
                {
                    "time": period_start,
                    "w": w,
                    "w10": float(p["pv_estimate10"]) * 1000 if p.get("pv_estimate10") is not None else w,
                    "w90": float(p["pv_estimate90"]) * 1000 if p.get("pv_estimate90") is not None else w,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points


def _parse_helios_points(state) -> list[dict]:
    forecast = state.attributes.get("forecast")
    if not isinstance(forecast, list):
        return []
    points = []
    for p in forecast:
        try:
            raw_time = p["datetime"]
            time = dt_util.parse_datetime(raw_time) if isinstance(raw_time, str) else raw_time
            if time is None:
                continue
            w = float(p.get("watts", 0))
            points.append(
                {
                    "time": time,
                    "w": w,
                    "w10": float(p["p10"]) if p.get("p10") is not None else w,
                    "w90": float(p["p90"]) if p.get("p90") is not None else w,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points


_FORECAST_PARSERS = {
    FORECAST_PROVIDER_SOLCAST: _parse_solcast_points,
    FORECAST_PROVIDER_HELIOS: _parse_helios_points,
}


def _read_forecast_points(hass: HomeAssistant, entity_id: str | None, provider: str) -> list[dict]:
    if not entity_id:
        return []
    state = hass.states.get(entity_id)
    if state is None:
        return []
    parser = _FORECAST_PARSERS.get(provider)
    if parser is None:
        _LOGGER.warning("Unknown forecast provider %r, falling back to Solcast parsing", provider)
        parser = _parse_solcast_points
    return sorted(parser(state), key=lambda pt: pt["time"])


def _discover_provider_entities(
    hass: HomeAssistant, config_entry_id: str, attribute_key: str, device_class: str | None = None
) -> list[str]:
    """Every entity registered under a config entry whose *current* state carries the given list
    attribute (and device_class, when given). A disabled entity has no state at all
    (hass.states.get() returns None), so this naturally only picks up entities the user has
    actually enabled — most of Solcast's day_3..7 sensors are disabled by default, and must be
    included automatically once the user enables one, never hand-maintained as a fixed
    "today"/"tomorrow" list.

    The device_class filter matters: Helios Forecast exposes its own "forecast" list attribute on
    several unrelated sensors too (cloud_cover, temperature, wind_speed, snow_depth, irradiance),
    none of which carry a "watts" key, so without this filter they'd get parsed as a flood of
    0-valued points at their own (hourly) timestamps, interleaved with power_now's real (15-min)
    ones: the exact "drops to 0 every hour" artifact reported live 2026-09-10. device_class is
    optional since Helios's forecast_reliability sensor (see _helios_reliability_weight()) carries
    none at all, unlike every other entity this function discovers.
    """
    registry = er.async_get(hass)
    entity_ids = []
    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        state = hass.states.get(entry.entity_id)
        if (
            state is not None
            and isinstance(state.attributes.get(attribute_key), list)
            and (device_class is None or state.attributes.get("device_class") == device_class)
        ):
            entity_ids.append(entry.entity_id)
    return entity_ids


async def _read_solcast_points(hass: HomeAssistant, config_entry_id: str) -> list[dict]:
    """Every enabled Solcast entity carrying a detailedForecast list on this config entry, merged
    and sorted: no manual entity picking (today/tomorrow/day 3...), whatever the user has enabled
    in Solcast's own entity list is used automatically.
    """
    points = []
    for entity_id in _discover_provider_entities(hass, config_entry_id, "detailedForecast", "energy"):
        points += _read_forecast_points(hass, entity_id, FORECAST_PROVIDER_SOLCAST)
    return sorted(points, key=lambda pt: pt["time"])


async def _read_helios_points(hass: HomeAssistant, config_entry_id: str) -> list[dict]:
    """Helios only ever has one relevant entity today, but discovered the same way as Solcast for
    consistency (and so a future Helios variant exposing more than one forecast entity would just
    work with no code change here).
    """
    points = []
    for entity_id in _discover_provider_entities(hass, config_entry_id, "forecast", "power"):
        points += _read_forecast_points(hass, entity_id, FORECAST_PROVIDER_HELIOS)
    return sorted(points, key=lambda pt: pt["time"])


def _helios_reliability_weight(hass: HomeAssistant, config_entry_id: str) -> float:
    """Helios's own forecast_reliability sensor (0..100%), normalized to a 0..1 weight for the
    "Weighted" Helios+Solcast blend. Discovered via its "per_day" list attribute: unlike every
    other entity _discover_provider_entities() finds, this sensor carries no device_class at all.
    Falls back to 0.0 (trust Solcast entirely) if the sensor doesn't exist yet or its state isn't a
    usable number: the safe default when Helios hasn't published a confidence figure.
    """
    for entity_id in _discover_provider_entities(hass, config_entry_id, "per_day"):
        state = hass.states.get(entity_id)
        if state is None:
            continue
        try:
            return max(0.0, min(1.0, float(state.state) / 100))
        except ValueError:
            continue
    return 0.0


async def _read_forecast_solar_points(hass: HomeAssistant, config_entry_id: str) -> list[dict]:
    """forecast_solar has no per-point forecast attribute on any entity at all (verified against
    the real integration): its curve is only reachable via HA's own "energy platform" hook
    (async_get_solar_forecast), the same mechanism the Energy dashboard's own solar-forecast picker
    uses. No P10/P90: it exposes a single estimate, so w10/w90 collapse to w like any source
    lacking percentiles.
    """
    try:
        integration = await async_get_integration(hass, FORECAST_PROVIDER_FORECAST_SOLAR)
        platform = await integration.async_get_platform("energy")
    except (IntegrationNotFound, ImportError):
        return []
    get_forecast = getattr(platform, "async_get_solar_forecast", None)
    if get_forecast is None:
        return []
    try:
        data = await get_forecast(hass, config_entry_id)
    except Exception:  # noqa: BLE001 - forecast_solar's own hook isn't defensive about this
        # Hit live: raises AttributeError ("ConfigEntry has no attribute runtime_data") when its
        # own coordinator hasn't completed a first refresh yet (e.g. right after the entry was
        # added, or no network to reach the real forecast.solar API) — entry.runtime_data is only
        # ever assigned once that first refresh succeeds. An unhandled exception here would fail
        # this whole coordinator's update, not just this one provider's points.
        _LOGGER.warning("forecast_solar's energy platform failed for config entry %s", config_entry_id, exc_info=True)
        return []
    if not data:
        return []
    points = []
    for iso_time, wh in data.get("wh_hours", {}).items():
        time = dt_util.parse_datetime(iso_time)
        if time is None:
            continue
        try:
            w = float(wh)
        except (TypeError, ValueError):
            continue
        points.append({"time": time, "w": w, "w10": w, "w90": w})
    return sorted(points, key=lambda pt: pt["time"])


# Every provider's own (hass, config_entry_id) -> points reader: all three are symmetric now,
# always keyed by config_entry_id (see resolve_forecast_sources), never an entity_id directly.
_PROVIDER_POINT_READERS = {
    FORECAST_PROVIDER_SOLCAST: _read_solcast_points,
    FORECAST_PROVIDER_HELIOS: _read_helios_points,
    FORECAST_PROVIDER_FORECAST_SOLAR: _read_forecast_solar_points,
}


async def _read_provider_points(hass: HomeAssistant, resolved_sources: dict[str, list[str]], provider: str) -> list[dict]:
    """Every point of every config entry resolved for one provider, merged and sorted by time."""
    reader = _PROVIDER_POINT_READERS.get(provider)
    if reader is None:
        return []
    points = []
    for config_entry_id in resolved_sources.get(provider, []):
        points += await reader(hass, config_entry_id)
    return sorted(points, key=lambda pt: pt["time"])


# Virtual forecast-source modes offered alongside real providers in the card's select, each
# combining every currently resolved real provider's points into one curve. No leading underscore,
# unlike _FORECAST_PARSERS: this table is imported by select.py to build its option list, so it's
# deliberately public.
FORECAST_COMBINERS = {
    FORECAST_PROVIDER_AVERAGE: average_forecast_points,
    FORECAST_PROVIDER_MIN: min_forecast_points,
}


def resolve_forecast_sources(data: dict) -> dict[str, list[str]]:
    """Configured forecast providers, resolved from the dedicated per-provider fields: each field
    holds a config_entry_id, not an entity_id — {provider: [config_entry_id]} for all three. The
    config_flow only ever asks the user to tick which providers to use, in one multi-select; it
    resolves that to these concrete fields itself, once, at submission time (see
    _resolve_provider_selection() in config_flow.py) — never re-looked-up here on every read, and
    this function stays a pure, hass-free lookup. The actual entities (or, for forecast_solar, the
    energy-platform hook) are resolved from the id at read time (see _PROVIDER_POINT_READERS), so
    this function never needs to know any provider's raw shape.
    """
    result: dict[str, list[str]] = {}
    solcast_entry = data.get(CONF_FORECAST_CONFIG_ENTRY_SOLCAST)
    if solcast_entry:
        result[FORECAST_PROVIDER_SOLCAST] = [solcast_entry]
    helios_entry = data.get(CONF_FORECAST_CONFIG_ENTRY_HELIOS)
    if helios_entry:
        result[FORECAST_PROVIDER_HELIOS] = [helios_entry]
    forecast_solar_entry = data.get(CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR)
    if forecast_solar_entry:
        result[FORECAST_PROVIDER_FORECAST_SOLAR] = [forecast_solar_entry]
    return result


def resolve_forecast_history_entities(hass: HomeAssistant, entry_id: str, data: dict) -> dict[str, str]:
    """Entity whose own state history reconstructs a forecast curve before "now": detailedForecast/
    forecast aren't kept by the recorder (verified live), but a provider's plain "power now" sensor
    is, since it's just a simple numeric state. {provider: entity_id}, only for providers where one
    was found. forecast_solar has no history-entity support: its data never comes through an entity
    at all. "average"/"min" resolve to this entry's own AverageForecastPowerNowSensor/
    MinForecastPowerNowSensor (sensor.py) instead of a raw provider entity, once 2+ providers are
    configured — looked up by unique_id via the entity registry, not string-built, so a user rename
    doesn't break the mapping.
    """
    resolved = resolve_forecast_sources(data)
    registry = er.async_get(hass)
    result: dict[str, str] = {}
    if FORECAST_PROVIDER_HELIOS in resolved:
        entities = _discover_provider_entities(hass, resolved[FORECAST_PROVIDER_HELIOS][0], "forecast", "power")
        if entities:
            result[FORECAST_PROVIDER_HELIOS] = entities[0]
    if FORECAST_PROVIDER_SOLCAST in resolved:
        for entry in er.async_entries_for_config_entry(registry, resolved[FORECAST_PROVIDER_SOLCAST][0]):
            if entry.entity_id.endswith("_power_now"):
                result[FORECAST_PROVIDER_SOLCAST] = entry.entity_id
                break
    if len(resolved) >= 2:
        for provider, unique_suffix in (
            (FORECAST_PROVIDER_AVERAGE, "forecast_average_power_now"),
            (FORECAST_PROVIDER_MIN, "forecast_min_power_now"),
        ):
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{unique_suffix}")
            if entity_id:
                result[provider] = entity_id
    if FORECAST_PROVIDER_SOLCAST in resolved and FORECAST_PROVIDER_HELIOS in resolved:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_forecast_weighted_power_now")
        if entity_id:
            result[FORECAST_PROVIDER_WEIGHTED] = entity_id
    return result
