"""Tests for savings.py (pure math) and SolarSavingsSensor's Energy-dashboard and statistics wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import sensor as sensor_module
from custom_components.solar_planner_scheduler.const import (
    CONF_SAVINGS_ENABLED,
    CONF_SAVINGS_INVESTMENT,
    CONF_SAVINGS_RESALE_START_DATE,
    CONF_SAVINGS_START_DATE,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.savings import (
    Price,
    energy_sources,
    hourly_sum,
    payback,
    price_lookup,
    solar_gains,
)
from custom_components.solar_planner_scheduler.sensor import SolarSavingsSensor

_H0 = datetime(2026, 7, 29, 10, tzinfo=UTC)


def _hour(n: int) -> datetime:
    return _H0 + timedelta(hours=n)


def _flat(price: float):
    return lambda _t: price


# Shape of a real Energy dashboard config (unified grid format), taken from the live instance.
_PREFS = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.solar_old"},
        {"type": "solar", "stat_energy_from": "sensor.solar_new"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_import",
            "stat_energy_to": "sensor.grid_export",
            "entity_energy_price": "sensor.import_price",
            "number_energy_price": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": 0.09,
        },
    ]
}


# --- energy_sources() ---


def test_energy_sources_collects_every_solar_source_export_meter_and_prices():
    sources = energy_sources(_PREFS)
    assert sources.solar == ["sensor.solar_old", "sensor.solar_new"]
    assert sources.export == ["sensor.grid_export"]
    assert sources.import_price == Price(entity="sensor.import_price")
    assert sources.export_price == Price(number=0.09)
    assert sources.energy_ids() == {"sensor.solar_old", "sensor.solar_new", "sensor.grid_export"}
    assert sources.price_entities() == {"sensor.import_price"}


def test_energy_sources_without_prefs_or_prices_is_empty():
    assert energy_sources(None).solar == []
    sources = energy_sources({"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid_import"}]})
    assert sources.export == []
    assert sources.import_price is None
    assert sources.export_price is None


# --- hourly_sum() / price_lookup() ---


def test_hourly_sum_adds_meters_per_hour_and_treats_none_as_zero():
    rows = {
        "a": [{"start": _hour(0), "change": 1.0}, {"start": _hour(1), "change": None}],
        "b": [{"start": _hour(0), "change": 2.0}],
        "ignored": [{"start": _hour(0), "change": 99.0}],
    }
    assert hourly_sum(rows, ["a", "b"]) == {_hour(0): 3.0, _hour(1): 0.0}


def test_price_lookup_uses_the_hourly_mean_and_carries_it_over_gaps():
    means = [{"start": _hour(1), "mean": 0.2}, {"start": _hour(5), "mean": 0.3}, {"start": _hour(6), "mean": None}]
    at = price_lookup(Price(entity="sensor.import_price"), means)
    assert at(_hour(0)) == 0.0
    assert at(_hour(1)) == 0.2
    assert at(_hour(3)) == 0.2
    assert at(_hour(6)) == 0.3


def test_price_lookup_fixed_number_and_missing_price():
    assert price_lookup(Price(number=0.15), [])(_hour(0)) == 0.15
    assert price_lookup(None, [])(_hour(0)) == 0.0


# --- solar_gains() ---


def test_solar_gains_values_self_consumption_at_import_and_export_at_export_price():
    solar = {_hour(0): 2.0, _hour(1): 3.0}
    exported = {_hour(0): 0.5, _hour(1): 1.0}
    savings, resale = solar_gains(solar, exported, _flat(0.2), _flat(0.1))
    assert savings == (1.5 + 2.0) * 0.2
    assert resale == (0.5 + 1.0) * 0.1


def test_solar_gains_never_counts_an_hour_negative():
    """Export exceeding solar within an hour (meter skew) must not subtract from savings."""
    savings, resale = solar_gains({_hour(0): 1.0, _hour(1): 1.0}, {_hour(0): 2.0}, _flat(1.0), _flat(0.0))
    assert savings == 1.0
    assert resale == 0.0


def test_solar_gains_counts_resale_only_from_resale_from_but_always_subtracts_export():
    """Export before the resale contract starts earns nothing, yet still isn't self-consumed."""
    solar = {_hour(0): 2.0, _hour(1): 2.0}
    exported = {_hour(0): 1.0, _hour(1): 1.0}
    savings, resale = solar_gains(solar, exported, _flat(0.2), _flat(0.1), resale_from=_hour(1))
    assert savings == (1.0 + 1.0) * 0.2
    assert resale == 1.0 * 0.1


def test_solar_gains_prices_each_hour_at_its_own_price():
    savings, _ = solar_gains({_hour(0): 1.0, _hour(12): 1.0}, {}, lambda t: 0.3 if t == _hour(0) else 0.1, _flat(0.0))
    assert savings == 0.4


# --- payback() ---


def test_payback_projects_linearly_from_the_daily_average():
    attrs = payback(gains=100.0, investment=1000.0, start=date(2026, 7, 29), today=date(2026, 8, 8))
    assert attrs["remaining"] == 900.0
    assert attrs["progress"] == 10.0
    # 10 €/day over 10 days, 900 € left: 90 more days.
    assert attrs["payback_date"] == "2026-11-06"
    assert attrs["days_of_data"] == 10
    assert attrs["estimate_reliable"] is False


