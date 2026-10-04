"""Read the demand panel and price plan from the dbt marts (DuckDB warehouse, Phase 2).

Reads only ``marts.*`` (the warehouse contract), always bounded by the partition column.
Segment is ``dim_customer.initial_tier``, never ``current_tier``, which reflects tier
changes that may happen after the forecast origin.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np

from praxis.forecasting.panel import CONTEXT_COLUMNS, DemandPanel, PricePlan, SeriesKey

_USAGE_SQL = """
SELECT u.event_date, u.region_id, u.product, c.initial_tier AS segment,
       sum(u.units + u.throttled_units)::DOUBLE AS demand,
       sum(u.units)::DOUBLE AS served,
       count(DISTINCT u.customer_id)::DOUBLE AS active
FROM marts.fct_usage_daily AS u
JOIN marts.dim_customer AS c ON u.customer_id = c.customer_id
WHERE u.event_date BETWEEN ? AND ?
GROUP BY ALL
"""

_CONTEXT_SQL = f"""
SELECT region_id, feature_date, {", ".join(f"{c}::DOUBLE" for c in CONTEXT_COLUMNS)}
FROM marts.feat_region_daily
WHERE feature_date BETWEEN ? AND ?
"""  # noqa: S608 - column names are module constants

_PRICE_SQL = """
SELECT product, event_date, max(list_price_micros)::BIGINT
FROM marts.fct_price_exposures
WHERE event_date <= ?
GROUP BY product, event_date
ORDER BY product, event_date
"""

_COMPLETE_DAY_SQL = """
SELECT max(event_date) FROM (
    SELECT event_date FROM marts.fct_service_metrics_hourly
    WHERE event_date BETWEEN ? AND ?
    GROUP BY event_date
    HAVING count(*) >= 24 * (SELECT count(*) FROM marts.dim_region)
)
"""


class WarehouseUnavailable(RuntimeError):
    """The warehouse file or the marts it needs are missing."""


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    if not path.exists():
        raise WarehouseUnavailable(f"warehouse not found: {path}")
    con = duckdb.connect(str(path), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def usage_date_range(con: duckdb.DuckDBPyConnection) -> tuple[date, date] | None:
    try:
        row = con.execute(
            "SELECT min(event_date), max(event_date) FROM marts.fct_usage_daily"
        ).fetchone()
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable("marts.fct_usage_daily is missing (run dbt)") from exc
    if row is None or row[0] is None:
        return None
    return row[0], row[1]


def latest_complete_day(
    con: duckdb.DuckDBPyConnection, *, not_after: date, lookback_days: int = 60
) -> date | None:
    """Latest day with all 24 hourly service rows for every region (a finished day)."""
    try:
        row = con.execute(
            _COMPLETE_DAY_SQL, [not_after - timedelta(days=lookback_days), not_after]
        ).fetchone()
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable("service metrics mart is missing (run dbt)") from exc
    return None if row is None else row[0]


def load_price_plan(con: duckdb.DuckDBPyConnection, *, until: date) -> PricePlan:
    """List-price change points per product, from logged exposures up to ``until``."""
    rows = con.execute(_PRICE_SQL, [until]).fetchall()
    changes: dict[str, list[tuple[date, int]]] = {}
    for product, d, price in rows:
        points = changes.setdefault(product, [])
        if not points or points[-1][1] != price:
            points.append((d, int(price)))
    return PricePlan({p: tuple(v) for p, v in changes.items()})


def load_panel(
    con: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    *,
    series: Sequence[SeriesKey] | None = None,
) -> DemandPanel:
    """Dense panel for ``start..end``. A series without usage on a day has demand 0.

    With ``series`` given, exactly those series are returned (missing ones are all-zero);
    otherwise every series observed in the window.
    """
    if end < start:
        raise ValueError("end before start")
    n = (end - start).days + 1
    usage = con.execute(_USAGE_SQL, [start, end]).fetchall()
    keys = (
        sorted(set(series))
        if series is not None
        else sorted({SeriesKey(r[1], r[2], r[3]) for r in usage})
    )
    index = {k: i for i, k in enumerate(keys)}
    demand, served, active = (np.zeros((len(keys), n)) for _ in range(3))
    for d, region, product, segment, dem, srv, act in usage:
        i = index.get(SeriesKey(region, product, segment))
        if i is None:
            continue
        day = (d - start).days
        demand[i, day], served[i, day], active[i, day] = dem, srv, act

    region_rows = con.execute("SELECT region_id FROM marts.dim_region ORDER BY 1").fetchall()
    regions = tuple(sorted({r[0] for r in region_rows} | {k.region_id for k in keys}))
    r_index = {r: i for i, r in enumerate(regions)}
    context = {c: np.full((len(regions), n), np.nan) for c in CONTEXT_COLUMNS}
    for region, d, *values in con.execute(_CONTEXT_SQL, [start, end]).fetchall():
        if region not in r_index:
            continue
        for c, v in zip(CONTEXT_COLUMNS, values, strict=True):
            if v is not None:
                context[c][r_index[region], (d - start).days] = v
    return DemandPanel(
        start_date=start,
        series=tuple(keys),
        regions=regions,
        demand=demand,
        served=served,
        active=active,
        context=context,
    )
