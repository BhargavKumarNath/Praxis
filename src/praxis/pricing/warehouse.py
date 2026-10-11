"""Market inputs for one decision date, from the dbt marts (DuckDB warehouse).

Every query reads only ``marts.*`` and is bounded by the partition column, ending on the
day BEFORE the decision date: a decision at the start of day T uses data through T - 1.

* list prices and change dates         (fct_price_exposures, via the forecasting price plan)
* marginal cost per region and product  (fct_marginal_cost_daily, recent mean)
* utilisation per region                (fct_service_metrics_hourly, recent peak daily mean)
* served share per region/product/tier  (fct_usage_daily: served / requested)
* payment loss rate                     (fct_payments: invoiced value finally uncollected)

Customers, churn and lifetime value come from ``praxis.customer`` (Phase 9), not from here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import duckdb

from praxis.forecasting.panel import PricePlan
from praxis.forecasting.warehouse import load_price_plan
from praxis.pricing.config import Valuation


@dataclass(frozen=True)
class MarketSnapshot:
    cutoff: date  # last day of data used
    plan: PricePlan
    unit_cost: dict[tuple[str, str], float]  # (region, product) -> micro-GBP per unit
    utilization: dict[str, float]  # region -> peak daily-mean utilisation
    served_share: dict[tuple[str, str, str], float]  # (region, product, tier)
    payment_loss_rate: float | None

    def version(self) -> str:
        """Content reference recorded on every decision (feature snapshot lineage)."""
        body = {
            "cutoff": self.cutoff.isoformat(),
            "plan": self.plan.version(),
            "unit_cost": sorted((list(k), v) for k, v in self.unit_cost.items()),
            "utilization": sorted(self.utilization.items()),
            "served_share": sorted((list(k), v) for k, v in self.served_share.items()),
            "rest": {k: v for k, v in asdict(self).items() if k == "payment_loss_rate"},
        }
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "market-" + hashlib.sha256(text.encode()).hexdigest()[:16]


_COST_SQL = """
SELECT region_id, product, sum(cost_micros_sum)::DOUBLE / sum(hours)
FROM marts.fct_marginal_cost_daily
WHERE event_date BETWEEN ? AND ?
GROUP BY ALL
"""

_UTIL_SQL = """
SELECT region_id, max(daily)::DOUBLE FROM (
    SELECT region_id, event_date, avg(utilization::DECIMAL(38, 12)) AS daily
    FROM marts.fct_service_metrics_hourly
    WHERE event_date BETWEEN ? AND ?
    GROUP BY ALL
) GROUP BY ALL
"""

_SERVED_SQL = """
SELECT u.region_id, u.product, c.initial_tier,
       sum(u.units)::DOUBLE / nullif(sum(u.units + u.throttled_units), 0)
FROM marts.fct_usage_daily AS u
JOIN marts.dim_customer AS c ON u.customer_id = c.customer_id
WHERE u.event_date BETWEEN ? AND ?
GROUP BY ALL
"""

_LOSS_SQL = """
WITH invoices AS (
    SELECT invoice_id, max(CASE WHEN attempt_number = 1 THEN amount_minor END) AS amount,
           bool_or(is_final_failure) AS lost
    FROM marts.fct_payments
    WHERE event_date BETWEEN ? AND ?
    GROUP BY invoice_id
)
SELECT sum(amount)::DOUBLE, sum(CASE WHEN lost THEN amount ELSE 0 END)::DOUBLE
FROM invoices WHERE amount IS NOT NULL
"""


def load_market(con: duckdb.DuckDBPyConnection, as_of: date, v: Valuation) -> MarketSnapshot:
    cutoff = as_of - timedelta(days=1)

    def window(days: int) -> list[date]:
        return [cutoff - timedelta(days=days - 1), cutoff]

    cost = {
        (r, p): float(c)
        for r, p, c in con.execute(_COST_SQL, window(v.cost_lookback_days)).fetchall()
    }
    util = {
        r: float(u)
        for r, u in con.execute(_UTIL_SQL, window(v.utilization_lookback_days)).fetchall()
    }
    cw = window(v.customer_lookback_days)
    served = {
        (r, p, t): float(s)
        for r, p, t, s in con.execute(_SERVED_SQL, cw).fetchall()
        if s is not None
    }
    invoiced, lost = con.execute(_LOSS_SQL, window(v.payment_loss_lookback_days)).fetchone() or (
        None,
        None,
    )
    return MarketSnapshot(
        cutoff=cutoff,
        plan=load_price_plan(con, until=cutoff),
        unit_cost=cost,
        utilization=util,
        served_share=served,
        payment_loss_rate=None if not invoiced else float(lost or 0.0) / float(invoiced),
    )