def test_payback_is_reliable_only_after_a_full_year():
    attrs = payback(gains=500.0, investment=1000.0, start=date(2026, 7, 29), today=date(2027, 7, 29))
    assert attrs["estimate_reliable"] is True


def test_payback_without_gains_yet_has_no_date():
    attrs = payback(gains=0.0, investment=1000.0, start=date(2026, 7, 29), today=date(2026, 7, 29))
    assert attrs["payback_date"] is None
    assert attrs["progress"] == 0.0
    assert attrs["days_of_data"] == 1


def test_payback_once_paid_off_clamps_and_dates_today():
    attrs = payback(gains=1200.0, investment=1000.0, start=date(2026, 7, 29), today=date(2036, 1, 1))
    assert attrs["remaining"] == 0.0
    assert attrs["progress"] == 100.0
    assert attrs["payback_date"] == "2036-01-01"


# --- SolarSavingsSensor ---


def _entry_data(**overrides) -> dict:
    return {
        CONF_SAVINGS_ENABLED: True,
        CONF_SAVINGS_START_DATE: "2026-07-29",
        CONF_SAVINGS_INVESTMENT: 10900,
        **overrides,
    }


async def _refreshed_sensor(hass, monkeypatch, data: dict, prefs: dict | None, stats: dict) -> tuple[SolarSavingsSensor, list]:
    """Run async_refresh_savings() against canned Energy prefs and recorder rows (the real
    recorder's shape: `start` as a UTC timestamp float), returning the sensor and the
    statistics_during_period() calls made."""
    calls = []

    def fake_statistics_during_period(_hass, start, end, ids, period, units, types):
        calls.append((start, end, ids, period, units, types))
        return {statistic_id: rows for statistic_id, rows in stats.items() if statistic_id in ids}

    class _FakeRecorder:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    class _FakeEnergyManager:
        def __init__(self):
            self.data = prefs

    async def fake_energy_manager(_hass):
        return _FakeEnergyManager()

    monkeypatch.setattr(sensor_module, "statistics_during_period", fake_statistics_during_period)
    monkeypatch.setattr(sensor_module, "get_instance", lambda _hass: _FakeRecorder())
    monkeypatch.setattr(sensor_module, "async_get_energy_manager", fake_energy_manager)
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    sensor = SolarSavingsSensor(hass, entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.solar_planner_scheduler_savings"
    await sensor.async_refresh_savings()
    return sensor, calls


async def test_savings_sensor_sums_self_consumption_and_resale_from_energy_prefs(hass, monkeypatch):
    stats = {
        "sensor.solar_old": [{"start": _hour(0).timestamp(), "change": 2.0}],
        "sensor.solar_new": [{"start": _hour(1).timestamp(), "change": 3.0}],
        "sensor.grid_export": [{"start": _hour(1).timestamp(), "change": 1.0}],
        "sensor.import_price": [{"start": _hour(0).timestamp(), "mean": 0.2}],
    }
    sensor, calls = await _refreshed_sensor(hass, monkeypatch, _entry_data(), _PREFS, stats)

    savings = (2.0 + 2.0) * 0.2
    resale = 1.0 * 0.09
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == round(savings + resale, 2)
    assert attrs["self_consumption_savings"] == round(savings, 2)
    assert attrs["resale_revenue"] == round(resale, 2)
    assert attrs["investment"] == 10900.0
    energy_call, price_call = calls
    assert energy_call[0].date() == date(2026, 7, 29)
    assert energy_call[2:] == ({"sensor.solar_old", "sensor.solar_new", "sensor.grid_export"}, "hour", {"energy": "kWh"}, {"change"})
    assert price_call[2:] == ({"sensor.import_price"}, "hour", None, {"mean"})


async def test_savings_sensor_ignores_export_revenue_before_the_resale_start_date(hass, monkeypatch):
    day1 = datetime(2026, 8, 1, 10, tzinfo=UTC)
    stats = {
        "sensor.solar_new": [{"start": day1.timestamp(), "change": 3.0}],
        "sensor.grid_export": [
            {"start": day1.timestamp(), "change": 1.0},
            {"start": (day1 + timedelta(days=2)).timestamp(), "change": 2.0},
        ],
        "sensor.import_price": [{"start": day1.timestamp(), "mean": 0.2}],
    }
    data = _entry_data(**{CONF_SAVINGS_RESALE_START_DATE: "2026-08-02"})
    sensor, _ = await _refreshed_sensor(hass, monkeypatch, data, _PREFS, stats)

    assert sensor.extra_state_attributes["self_consumption_savings"] == round((3.0 - 1.0) * 0.2, 2)
    assert sensor.extra_state_attributes["resale_revenue"] == round(2.0 * 0.09, 2)


async def test_savings_sensor_is_none_when_disabled_or_energy_dashboard_incomplete(hass, monkeypatch):
    no_price = {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar_new"}]}
    cases = [
        (_entry_data(**{CONF_SAVINGS_ENABLED: False}), _PREFS),
        (_entry_data(), None),
        (_entry_data(), no_price),
    ]
    for data, prefs in cases:
        sensor, calls = await _refreshed_sensor(hass, monkeypatch, data, prefs, {})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}
        assert calls == []
