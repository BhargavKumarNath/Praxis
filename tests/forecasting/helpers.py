"""Small, fully known panels and warehouses for forecasting tests (SYNTHETIC)."""

from __future__ import annotations

import csv
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from praxis.forecasting.config import ForecastConfig, load_forecast_config
from praxis.forecasting.features import Catalogue
from praxis.forecasting.panel import CONTEXT_COLUMNS, DemandPanel, PricePlan, SeriesKey

START = date(2026, 1, 5)  # a Monday
REGIONS = ("r_east", "r_west")
PRODUCTS = ("p_api", "p_gpu")
SEGMENTS = ("s_big", "s_small")
PRICE_CHANGE_DAY = 60


def series_keys() -> tuple[SeriesKey, ...]:
    return tuple(sorted(SeriesKey(r, p, s) for r in REGIONS for p in PRODUCTS for s in SEGMENTS))


def make_panel(n_days: int = 112, seed: int = 3, noise: float = 0.08) -> DemandPanel:
    """Weekly seasonality x trend x Gamma noise; demand drops 10% after the price change."""
    rng = np.random.default_rng(seed)
    keys = series_keys()
    days = np.arange(n_days)
    dow = (START.weekday() + days) % 7
    demand = np.zeros((len(keys), n_days))
    for i, k in enumerate(keys):
        base = (400.0 if k.segment == "s_big" else 40.0) * (3.0 if k.product == "p_api" else 1.0)
        season = 1.0 + 0.3 * np.cos(2 * np.pi * (dow - (i % 7)) / 7)
        trend = 1.0 + 0.002 * days
        price = np.where((k.product == "p_api") & (days >= PRICE_CHANGE_DAY), 0.9, 1.0)
        mean = base * season * trend * price
        demand[i] = np.round(rng.gamma(1 / noise**2, mean * noise**2))
    served = np.floor(demand * 0.97)
    active = np.full_like(demand, 5.0)
    context = {c: rng.normal(1.0, 0.1, (len(REGIONS), n_days)) for c in CONTEXT_COLUMNS}
    return DemandPanel(START, keys, REGIONS, demand, served, active, context)


def make_plan() -> PricePlan:
    return PricePlan(
        {
            "p_api": ((START, 1000), (START + timedelta(days=PRICE_CHANGE_DAY), 900)),
            "p_gpu": ((START, 500),),
        }
    )


def catalogue() -> Catalogue:
    return Catalogue.from_series(series_keys())


def small_config(**overrides: Any) -> ForecastConfig:
    """Production config with a fast model and a short backtest (same code paths)."""
    raw = load_forecast_config().model_dump(mode="json")
    raw["lightgbm"].update(num_boost_round=25, num_threads=1, min_data_in_leaf=20)
    raw["lightgbm"]["calibration_days"] = 14
    raw["backtest"].update(initial_train_days=70, step_days=7, bootstrap_samples=200)
    raw["evaluation"]["value_weights"] = {"p_api": 0.4, "p_gpu": 0.05}
    for section, values in overrides.items():
        raw[section].update(values)
    return ForecastConfig.model_validate(raw)


def _bulk(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple[Any, ...]]) -> None:
    """COPY via a temp CSV: row-by-row executemany is orders of magnitude slower in DuckDB."""
    if not rows:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False) as fh:
        csv.writer(fh).writerows(rows)
        name = fh.name
    try:
        con.execute(f"COPY {table} FROM '{name}' (HEADER false)")
    finally:
        Path(name).unlink()


