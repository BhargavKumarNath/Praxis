"""A tiny marts warehouse with exactly known contents (SYNTHETIC) for point-in-time tests.

Days 2026-06-01 .. 2026-08-19. Snapshots are taken as of 2026-07-20 (``AS_OF``): every row on or
after it is "the future" and poisoned (x100 usage, failures, broken service, extra price
changes, tier changes), so any feature that reads it shows up in the assertions.

Customers (all eu_west):
  c1 growth since 2026-01-01; two failed attempts before AS_OF (one more after); price changes
     on 2026-04-01 (outside 90 days), 2026-06-01 and 2026-07-01 (+ one in the future)
  c2 started growth, enterprise from 2026-06-10, back to growth on 2026-07-25 (future)
  c3 starter, churned voluntarily 2026-07-05 (before AS_OF: not in the snapshot)
  c4 growth, churns voluntarily 2026-07-30 (label: voluntary, 11 days at risk)
  c5 starter, churns for non-payment 2026-08-05 (label: involuntary, 17 days at risk)
  c6 prospect created 2026-07-01 that never subscribed (not a customer)
  c7 created 2026-07-25 (the future)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

START = date(2026, 6, 1)
END = date(2026, 8, 19)
AS_OF = date(2026, 7, 20)
API_PRICE = 400_000
API_COST = 120_000  # per unit, every hour


def ts(d: date, hour: int = 12) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


CUSTOMERS = [
    # id, created, tenure at start, churned, reason, initial tier, current tier (END of data)
    ("c1", date(2026, 1, 1), 100, None, None, "growth", "growth"),
    ("c2", date(2026, 1, 1), 300, None, None, "growth", "growth"),
    ("c3", date(2026, 1, 1), 50, date(2026, 7, 5), "voluntary", "starter", "starter"),
    ("c4", date(2026, 1, 1), 20, date(2026, 7, 30), "voluntary", "growth", "growth"),
    ("c5", date(2026, 3, 1), 0, date(2026, 8, 5), "involuntary_payment", "starter", "starter"),
    ("c6", date(2026, 7, 1), 0, None, None, "starter", "starter"),
    ("c7", date(2026, 7, 25), 0, None, None, "growth", "growth"),
]
SUBSCRIPTIONS = [
    # id, customer, effective date, kind, tier, fee
    ("s1", "c1", date(2026, 1, 1), "started", "growth", 9900),
    ("s2", "c2", date(2026, 1, 1), "started", "growth", 9900),
    ("s2b", "c2", date(2026, 6, 10), "changed", "enterprise", 99900),
    ("s2c", "c2", date(2026, 7, 25), "changed", "growth", 9900),  # future
    ("s3", "c3", date(2026, 1, 1), "started", "starter", 900),
    ("s4", "c4", date(2026, 1, 1), "started", "growth", 9900),
    ("s5", "c5", date(2026, 3, 1), "started", "starter", 900),
    ("s7", "c7", date(2026, 7, 25), "started", "growth", 9900),  # future
]
# daily api_requests units before AS_OF; the last 14 days double for c1 (usage trend)
UNITS = {"c1": 10, "c2": 100, "c3": 5, "c4": 3, "c5": 1}


def build(path: Path) -> Path:
    con = duckdb.connect(str(path))
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA marts")
    con.execute(
        "CREATE TABLE marts.dim_customer (customer_id VARCHAR, region_id VARCHAR, "
        "industry VARCHAR, is_existing BOOLEAN, tenure_days_at_start BIGINT, "
        "created_at TIMESTAMPTZ, churned_at TIMESTAMPTZ, churn_reason VARCHAR, "
        "initial_tier VARCHAR, current_tier VARCHAR)"
    )
    for cid, created, tenure, churned, reason, tier, current in CUSTOMERS:
        con.execute(
            "INSERT INTO marts.dim_customer VALUES (?, 'eu_west', 'saas', ?, ?, ?, ?, ?, ?, ?)",
            [
                cid,
                tenure > 0,
                tenure,
                ts(created, 0),
                None if churned is None else ts(churned, 23),
                reason,
                tier,
                current,
            ],
        )
    con.execute(
        "CREATE TABLE marts.fct_subscriptions (event_id VARCHAR, customer_id VARCHAR, "
        "effective_at TIMESTAMPTZ, event_date DATE, kind VARCHAR, tier VARCHAR, "
        "base_fee_minor BIGINT)"
    )
    for sid, cid, d, kind, tier, fee in SUBSCRIPTIONS:
        con.execute(
            "INSERT INTO marts.fct_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sid, cid, ts(d, 0), d, kind, tier, fee],
        )
    _usage_and_cost(con)
    _payments_and_service(con)
    con.execute(
        "CREATE TABLE marts.fct_customer_price_changes (change_id VARCHAR, customer_id VARCHAR, "
        "product VARCHAR, occurred_at TIMESTAMPTZ, event_date DATE, "
        "previous_price_micros BIGINT, unit_price_micros BIGINT, experiment_id VARCHAR)"
    )
    for k, (cid, d) in enumerate(
        [
            ("c1", date(2026, 4, 1)),
            ("c1", date(2026, 6, 1)),
            ("c1", date(2026, 7, 1)),
            ("c1", date(2026, 7, 1)),  # two products the same day: one date
            ("c1", date(2026, 7, 22)),  # future
            ("c2", date(2026, 7, 1)),
        ]
    ):
        con.execute(
            "INSERT INTO marts.fct_customer_price_changes "
            "VALUES (?, ?, 'api_requests', ?, ?, 1, 2, NULL)",
            [f"pc{k}", cid, ts(d), d],
        )
    con.close()
    return path


def _usage_and_cost(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TABLE marts.fct_usage_daily (event_date DATE, customer_id VARCHAR, "
        "product VARCHAR, region_id VARCHAR, units BIGINT, throttled_units BIGINT, "
        "usage_value_micros BIGINT, observations BIGINT)"
    )
    con.execute(
        "CREATE TABLE marts.fct_marginal_cost_daily (event_date DATE, region_id VARCHAR, "
        "product VARCHAR, hours BIGINT, cost_micros_sum BIGINT, avg_cost_micros DOUBLE)"
    )
    days = "SELECT CAST(d AS DATE) AS d FROM range(DATE '{0}', DATE '{1}' + 1, INTERVAL 1 DAY) t(d)"
    days = days.format(START, END)
    future = f"d >= DATE '{AS_OF}'"
    for product, cost in (("api_requests", API_COST), ("gpu_minutes", 30_000)):
        con.execute(
            f"INSERT INTO marts.fct_marginal_cost_daily SELECT d, 'eu_west', '{product}', 24, "  # noqa: S608
            f"{cost} * 24 * (CASE WHEN {future} THEN 100 ELSE 1 END), "
            f"{cost} * (CASE WHEN {future} THEN 100 ELSE 1 END) FROM ({days})"
        )
    recent = f"d >= DATE '{AS_OF - timedelta(days=14)}' AND NOT ({future})"
    for cid, units in UNITS.items():
        # c1's usage doubled over the last 14 days (usage trend); the future is x100
        double = "2" if cid == "c1" else "1"
        u = (
            f"{units} * (CASE WHEN {future} THEN 100 ELSE 1 END) "
            f"* (CASE WHEN {recent} THEN {double} ELSE 1 END)"
        )
        con.execute(
            f"INSERT INTO marts.fct_usage_daily SELECT d, '{cid}', 'api_requests', 'eu_west', "  # noqa: S608
            f"{u}, 0, ({u})::BIGINT * {API_PRICE}, 1 FROM ({days})"
        )
    con.execute(  # c2 also uses gpu (shares), before AS_OF only
        "INSERT INTO marts.fct_usage_daily SELECT d, 'c2', 'gpu_minutes', 'eu_west', 100, 0, "  # noqa: S608
        f"4500000, 1 FROM ({days}) WHERE NOT ({future})"
    )


def _payments_and_service(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TABLE marts.fct_payments (event_date DATE, invoice_id VARCHAR, "
        "customer_id VARCHAR, attempt_number BIGINT, amount_minor BIGINT, outcome VARCHAR, "
        "is_final_failure BOOLEAN, resolved_at TIMESTAMPTZ)"
    )
    rows = [
        (date(2026, 7, 1), "i1", "c1", 1, 10_000, "failed", False, ts(date(2026, 7, 1))),
        (date(2026, 7, 4), "i1", "c1", 2, 10_000, "failed", False, ts(date(2026, 7, 4))),
        # attempted before AS_OF, resolved after: unknown at AS_OF
        (date(2026, 7, 19), "i2", "c1", 1, 10_000, "failed", True, ts(date(2026, 7, 21))),
        (date(2026, 7, 25), "i3", "c1", 1, 10_000, "failed", True, ts(date(2026, 7, 25))),
        (date(2026, 7, 2), "i4", "c2", 1, 30_000, "succeeded", False, ts(date(2026, 7, 2))),
    ]
    con.execute(
        "CREATE TABLE marts.fct_service_metrics_hourly (event_date DATE, region_id VARCHAR, "
        "utilization DOUBLE, error_rate DOUBLE, latency_p95_ms DOUBLE)"
    )
    for r in rows:
        con.execute("INSERT INTO marts.fct_payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(r))
    con.execute(
        "INSERT INTO marts.fct_service_metrics_hourly "  # noqa: S608
        f"SELECT CAST(d AS DATE), 'eu_west', 0.6, "
        f"CASE WHEN d >= DATE '{AS_OF}' THEN 0.5 WHEN h % 2 = 1 THEN 0.002 ELSE 0.001 END, "
        f"CASE WHEN d >= DATE '{AS_OF}' THEN 5000.0 ELSE 60.0 END "
        f"FROM range(DATE '{START}', DATE '{END}' + 1, INTERVAL 1 DAY) t(d), range(24) r(h)"
    )
