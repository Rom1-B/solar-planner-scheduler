"""Solar savings and payback math: pure functions, no homeassistant import.

Savings are recomputed in full from the recorder's hourly long-term statistics rather than
accumulated live: the recorder already handles meter resets in its `change` values, and the
installation date backfill then needs no separate code path.

Self-consumed solar is derived as house consumption minus grid import, not production minus
export: it stays correct even when the solar production meter is missing or misreading, as it
was on the real install for most of its first month.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

# Below one year of data, a payback projection is biased by the seasons covered so far.
FULL_YEAR_DAYS = 365


def hourly_savings(
    consumption_rows: Sequence[dict],
    import_rows: Sequence[dict],
    price_at_hour: Callable[[datetime], float],
) -> float:
    """Sum over hours of max(0, consumed - imported) x price at that hour.

    Rows are recorder statistics: `start` (datetime) and `change` (kWh over that hour). An hour
    missing from import_rows is skipped: treating it as zero import would count every kWh as solar.
    """
    imported = {row["start"]: row.get("change") or 0.0 for row in import_rows}
    total = 0.0
    for row in consumption_rows:
        if row["start"] not in imported:
            continue
        self_consumed = max(0.0, (row.get("change") or 0.0) - imported[row["start"]])
        if self_consumed:
            total += self_consumed * price_at_hour(row["start"])
    return total


def payback(savings: float, investment: float, start: date, today: date) -> dict:
    """Payback attributes, projected linearly from the average daily savings since start."""
    days = max((today - start).days, 1)
    remaining = max(investment - savings, 0.0)
    payback_date = None
    if remaining == 0:
        payback_date = today
    elif savings > 0:
        payback_date = today + timedelta(days=round(remaining / (savings / days)))
    return {
        "investment": investment,
        "remaining": round(remaining, 2),
        "progress": round(min(savings / investment, 1.0) * 100, 1) if investment > 0 else None,
        "payback_date": payback_date.isoformat() if payback_date else None,
        "days_of_data": days,
        "estimate_reliable": days >= FULL_YEAR_DAYS,
    }
