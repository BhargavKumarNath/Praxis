"""Executive view: revenue, gross margin, demand, active customers, churn, recovery rate and a
price index, as daily series from the warehouse marts (the stores of record for history; the
metric snapshots hold latest values only, ADR 0020).

Each figure is its own panel: a mart that is not built makes only that figure unavailable.
Every figure carries its definition, the latest simulated day, and a comparison with the
median of the 28 days before it. All values are SYNTHETIC simulator output; the days are
simulated days, not calendar time.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from praxis.console.context import ConsoleContext
from praxis.console.panels import SYNTHETIC_NOTE, Data, figure, jsonable, panel
from praxis.data.warehouse import Warehouse

WINDOW_DAYS = 56
BASELINE_DAYS = 28
BASELINE_LABEL = f"median of the previous {BASELINE_DAYS} days"


@dataclass(frozen=True)
class Metric:
    source: str
    unit: str
    definition: str
    sql: str


METRICS: dict[str, Metric] = {
    "revenue": Metric(
        "duckdb: marts.fct_payments",
        "GBP minor units per day",
        "Sum of succeeded payment attempts, by the day the attempt was resolved.",
        "SELECT CAST(coalesce(resolved_at, attempted_at) AS DATE) AS day, "
        "sum(amount_minor) AS value FROM marts.fct_payments "
        "WHERE outcome = 'succeeded' AND currency = 'GBP' GROUP BY 1",
    ),
    "gross_margin": Metric(
        "duckdb: marts.fct_usage_daily, marts.fct_marginal_cost_daily",
        "ratio",
        "(usage value - units x daily marginal cost) / usage value, per day, over all regions "
        "and products. Usage revenue only; base fees are excluded.",
        "SELECT u.event_date AS day, (sum(u.usage_value_micros) - sum(u.units * c.avg_cost_micros))"
        " / nullif(sum(u.usage_value_micros), 0) AS value FROM marts.fct_usage_daily u "
        "JOIN marts.fct_marginal_cost_daily c ON c.event_date = u.event_date "
        "AND c.region_id = u.region_id AND c.product = u.product GROUP BY 1",
    ),
    "demand": Metric(
        "duckdb: marts.fct_usage_daily",
        "billing units per day",
        "Sum of served usage units over all products and regions (throttled units excluded).",
        "SELECT event_date AS day, sum(units) AS value FROM marts.fct_usage_daily GROUP BY 1",
    ),
    "active_customers": Metric(
        "duckdb: marts.dim_customer",
        "customers",
        "Customers created on or before the day and not churned by the end of it.",
        "WITH b AS (SELECT min(CAST(created_at AS DATE)) AS lo, "
        "  max(coalesce(CAST(churned_at AS DATE), CAST(created_at AS DATE))) AS hi "
        "  FROM marts.dim_customer), "
        "d AS (SELECT CAST(g AS DATE) AS day FROM b, generate_series(b.lo, b.hi, INTERVAL 1 DAY) "
        "  t(g)) "
        "SELECT d.day, count(c.customer_id) AS value FROM d LEFT JOIN marts.dim_customer c "
        "ON CAST(c.created_at AS DATE) <= d.day "
        "AND (c.churned_at IS NULL OR CAST(c.churned_at AS DATE) > d.day) GROUP BY 1",
    ),
    "churn": Metric(
        "duckdb: marts.dim_customer",
        "customers per day",
        "Customers whose churn was observed on the day.",
        "SELECT CAST(churned_at AS DATE) AS day, count(*) AS value FROM marts.dim_customer "
        "WHERE churned_at IS NOT NULL GROUP BY 1",
    ),
    "recovery_rate": Metric(
        "duckdb: marts.fct_payments",
        "ratio",
        "Of invoices whose first attempt failed on the day, the share later paid. Recent days "
        "are immature: their invoices may still recover.",
        "WITH f AS (SELECT invoice_id, min(event_date) AS day FROM marts.fct_payments "
        "  WHERE outcome = 'failed' AND attempt_number = 1 GROUP BY 1), "
        "p AS (SELECT DISTINCT invoice_id FROM marts.fct_payments WHERE outcome = 'succeeded') "
        "SELECT f.day, avg(CASE WHEN p.invoice_id IS NULL THEN 0.0 ELSE 1.0 END) AS value "
        "FROM f LEFT JOIN p USING (invoice_id) GROUP BY 1",
    ),
    "price_index": Metric(
        "duckdb: marts.fct_price_exposures",
        "index (first day = 100)",
        "Mean over products of the day's average list price divided by the product's first-day "
        "average list price, x 100. List prices shown to customers, not optimiser proposals.",
        "WITH d AS (SELECT event_date AS day, product, avg(list_price_micros) AS p "
        "  FROM marts.fct_price_exposures GROUP BY 1, 2), "
        "f AS (SELECT product, arg_min(p, day) AS p0 FROM d GROUP BY 1) "
        "SELECT d.day, 100 * avg(d.p / f.p0) AS value FROM d JOIN f USING (product) GROUP BY 1",
    ),
}


def _series(wh: Warehouse, metric: Metric) -> Data | None:
    result = wh.con.execute(
        f"SELECT day, value FROM ({metric.sql}) WHERE value IS NOT NULL "  # noqa: S608 - constant SQL
        "ORDER BY day DESC LIMIT ?",
        [WINDOW_DAYS + BASELINE_DAYS],
    ).fetchall()
    if not result:
        return None
    days = [{"day": jsonable(d), "value": float(v)} for d, v in reversed(result)]
    latest = days[-1]
    history = [d["value"] for d in days[-(BASELINE_DAYS + 1) : -1]]
    baseline = statistics.median(history) if history else None
    return {
        "figure": figure(
            latest["value"],
            unit=metric.unit,
            baseline=baseline,
            baseline_label=BASELINE_LABEL,
            series=days[-WINDOW_DAYS:],
        ),
        "latest_day": latest["day"],
        "baseline_days": len(history),
        "definition": metric.definition,
    }


class _Opened:
    """One read-only warehouse connection for the whole view, or the error that prevented it
    (re-raised inside each panel so every figure reports it)."""

    def __init__(self, ctx: ConsoleContext) -> None:
        self.wh: Warehouse | None = None
        self.error: BaseException | None = None
        try:
            self.wh = Warehouse(ctx.warehouse(), read_only=True)
        except Exception as error:
            self.error = error

    def read(self, metric: Metric) -> Callable[[], Data | None]:
        def run() -> Data | None:
            if self.wh is None:
                assert self.error is not None  # noqa: S101 - set when wh is None
                raise self.error
            return _series(self.wh, metric)

        return run

    def close(self) -> None:
        if self.wh is not None:
            self.wh.close()


def executive_view(ctx: ConsoleContext) -> Data:
    opened = _Opened(ctx)
    try:
        panels: dict[str, Any] = {
            name: panel(
                m.source,
                opened.read(m),
                empty=f"the mart behind this figure has no rows ({m.source.split(': ')[1]})",
            )
            for name, m in METRICS.items()
        }
    finally:
        opened.close()
    return {
        "view": "executive",
        "generated_at": ctx.clock().isoformat(),
        "data_origin": "synthetic",
        "note": f"{SYNTHETIC_NOTE} Days are simulated days from the warehouse.",
        "window_days": WINDOW_DAYS,
        "panels": panels,
    }