def _service_rows(
    panel: DemandPanel, n_complete: int, jitter: float, order_seed: int | None
) -> list[tuple[Any, ...]]:
    noise = np.random.default_rng(99).normal(0.0, 1.0, (panel.n_days, len(panel.regions), 24, 3))
    svc = []
    for d in range(panel.n_days):
        for ri, r in enumerate(panel.regions):
            base = [
                float(panel.context[c][ri, d])
                for c in ("avg_utilization", "avg_error_rate", "avg_latency_p95_ms")
            ]
            for h in range(24 if d < n_complete else 12):
                vals = [b * (1 + jitter * noise[d, ri, h, k]) for k, b in enumerate(base)]
                svc.append((panel.date_of(d), r, h, *vals))
    if order_seed is not None:
        order = np.random.default_rng(order_seed).permutation(len(svc))
        svc = [svc[i] for i in order.tolist()]
    return svc


def write_warehouse(
    path: Path,
    panel: DemandPanel,
    plan: PricePlan,
    *,
    complete_days: int | None = None,
    hourly_jitter: float = 0.0,
    row_order_seed: int | None = None,
) -> None:
    """A DuckDB file with just the marts the forecaster reads, built from a known panel.

    One synthetic customer per series (initial tier = segment). ``complete_days`` limits how
    many days get all 24 hourly service rows (later days look unfinished). Hourly service
    values equal the panel's daily context; ``hourly_jitter`` makes them vary by hour (fixed
    values), and ``row_order_seed`` shuffles their physical insertion order.
    """
    con = duckdb.connect(str(path))
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA marts")
    con.execute(
        "CREATE TABLE marts.dim_customer (customer_id VARCHAR, region_id VARCHAR, "
        "initial_tier VARCHAR, current_tier VARCHAR)"
    )
    con.execute(
        "CREATE TABLE marts.fct_usage_daily (event_date DATE, customer_id VARCHAR, "
        "product VARCHAR, region_id VARCHAR, units BIGINT, throttled_units BIGINT)"
    )
    con.execute("CREATE TABLE marts.dim_region (region_id VARCHAR)")
    cols = ", ".join(f"{c} DOUBLE" for c in CONTEXT_COLUMNS)
    con.execute(
        f"CREATE TABLE marts.feat_region_daily (region_id VARCHAR, feature_date DATE, {cols})"
    )
    con.execute(
        "CREATE TABLE marts.fct_price_exposures (event_date DATE, product VARCHAR, "
        "list_price_micros BIGINT)"
    )
    con.execute(
        "CREATE TABLE marts.fct_service_metrics_hourly (event_date DATE, region_id VARCHAR, "
        "hour INT, utilization DOUBLE, error_rate DOUBLE, latency_p95_ms DOUBLE)"
    )
    for r in panel.regions:
        con.execute("INSERT INTO marts.dim_region VALUES (?)", [r])
    usage = []
    for i, k in enumerate(panel.series):
        cid = f"cust_{i:04d}"
        # current_tier deliberately differs: the loader must ignore it
        con.execute(
            "INSERT INTO marts.dim_customer VALUES (?, ?, ?, ?)",
            [cid, k.region_id, k.segment, "changed_later"],
        )
        for d in range(panel.n_days):
            if panel.demand[i, d] > 0:
                served = int(panel.served[i, d])
                usage.append(
                    (
                        panel.date_of(d),
                        cid,
                        k.product,
                        k.region_id,
                        served,
                        int(panel.demand[i, d]) - served,
                    )
                )
    _bulk(con, "marts.fct_usage_daily", usage)
    ctx_rows = [
        (r, panel.date_of(d), *(float(panel.context[c][ri, d]) for c in CONTEXT_COLUMNS))
        for ri, r in enumerate(panel.regions)
        for d in range(panel.n_days)
    ]
    _bulk(con, "marts.feat_region_daily", ctx_rows)
    prices = []
    for product in plan.changes:
        for d in range(panel.n_days):
            price = plan.price_on(product, panel.date_of(d))
            if price is not None:
                prices.append((panel.date_of(d), product, price))
    _bulk(con, "marts.fct_price_exposures", prices)
    n_complete = panel.n_days if complete_days is None else complete_days
    svc = _service_rows(panel, n_complete, hourly_jitter, row_order_seed)
    _bulk(con, "marts.fct_service_metrics_hourly", svc)
    con.close()
