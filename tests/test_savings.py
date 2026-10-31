"""Tests for savings.py (pure math) and SolarSavingsSensor's statistics wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_planner_scheduler import sensor as sensor_module
from custom_components.solar_planner_scheduler.const import (
    CONF_PRICE_TRACKING_ENABLED,
    CONF_SAVINGS_CONSUMPTION_ENTITY,
    CONF_SAVINGS_ENABLED,
    CONF_SAVINGS_IMPORT_ENTITY,
    CONF_SAVINGS_INVESTMENT,
    CONF_SAVINGS_START_DATE,
    CONF_TARIFF_BANDS,
    DOMAIN,
)
from custom_components.solar_planner_scheduler.savings import hourly_savings, payback
from custom_components.solar_planner_scheduler.sensor import SolarSavingsSensor

_H0 = datetime(2026, 7, 29, 10, tzinfo=UTC)


def _hour(n: int) -> datetime:
    return _H0 + timedelta(hours=n)


def _flat(price: float):
    return lambda _t: price


def _zero_import(*hours: int) -> list[dict]:
    return [{"start": _hour(n), "change": 0.0} for n in hours]


def test_hourly_savings_values_consumed_minus_imported_per_hour():
    consumption = [{"start": _hour(0), "change": 2.0}, {"start": _hour(1), "change": 3.0}]
    grid_import = [{"start": _hour(0), "change": 0.5}, {"start": _hour(1), "change": 1.0}]
    assert hourly_savings(consumption, grid_import, _flat(0.2)) == (1.5 + 2.0) * 0.2


def test_hourly_savings_prices_each_hour_at_its_own_tariff():
    consumption = [{"start": _hour(0), "change": 1.0}, {"start": _hour(12), "change": 1.0}]
    assert hourly_savings(consumption, _zero_import(0, 12), lambda t: 0.3 if t == _hour(0) else 0.1) == 0.4


def test_hourly_savings_never_counts_an_hour_negative():
    """Import exceeding consumption within an hour (meter skew), or a negative change left by a
    meter glitch, must not subtract from the total."""
    consumption = [{"start": _hour(0), "change": 1.0}, {"start": _hour(1), "change": -5.0}, {"start": _hour(2), "change": 1.0}]
    grid_import = [{"start": _hour(0), "change": 2.0}, *_zero_import(1, 2)]
    assert hourly_savings(consumption, grid_import, _flat(1.0)) == 1.0


def test_hourly_savings_skips_hours_missing_from_import():
    """A gap in the import meter's statistics must not count that hour's whole consumption as solar
    (at night that would be pure invention)."""
    consumption = [{"start": _hour(0), "change": 2.0}, {"start": _hour(1), "change": 4.0}]
    assert hourly_savings(consumption, _zero_import(0), _flat(1.0)) == 2.0


def test_hourly_savings_treats_none_changes_as_zero():
    consumption = [{"start": _hour(0), "change": 2.0}, {"start": _hour(1), "change": None}]
    grid_import = [{"start": _hour(0), "change": None}, *_zero_import(1)]
    assert hourly_savings(consumption, grid_import, _flat(1.0)) == 2.0


def test_payback_projects_linearly_from_the_daily_average():
    attrs = payback(savings=100.0, investment=1000.0, start=date(2026, 7, 29), today=date(2026, 8, 8))
    assert attrs["remaining"] == 900.0
    assert attrs["progress"] == 10.0
    # 10 €/day over 10 days, 900 € left: 90 more days.
    assert attrs["payback_date"] == "2026-11-06"
    assert attrs["days_of_data"] == 10
    assert attrs["estimate_reliable"] is False


def test_payback_is_reliable_only_after_a_full_year():
    attrs = payback(savings=500.0, investment=1000.0, start=date(2026, 7, 29), today=date(2027, 7, 29))
    assert attrs["estimate_reliable"] is True


def test_payback_without_savings_yet_has_no_date():
    attrs = payback(savings=0.0, investment=1000.0, start=date(2026, 7, 29), today=date(2026, 7, 29))
    assert attrs["payback_date"] is None
    assert attrs["progress"] == 0.0
    assert attrs["days_of_data"] == 1


def test_payback_once_paid_off_clamps_and_dates_today():
    attrs = payback(savings=1200.0, investment=1000.0, start=date(2026, 7, 29), today=date(2036, 1, 1))
    assert attrs["remaining"] == 0.0
    assert attrs["progress"] == 100.0
    assert attrs["payback_date"] == "2036-01-01"


# --- SolarSavingsSensor ---


def _savings_data(**overrides) -> dict:
    return {
        CONF_PRICE_TRACKING_ENABLED: True,
        CONF_TARIFF_BANDS: [{"start": "00:00", "price": 0.2}],
        CONF_SAVINGS_ENABLED: True,
        CONF_SAVINGS_CONSUMPTION_ENTITY: "sensor.house_energy",
        CONF_SAVINGS_IMPORT_ENTITY: "sensor.grid_imported",
        CONF_SAVINGS_START_DATE: "2026-07-29",
        CONF_SAVINGS_INVESTMENT: 10900,
        **overrides,
    }


async def _refreshed_sensor(hass, monkeypatch, data: dict, stats: dict) -> tuple[SolarSavingsSensor, list]:
    """Run async_refresh_savings() against canned recorder rows (the real recorder's shape: `start`
    as a UTC timestamp float), returning the sensor and the statistics_during_period() calls made."""
    calls = []

    def fake_statistics_during_period(_hass, start, end, ids, period, units, types):
        calls.append((start, end, ids, period, units, types))
        return stats

    class _FakeRecorder:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    monkeypatch.setattr(sensor_module, "statistics_during_period", fake_statistics_during_period)
    monkeypatch.setattr(sensor_module, "get_instance", lambda _hass: _FakeRecorder())
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    sensor = SolarSavingsSensor(hass, entry)
    sensor.hass = hass
    sensor.entity_id = "sensor.solar_planner_scheduler_savings"
    await sensor.async_refresh_savings()
    return sensor, calls


async def test_savings_sensor_sums_consumption_minus_import_from_hourly_statistics(hass, monkeypatch):
    stats = {
        "sensor.house_energy": [
            {"start": _hour(0).timestamp(), "change": 2.0},
            {"start": _hour(1).timestamp(), "change": 3.0},
        ],
        "sensor.grid_imported": [
            {"start": _hour(0).timestamp(), "change": 0.0},
            {"start": _hour(1).timestamp(), "change": 1.0},
        ],
    }
    sensor, calls = await _refreshed_sensor(hass, monkeypatch, _savings_data(), stats)

    assert sensor.native_value == round((2.0 + 2.0) * 0.2, 2)
    assert sensor.extra_state_attributes["investment"] == 10900.0
    assert sensor.extra_state_attributes["estimate_reliable"] is False
    (start, end, ids, period, units, types) = calls[0]
    assert start.date() == date(2026, 7, 29)
    assert end is None
    assert ids == {"sensor.house_energy", "sensor.grid_imported"}
    assert (period, units, types) == ("hour", {"energy": "kWh"}, {"change"})


async def test_savings_sensor_is_none_when_tracking_is_disabled(hass, monkeypatch):
    for data in (_savings_data(**{CONF_SAVINGS_ENABLED: False}), _savings_data(**{CONF_PRICE_TRACKING_ENABLED: False})):
        sensor, calls = await _refreshed_sensor(hass, monkeypatch, data, {})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}
        assert calls == []
