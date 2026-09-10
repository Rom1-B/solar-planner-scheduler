"""Tests for forecast_providers.py: parsing, entity discovery, and source resolution for
Solcast/Helios Forecast/Forecast.Solar. Split out of test_coordinator.py alongside the source
module split (2026-09-10): these tests exercise no coordinator/Store state at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.helpers import entity_registry as er

from custom_components.solar_planner_scheduler.const import (
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
from custom_components.solar_planner_scheduler.forecast_providers import (
    _discover_provider_entities,
    _helios_reliability_weight,
    _read_forecast_points,
    _read_forecast_solar_points,
    _read_helios_points,
    _read_solcast_points,
    resolve_forecast_history_entities,
    resolve_forecast_sources,
)
from tests.conftest import register_provider_entities


async def test_read_forecast_points_handles_a_raw_datetime_period_start(hass):
    """Regression test for the coverage_pct-always-0% bug: Solcast stores `period_start` as a
    live `datetime` object in hass.states (only serialized to a string over WS/REST/JSON), and
    dt_util.parse_datetime() used to be called on it unconditionally, raising TypeError and
    silently emptying every forecast point.
    """
    period_start = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {"detailedForecast": [{"period_start": period_start, "pv_estimate": 1.5}]},
    )

    points = _read_forecast_points(hass, "sensor.forecast", FORECAST_PROVIDER_SOLCAST)

    assert points == [{"time": period_start, "w": 1500.0, "w10": 1500.0, "w90": 1500.0}]


async def test_read_forecast_points_parses_helios_forecast_shape(hass):
    """Helios Forecast's own shape, confirmed against its real source (forecast.py's
    forecast_point_dict()): a "forecast" attribute (not "detailedForecast"), watts already in W
    (not kW), keys "datetime"/"watts"/"p10"/"p90" (not "period_start"/"pv_estimate*").
    """
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    hass.states.async_set(
        "sensor.helios_power_now",
        "1200",
        {"forecast": [{"datetime": point_time.isoformat(), "watts": 1200.0, "p10": 900.0, "p90": 1500.0}]},
    )

    points = _read_forecast_points(hass, "sensor.helios_power_now", FORECAST_PROVIDER_HELIOS)

    assert points == [{"time": point_time, "w": 1200.0, "w10": 900.0, "w90": 1500.0}]


async def test_read_forecast_points_falls_back_to_solcast_for_unknown_provider(hass):
    """An unrecognized provider key must fall back to Solcast parsing, not raise or silently
    return nothing."""
    period_start = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    hass.states.async_set(
        "sensor.forecast",
        "3",
        {"detailedForecast": [{"period_start": period_start, "pv_estimate": 1.5}]},
    )

    points = _read_forecast_points(hass, "sensor.forecast", "not_a_real_provider")

    assert points == [{"time": period_start, "w": 1500.0, "w10": 1500.0, "w90": 1500.0}]


async def test_read_forecast_solar_points_parses_wh_hours_into_uniform_points(hass, monkeypatch):
    """forecast_solar has no state attribute to read at all: its curve only comes from HA's own
    "energy platform" hook, keyed by config_entry_id, so this is mocked at the loader boundary
    (async_get_integration) rather than via hass.states.async_set() like every other provider's
    tests in this file.
    """
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)

    class _FakePlatform:
        @staticmethod
        async def async_get_solar_forecast(hass, config_entry_id):
            assert config_entry_id == "entry123"
            return {"wh_hours": {point_time.isoformat(): 1200}}

    class _FakeIntegration:
        async def async_get_platform(self, name):
            assert name == "energy"
            return _FakePlatform()

    async def _fake_async_get_integration(hass, domain):
        assert domain == FORECAST_PROVIDER_FORECAST_SOLAR
        return _FakeIntegration()

    monkeypatch.setattr(
        "custom_components.solar_planner_scheduler.forecast_providers.async_get_integration", _fake_async_get_integration
    )

    points = await _read_forecast_solar_points(hass, "entry123")

    assert points == [{"time": point_time, "w": 1200.0, "w10": 1200.0, "w90": 1200.0}]


async def test_read_forecast_solar_points_returns_empty_when_not_installed(hass, monkeypatch):
    from homeassistant.loader import IntegrationNotFound

    async def _raise(hass, domain):
        raise IntegrationNotFound(domain)

    monkeypatch.setattr("custom_components.solar_planner_scheduler.forecast_providers.async_get_integration", _raise)

    assert await _read_forecast_solar_points(hass, "entry123") == []


async def test_read_forecast_solar_points_survives_the_platform_hook_raising(hass, monkeypatch):
    """Hit live on ha-dev: forecast_solar's own async_get_solar_forecast() raised AttributeError
    ("ConfigEntry object has no attribute runtime_data"), not just returned None, because its own
    coordinator hadn't completed a first refresh yet (runtime_data is only ever assigned once that
    succeeds — e.g. right after the entry was added, or no network to reach the real forecast.solar
    API). That exception used to propagate straight out of _async_update_data(), failing this whole
    coordinator's update, not just this one provider's points. Mocked at the platform-hook boundary
    (the real forecast_solar shipped with pytest-homeassistant-custom-component's HA version
    defaults runtime_data to None on a plain MockConfigEntry, so it doesn't reproduce the exact
    live AttributeError locally — this reproduces the failure mode directly instead).
    """
    class _FakePlatform:
        @staticmethod
        async def async_get_solar_forecast(hass, config_entry_id):
            raise AttributeError("'ConfigEntry' object has no attribute 'runtime_data'")

    class _FakeIntegration:
        async def async_get_platform(self, name):
            return _FakePlatform()

    async def _fake_async_get_integration(hass, domain):
        return _FakeIntegration()

    monkeypatch.setattr(
        "custom_components.solar_planner_scheduler.forecast_providers.async_get_integration", _fake_async_get_integration
    )

    assert await _read_forecast_solar_points(hass, "entry123") == []


async def test_read_forecast_solar_points_returns_empty_for_a_nonexistent_config_entry(hass):
    """No monkeypatching here: exercises the real, installed forecast_solar integration's own
    energy.py (core HA ships it, so it's always present regardless of whether the user has
    actually set it up) end-to-end against a config_entry_id that doesn't exist, confirming the
    real async_get_solar_forecast()'s own None-on-missing-entry behavior surfaces as [], not a
    raised exception.
    """
    assert await _read_forecast_solar_points(hass, "nonexistent") == []


def test_discover_provider_entities_skips_a_disabled_entity(hass):
    """A disabled entity has no state at all: discovery must skip it silently rather than error,
    since most of Solcast's day_3..7 sensors are disabled by default.
    """
    entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.solcast_forecast_today": {"detailedForecast": [{"period_start": "x"}]},
            "sensor.solcast_forecast_day_3": None,  # registered, but no state: disabled
        },
    )
    assert _discover_provider_entities(hass, entry_id, "detailedForecast", "energy") == ["sensor.solcast_forecast_today"]


def test_discover_provider_entities_ignores_entities_without_the_attribute(hass):
    entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.solcast_forecast_today": {"detailedForecast": []},
            "sensor.solcast_api_used": {"unrelated": 3},
        },
    )
    assert _discover_provider_entities(hass, entry_id, "detailedForecast", "energy") == ["sensor.solcast_forecast_today"]


async def test_read_solcast_points_merges_every_enabled_entity_on_the_config_entry(hass):
    today = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    tomorrow = today + timedelta(days=1)
    entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.solcast_forecast_today": {"detailedForecast": [{"period_start": today, "pv_estimate": 1.0}]},
            "sensor.solcast_forecast_tomorrow": {"detailedForecast": [{"period_start": tomorrow, "pv_estimate": 2.0}]},
            "sensor.solcast_api_used": {"unrelated": 3},
        },
    )

    points = await _read_solcast_points(hass, entry_id)

    assert [pt["time"] for pt in points] == [today, tomorrow]


async def test_read_helios_points_reads_the_discovered_entity(hass):
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": point_time.isoformat(), "watts": 900.0}]}}
    )

    assert await _read_helios_points(hass, entry_id) == [{"time": point_time, "w": 900.0, "w10": 900.0, "w90": 900.0}]


async def test_read_helios_points_ignores_other_sensors_sharing_the_forecast_attribute(hass):
    """Real Helios Forecast installs expose a "forecast" list on cloud_cover/temperature/wind_speed/
    snow_depth/irradiance sensors too, none of them carrying a "watts" key: without the device_class
    filter these get parsed as a flood of 0-valued points at their own (hourly) timestamps, the live
    "drops to 0 every hour" bug reported 2026-09-10. Confirmed failing pre-fix.
    """
    point_time = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    entry_id = register_provider_entities(
        hass,
        "helios_forecast",
        {
            "sensor.helios_power_now": {"forecast": [{"datetime": point_time.isoformat(), "watts": 900.0}]},
            "sensor.helios_cloud_cover": {"forecast": [{"datetime": point_time.isoformat(), "cloud_cover": 10.0}], "device_class": None},
        },
    )

    assert await _read_helios_points(hass, entry_id) == [{"time": point_time, "w": 900.0, "w10": 900.0, "w90": 900.0}]


def test_helios_reliability_weight_reads_the_discovered_entity(hass):
    entry_id = register_provider_entities(hass, "helios_forecast", {"sensor.helios_reliability": {"per_day": [9.2]}})
    hass.states.async_set("sensor.helios_reliability", "42", {"per_day": [9.2]})

    assert _helios_reliability_weight(hass, entry_id) == 0.42


def test_helios_reliability_weight_is_zero_when_no_reliability_entity_exists(hass):
    entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1.0}]}}
    )

    assert _helios_reliability_weight(hass, entry_id) == 0.0


def test_helios_reliability_weight_is_zero_when_the_state_is_not_numeric(hass):
    entry_id = register_provider_entities(hass, "helios_forecast", {"sensor.helios_reliability": {"per_day": [9.2]}})
    hass.states.async_set("sensor.helios_reliability", "unknown", {"per_day": [9.2]})

    assert _helios_reliability_weight(hass, entry_id) == 0.0


def test_resolve_forecast_sources_reads_the_dedicated_config_entry_fields():
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: "entry_solcast", CONF_FORECAST_CONFIG_ENTRY_HELIOS: "entry_helios"}
    assert resolve_forecast_sources(data) == {
        FORECAST_PROVIDER_SOLCAST: ["entry_solcast"],
        FORECAST_PROVIDER_HELIOS: ["entry_helios"],
    }


def test_resolve_forecast_sources_reads_the_dedicated_forecast_solar_field():
    data = {CONF_FORECAST_CONFIG_ENTRY_FORECAST_SOLAR: "entry123"}
    assert resolve_forecast_sources(data) == {FORECAST_PROVIDER_FORECAST_SOLAR: ["entry123"]}


def test_resolve_forecast_sources_returns_empty_dict_when_nothing_configured():
    assert resolve_forecast_sources({}) == {}


async def test_resolve_forecast_history_entities_finds_the_helios_entity(hass):
    entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1}]}}
    )
    data = {CONF_FORECAST_CONFIG_ENTRY_HELIOS: entry_id}
    assert resolve_forecast_history_entities(hass, "own_entry", data) == {FORECAST_PROVIDER_HELIOS: "sensor.helios_power_now"}


async def test_resolve_forecast_history_entities_finds_the_solcast_power_now_entity(hass):
    entry_id = register_provider_entities(
        hass,
        "solcast_solar",
        {
            "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": []},
            "sensor.solcast_pv_forecast_power_now": {"unit": "W"},
        },
    )
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: entry_id}

    assert resolve_forecast_history_entities(hass, "own_entry", data) == {FORECAST_PROVIDER_SOLCAST: "sensor.solcast_pv_forecast_power_now"}


async def test_resolve_forecast_history_entities_omits_solcast_without_a_power_now_entity(hass):
    entry_id = register_provider_entities(hass, "solcast_solar", {"sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": []}})
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: entry_id}

    assert resolve_forecast_history_entities(hass, "own_entry", data) == {}


def test_resolve_forecast_history_entities_omits_solcast_for_a_nonexistent_config_entry(hass):
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: "nonexistent"}
    assert resolve_forecast_history_entities(hass, "own_entry", data) == {}


async def test_resolve_forecast_history_entities_resolves_average_and_min_to_this_entrys_own_sensors(hass):
    solcast_entry_id = register_provider_entities(hass, "solcast_solar", {"sensor.solcast_pv_forecast_power_now": {"unit": "W"}})
    helios_entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1}]}}
    )
    own_entry_id = "own_entry"
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{own_entry_id}_forecast_average_power_now", suggested_object_id="spf_average")
    registry.async_get_or_create("sensor", DOMAIN, f"{own_entry_id}_forecast_min_power_now", suggested_object_id="spf_min")
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id}

    result = resolve_forecast_history_entities(hass, own_entry_id, data)

    assert result[FORECAST_PROVIDER_AVERAGE] == "sensor.spf_average"
    assert result[FORECAST_PROVIDER_MIN] == "sensor.spf_min"


async def test_resolve_forecast_history_entities_omits_average_and_min_with_only_one_provider(hass):
    helios_entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1}]}}
    )
    own_entry_id = "own_entry"
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{own_entry_id}_forecast_average_power_now", suggested_object_id="spf_average")
    data = {CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id}

    result = resolve_forecast_history_entities(hass, own_entry_id, data)

    assert FORECAST_PROVIDER_AVERAGE not in result
    assert FORECAST_PROVIDER_MIN not in result


async def test_resolve_forecast_history_entities_includes_weighted_when_solcast_and_helios_are_both_configured(hass):
    solcast_entry_id = register_provider_entities(hass, "solcast_solar", {"sensor.solcast_pv_forecast_power_now": {"unit": "W"}})
    helios_entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1}]}}
    )
    own_entry_id = "own_entry"
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{own_entry_id}_forecast_weighted_power_now", suggested_object_id="spf_weighted")
    data = {CONF_FORECAST_CONFIG_ENTRY_SOLCAST: solcast_entry_id, CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id}

    result = resolve_forecast_history_entities(hass, own_entry_id, data)

    assert result[FORECAST_PROVIDER_WEIGHTED] == "sensor.spf_weighted"


async def test_resolve_forecast_history_entities_omits_weighted_without_both_solcast_and_helios(hass):
    helios_entry_id = register_provider_entities(
        hass, "helios_forecast", {"sensor.helios_power_now": {"forecast": [{"datetime": "x", "watts": 1}]}}
    )
    own_entry_id = "own_entry"
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{own_entry_id}_forecast_weighted_power_now", suggested_object_id="spf_weighted")
    data = {CONF_FORECAST_CONFIG_ENTRY_HELIOS: helios_entry_id}

    result = resolve_forecast_history_entities(hass, own_entry_id, data)

    assert FORECAST_PROVIDER_WEIGHTED not in result
