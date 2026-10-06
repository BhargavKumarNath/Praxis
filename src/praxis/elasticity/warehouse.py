"""Read randomised price-test data from the dbt marts (DuckDB warehouse, Phase 2).

Reads only ``marts.*`` (the warehouse contract), always bounded by the partition column and
by the experiment registry. Nothing here knows the simulator: arms, prices and outcomes come
from logged events, exactly as they would from a real experimentation platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from praxis.elasticity.config import ElasticityConfig, ExperimentDesign, ExperimentRegistry

_CUSTOMERS_SQL = """
SELECT customer_id, initial_tier, industry, region_id, is_existing,
       CAST(created_at AS DATE), CAST(churned_at AS DATE)
FROM marts.dim_customer
"""

_USAGE_SQL = """
SELECT customer_id,
       coalesce(sum(units + throttled_units) FILTER (WHERE event_date < ?), 0)::BIGINT,
       coalesce(sum(units + throttled_units) FILTER (WHERE event_date >= ?), 0)::BIGINT,
       coalesce(sum(units) FILTER (WHERE event_date >= ?), 0)::BIGINT,
       coalesce(sum(usage_value_micros) FILTER (WHERE event_date >= ?), 0)::BIGINT
FROM marts.fct_usage_daily
WHERE product = ? AND event_date BETWEEN ? AND ?
GROUP BY customer_id
"""

_EXPOSURE_SQL = """
SELECT customer_id, event_date, arm, unit_price_micros, list_price_micros
FROM marts.fct_price_exposures
WHERE experiment_id = ? AND product = ? AND event_date BETWEEN ? AND ?
ORDER BY customer_id, event_date, exposure_id
"""


class WarehouseUnavailable(RuntimeError):
    """The warehouse file or the marts it needs are missing."""


@dataclass(frozen=True)
class Customer:
    tier: str  # tier at signup (pre-treatment); never the current tier
    industry: str
    region: str
    is_existing: bool
    created: date
    churned: date | None


@dataclass(frozen=True)
class Exposure:
    day: date
    arm: str | None
    unit_price_micros: int
    list_price_micros: int


@dataclass(frozen=True)
class Usage:
    pre_requested: int
    post_requested: int
    post_served: int
    post_value_micros: int


@dataclass(frozen=True)
class ExperimentExtract:
    design: ExperimentDesign
    usage: dict[str, Usage]
    exposures: dict[str, tuple[Exposure, ...]]  # every logged exposure of this test, by customer


@dataclass(frozen=True)
class WarehouseExtract:
    customers: dict[str, Customer]
    experiments: tuple[ExperimentExtract, ...]


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    if not path.exists():
        raise WarehouseUnavailable(f"warehouse not found: {path}")
    con = duckdb.connect(str(path), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def load_extract(
    con: duckdb.DuckDBPyConnection, registry: ExperimentRegistry, cfg: ElasticityConfig
) -> WarehouseExtract:
    try:
        customers = {
            r[0]: Customer(r[1], r[2], r[3], bool(r[4]), r[5], r[6])
            for r in con.execute(_CUSTOMERS_SQL).fetchall()
        }
        experiments = tuple(
            _experiment(con, d, cfg.eligibility.pre_window_days) for d in registry.experiments
        )
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable("experiment marts are missing (run dbt)") from exc
    return WarehouseExtract(customers, experiments)


def _experiment(
    con: duckdb.DuckDBPyConnection, design: ExperimentDesign, pre_days: int
) -> ExperimentExtract:
    start, end = design.assignment_date, design.end_date
    pre_start = design.pre_start(pre_days)
    usage = {
        r[0]: Usage(int(r[1]), int(r[2]), int(r[3]), int(r[4]))
        for r in con.execute(
            _USAGE_SQL, [start, start, start, start, design.product, pre_start, end]
        ).fetchall()
    }
    exposures: dict[str, list[Exposure]] = {}
    for cid, day, arm, price, list_price in con.execute(
        _EXPOSURE_SQL, [design.id, design.product, start, end]
    ).fetchall():
        exposures.setdefault(cid, []).append(Exposure(day, arm, int(price), int(list_price)))
    return ExperimentExtract(design, usage, {k: tuple(v) for k, v in exposures.items()})
