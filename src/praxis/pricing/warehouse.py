"""Market inputs for one decision date, from the dbt marts (DuckDB warehouse).

Every query reads only ``marts.*`` and is bounded by the partition column, ending on the
day BEFORE the decision date: a decision at the start of day T uses data through T - 1.

* list prices and change dates         (fct_price_exposures, via the forecasting price plan)
* marginal cost per region and product  (fct_marginal_cost_daily, recent mean)
* utilisation per region                (fct_service_metrics_hourly, recent peak daily mean)
* served share per region/product/tier  (fct_usage_daily: served / requested)
* payment loss rate                     (fct_payments: invoiced value finally uncollected)
* exposed customers, CLV proxy          (fct_usage_daily x cost, dim_customer churn)
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
    exposed_customers: dict[str, int]  # product -> active customers using it
    daily_contribution_per_customer: float | None  # micro-GBP per active customer-day
    daily_churn_hazard: float | None

    def clv_micros(self, cap_days: int) -> float | None:
        if self.daily_contribution_per_customer is None or self.daily_churn_hazard is None:
            return None
        hazard = self.daily_churn_hazard
        lifetime = cap_days if hazard <= 0 else min(1.0 / hazard, float(cap_days))
        return max(self.daily_contribution_per_customer, 0.0) * lifetime

    def version(self) -> str:
        """Content reference recorded on every decision (feature snapshot lineage)."""
        body = {
            "cutoff": self.cutoff.isoformat(),
            "plan": self.plan.version(),
            "unit_cost": sorted((list(k), v) for k, v in self.unit_cost.items()),
            "utilization": sorted(self.utilization.items()),
            "served_share": sorted((list(k), v) for k, v in self.served_share.items()),
            "rest": {
                k: v
                for k, v in asdict(self).items()
                if k
                in (
                    "payment_loss_rate",
                    "exposed_customers",
                    "daily_contribution_per_customer",
                    "daily_churn_hazard",
                )
            },
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

_EXPOSED_SQL = """
SELECT product, count(DISTINCT customer_id)
FROM marts.fct_usage_daily
WHERE event_date BETWEEN ? AND ?
GROUP BY product
"""

# Exact: integer numerators (value x hours - units x hourly cost sum) summed per `hours`, one
# division per group. A float sum would depend on DuckDB's parallel aggregation order and make
# rebuilt decisions differ in the last digit (found by the reproducibility check).
_CONTRIBUTION_SQL = """
SELECT c.hours,
       sum(u.usage_value_micros::HUGEINT * c.hours - u.units::HUGEINT * c.cost_micros_sum)
FROM marts.fct_usage_daily AS u
JOIN marts.fct_marginal_cost_daily AS c
  ON u.event_date = c.event_date AND u.region_id = c.region_id AND u.product = c.product
WHERE u.event_date BETWEEN ? AND ? AND c.event_date BETWEEN ? AND ?
GROUP BY c.hours ORDER BY c.hours
"""

_CUSTOMER_DAYS_SQL = """
SELECT count(DISTINCT (customer_id, event_date))
FROM marts.fct_usage_daily
WHERE event_date BETWEEN ? AND ?
"""

_CHURN_SQL = """
SELECT count(*) FROM marts.dim_customer
WHERE CAST(churned_at AS DATE) BETWEEN ? AND ?
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
    exposed = {p: int(n) for p, n in con.execute(_EXPOSED_SQL, cw).fetchall()}
    groups = con.execute(_CONTRIBUTION_SQL, cw + cw).fetchall()
    contrib = sum(int(total) / int(hours) for hours, total in groups) if groups else None
    customer_days = (con.execute(_CUSTOMER_DAYS_SQL, cw).fetchone() or (0,))[0]
    churned = (con.execute(_CHURN_SQL, cw).fetchone() or (0,))[0]
    return MarketSnapshot(
        cutoff=cutoff,
        plan=load_price_plan(con, until=cutoff),
        unit_cost=cost,
        utilization=util,
        served_share=served,
        payment_loss_rate=None if not invoiced else float(lost or 0.0) / float(invoiced),
        exposed_customers=exposed,
        daily_contribution_per_customer=None
        if not customer_days or contrib is None
        else float(contrib) / customer_days,
        daily_churn_hazard=None if not customer_days else float(churned) / customer_days,
    )
