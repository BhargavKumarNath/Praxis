"""A tiny marts warehouse with exactly known contents (SYNTHETIC), for market / input tests.

Days 2026-07-01 .. 2026-07-21; decisions are taken as of 2026-07-20, so 2026-07-20 and
2026-07-21 are "the future" and must never be read. One region (eu_west), two products.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

START = date(2026, 7, 1)
DAYS = 21
AS_OF = date(2026, 7, 20)
FUTURE = (date(2026, 7, 20), date(2026, 7, 21))


def build(path: Path, *, with_cost: bool = True, gpu_price: bool = True) -> Path:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    con.execute(
        "CREATE TABLE marts.fct_marginal_cost_daily (event_date DATE, region_id VARCHAR, "
        "product VARCHAR, hours BIGINT, cost_micros_sum BIGINT, avg_cost_micros DOUBLE)"
    )
    con.execute(
        "CREATE TABLE marts.fct_service_metrics_hourly (event_date DATE, region_id VARCHAR, "
        "utilization DOUBLE, error_rate DOUBLE, latency_p95_ms DOUBLE)"
    )
    con.execute(
        "CREATE TABLE marts.fct_usage_daily (event_date DATE, customer_id VARCHAR, "
        "product VARCHAR, region_id VARCHAR, units BIGINT, throttled_units BIGINT, "
        "usage_value_micros BIGINT, observations BIGINT)"
    )
    con.execute(
        "CREATE TABLE marts.dim_customer (customer_id VARCHAR, initial_tier VARCHAR, "
        "churned_at TIMESTAMPTZ)"
    )
    con.execute(
        "CREATE TABLE marts.fct_payments (event_date DATE, invoice_id VARCHAR, "
        "attempt_number BIGINT, amount_minor BIGINT, is_final_failure BOOLEAN)"
    )
    con.execute(
        "CREATE TABLE marts.fct_price_exposures (product VARCHAR, event_date DATE, "
        "list_price_micros BIGINT)"
    )
    for k in range(DAYS):
        d = START + timedelta(days=k)
        future = d in FUTURE
        if with_cost:
            for product, cost in (("api_requests", 120_000), ("gpu_minutes", 30_000)):
                c = cost * (10 if future else 1)  # the future is poisoned: reading it shows
                con.execute(
                    "INSERT INTO marts.fct_marginal_cost_daily VALUES (?, 'eu_west', ?, 24, ?, ?)",
                    [d, product, c * 24, float(c)],
                )
        util = 0.99 if future else (0.5 + 0.01 * k)
        for _ in range(24):
            con.execute(
                "INSERT INTO marts.fct_service_metrics_hourly VALUES (?, 'eu_west', ?, 0.001, 50)",
                [d, util],
            )
        for cid, tier in (("c1", "growth"), ("c2", "enterprise")):
            units = 1000 if future else 100
            con.execute(
                "INSERT INTO marts.fct_usage_daily "
                "VALUES (?, ?, 'api_requests', 'eu_west', ?, ?, ?, 1)",
                [d, cid, units, 10 if tier == "growth" else 0, units * 400_000],
            )
    con.execute(
        "INSERT INTO marts.dim_customer VALUES ('c1', 'growth', NULL), "
        "('c2', 'enterprise', NULL), ('c3', 'growth', TIMESTAMPTZ '2026-07-10 12:00:00+00')"
    )
    con.execute(
        "INSERT INTO marts.fct_payments VALUES "
        "(DATE '2026-07-05', 'i1', 1, 10000, false), (DATE '2026-07-06', 'i1', 2, 10000, false), "
        "(DATE '2026-07-06', 'i2', 1, 30000, false), (DATE '2026-07-09', 'i2', 2, 30000, true), "
        "(DATE '2026-07-20', 'i3', 1, 99999, true)"
    )
    con.execute(
        "INSERT INTO marts.fct_price_exposures VALUES "
        "('api_requests', DATE '2026-01-05', 400000), ('api_requests', DATE '2026-02-02', 400000), "
        "('api_requests', DATE '2026-07-02', 368000), ('api_requests', DATE '2026-07-20', 1)"
    )
    if gpu_price:
        con.execute(
            "INSERT INTO marts.fct_price_exposures VALUES ('gpu_minutes', DATE '2026-01-05', 45000)"
        )
    con.close()
    return path
