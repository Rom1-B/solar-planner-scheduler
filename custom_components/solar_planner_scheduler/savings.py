"""Solar gains and payback math from the Energy dashboard's configuration: pure functions, no
homeassistant import.

Gains are recomputed in full from the recorder's hourly long-term statistics rather than
accumulated live: the recorder already handles meter resets in its `change` values, and the
installation date backfill then needs no separate code path.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# Below one year of data, a payback projection is biased by the seasons covered so far.
FULL_YEAR_DAYS = 365


@dataclass
class Price:
    """A price from the Energy dashboard: a recorded entity (hourly mean) or a fixed number."""

    entity: str | None = None
    number: float | None = None


@dataclass
class EnergySources:
    solar: list[str] = field(default_factory=list)
    export: list[str] = field(default_factory=list)
    import_price: Price | None = None
    export_price: Price | None = None

    def energy_ids(self) -> set[str]:
        return {*self.solar, *self.export}

    def price_entities(self) -> set[str]:
        return {p.entity for p in (self.import_price, self.export_price) if p and p.entity}


def _price(entity: str | None, number: float | None) -> Price | None:
    if entity:
        return Price(entity=entity)
    if number is not None:
        return Price(number=number)
    return None


def energy_sources(prefs: dict | None) -> EnergySources:
    """Solar meters, grid export meters and prices from the Energy dashboard preferences.

    Every solar source and every grid's export meter is summed; the import/export price comes from
    the first grid source that defines one.
    """
    sources = EnergySources()
    for source in (prefs or {}).get("energy_sources", []):
        if source.get("type") == "solar" and source.get("stat_energy_from"):
            sources.solar.append(source["stat_energy_from"])
        elif source.get("type") == "grid":
            if source.get("stat_energy_to"):
                sources.export.append(source["stat_energy_to"])
            sources.import_price = sources.import_price or _price(
                source.get("entity_energy_price"), source.get("number_energy_price")
            )
            sources.export_price = sources.export_price or _price(
                source.get("entity_energy_price_export"), source.get("number_energy_price_export")
            )
    return sources


def hourly_sum(rows_by_id: dict[str, list[dict]], ids: list[str]) -> dict[datetime, float]:
    """Per-hour sum of `change` (kWh) across the given statistics. Rows: `start` (datetime), `change`."""
    total: dict[datetime, float] = {}
    for statistic_id in ids:
        for row in rows_by_id.get(statistic_id, []):
            total[row["start"]] = total.get(row["start"], 0.0) + (row.get("change") or 0.0)
    return total


def price_lookup(price: Price | None, means: list[dict]) -> Callable[[datetime], float]:
    """Price at an hour: a fixed number, or that hour's recorded mean, carrying the last known mean
    over hours the price entity has no statistics for. 0 when no price is configured or known yet."""
    if price is None:
        return lambda _hour: 0.0
    if price.entity is None:
        return lambda _hour: price.number
    known = sorted((row["start"], row["mean"]) for row in means if row.get("mean") is not None)
    starts = [start for start, _ in known]

    def at(hour: datetime) -> float:
        index = bisect_right(starts, hour)
        return known[index - 1][1] if index else 0.0

    return at


def solar_gains(
    solar: dict[datetime, float],
    exported: dict[datetime, float],
    import_price: Callable[[datetime], float],
    export_price: Callable[[datetime], float],
    resale_from: datetime | None = None,
) -> tuple[float, float]:
    """(self-consumption savings, resale revenue): per hour, max(0, solar - export) at the import
    price, and export at the export price from resale_from on (a resale contract often starts
    after the installation; export before it still isn't self-consumption)."""
    savings = sum(max(0.0, kwh - exported.get(hour, 0.0)) * import_price(hour) for hour, kwh in solar.items())
    resale = sum(
        kwh * export_price(hour)
        for hour, kwh in exported.items()
        if kwh > 0 and (resale_from is None or hour >= resale_from)
    )
    return savings, resale


def payback(gains: float, investment: float, start: date, today: date) -> dict:
    """Payback attributes, projected linearly from the average daily gains since start."""
    days = max((today - start).days, 1)
    remaining = max(investment - gains, 0.0)
    payback_date = None
    if remaining == 0:
        payback_date = today
    elif gains > 0:
        payback_date = today + timedelta(days=round(remaining / (gains / days)))
    return {
        "investment": investment,
        "remaining": round(remaining, 2),
        "progress": round(min(gains / investment, 1.0) * 100, 1) if investment > 0 else None,
        "payback_date": payback_date.isoformat() if payback_date else None,
        "days_of_data": days,
        "estimate_reliable": days >= FULL_YEAR_DAYS,
    }
