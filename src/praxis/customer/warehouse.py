"""Point-in-time customer extracts from the dbt marts (DuckDB warehouse).

A snapshot ``as_of`` T is what the business knew at the start of day T: every query reads
``marts.*`` only, bounded by the partition column, and uses rows strictly BEFORE T. A customer
is in the snapshot when subscribed before T and not churned before T. The tier and fee are the
ones in force at T (``fct_subscriptions``); ``dim_customer.current_tier`` (the tier at the END
of the data) is never read. Labels (``load_outcomes``) are the only reads at or after T, and
they live in a separate function so a feature path can never reach them.

Money stays exact: usage value and marginal cost are integer sums per (customer, product,
hours) group with one division each (float aggregation order would make snapshots differ in
the last digit between runs, as found in Phase 6).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
from numpy.typing import NDArray

from praxis.customer.config import Features

F64 = NDArray[np.float64]


class WarehouseUnavailable(RuntimeError):
    """The warehouse file or the marts it needs are missing."""


@dataclass(frozen=True)
class Snapshot:
    """Every customer active at ``as_of``, with raw observable history (columnar, sorted ids)."""

    as_of: date
    products: tuple[str, ...]
    ids: tuple[str, ...]
    region: tuple[str, ...]
    industry: tuple[str, ...]
    tier: tuple[str, ...]  # tier in force at as_of
    fee_minor: F64  # monthly subscription fee in force at as_of (pence)
    tenure_days: F64  # days since the customer's relationship began, at as_of
    value_micros: F64  # (n, P) usage value over the spend window
    cost_micros: F64  # (n, P) marginal cost of that usage
    recent_value_micros: F64  # usage value over the last trend_half_days
    prior_value_micros: F64  # usage value over the trend_half_days before those
    failed_attempts: F64  # failed payment attempts in the failure window
    region_error_rate: F64  # mean hourly error rate of the customer's region (service window)
    region_latency_p95_ms: F64  # mean hourly p95 latency of the customer's region
    payment_loss_rate: float  # share of invoiced value finally uncollected (failure window)
    spend_window_days: int
    price_change_dates: dict[str, tuple[date, ...]] = field(default_factory=dict)
    initial_tier: tuple[str, ...] = ()  # tier at signup (static; the Phase 5 elasticity tiers)

    @property
    def n(self) -> int:
        return len(self.ids)

    def version(self) -> str:
        """Content reference recorded on decisions (feature snapshot lineage)."""
        h = hashlib.sha256(self.as_of.isoformat().encode())
        for part in (
            self.products,
            self.ids,
            self.region,
            self.industry,
            self.tier,
            self.initial_tier,
        ):
            h.update("\n".join(part).encode())
        for arr in (
            self.fee_minor,
            self.tenure_days,
            self.value_micros,
            self.cost_micros,
            self.recent_value_micros,
            self.prior_value_micros,
            self.failed_attempts,
            self.region_error_rate,
            self.region_latency_p95_ms,
        ):
            h.update(np.ascontiguousarray(arr).tobytes())
        h.update(repr(self.payment_loss_rate).encode())
        for c, days in self.price_change_dates.items():
            h.update(f"{c}:{','.join(d.isoformat() for d in days)}".encode())
        return "customers-" + h.hexdigest()[:16]

    def shares(self) -> F64:
        """(n, P) share of each product in the customer's usage value (0 rows stay 0)."""
        total = self.value_micros.sum(axis=1, keepdims=True)
        out: F64 = np.divide(
            self.value_micros, total, out=np.zeros_like(self.value_micros), where=total > 0
        )
        return out

    def daily_margin_micros(self) -> F64:
        """Daily contribution: usage value - cost + accrued fee, before payment loss."""
        usage = (self.value_micros - self.cost_micros).sum(axis=1) / self.spend_window_days
        fee = self.fee_minor * 10_000.0 / 30.0  # pence -> micro-GBP, monthly -> daily
        out: F64 = usage + fee
        return out

    def price_changes_within(self, days: int) -> NDArray[np.int64]:
        """Distinct dates with a price change per customer in [as_of - days, as_of)."""
        first = self.as_of - timedelta(days=days)
        return np.array(
            [
                sum(1 for d in self.price_change_dates.get(c, ()) if first <= d < self.as_of)
                for c in self.ids
            ],
            dtype=np.int64,
        )


@dataclass(frozen=True)
class Outcomes:
    """Voluntary churn within the label window after a snapshot (training / evaluation only)."""

    exposure_days: F64  # days at risk in [as_of, as_of + window): ends at any churn
    voluntary: F64  # 1 if the customer churned voluntarily within the window
    involuntary: F64  # 1 if the customer churned for non-payment within the window


_ACTIVE_SQL = """
SELECT c.customer_id, c.region_id, c.industry, c.is_existing, c.tenure_days_at_start,
       CAST(c.created_at AS DATE), c.initial_tier
FROM marts.dim_customer AS c
WHERE c.created_at < ? AND (c.churned_at IS NULL OR c.churned_at >= ?)
ORDER BY c.customer_id
"""

_SUBSCRIPTION_SQL = """
SELECT customer_id, tier, base_fee_minor FROM (
    SELECT customer_id, tier, base_fee_minor,
           row_number() OVER (PARTITION BY customer_id ORDER BY effective_at DESC, event_id DESC)
             AS rn
    FROM marts.fct_subscriptions
    WHERE event_date BETWEEN ? AND ? AND effective_at < ?
) WHERE rn = 1
"""

_USAGE_SQL = """
SELECT u.customer_id, u.product, c.hours,
       sum(u.usage_value_micros)::HUGEINT,
       sum(u.units::HUGEINT * c.cost_micros_sum)
FROM marts.fct_usage_daily AS u
JOIN marts.fct_marginal_cost_daily AS c
  ON u.event_date = c.event_date AND u.region_id = c.region_id AND u.product = c.product
WHERE u.event_date BETWEEN ? AND ? AND c.event_date BETWEEN ? AND ?
GROUP BY ALL
"""

_TREND_SQL = """
SELECT customer_id,
       sum(usage_value_micros) FILTER (WHERE event_date >= ?)::HUGEINT,
       sum(usage_value_micros) FILTER (WHERE event_date < ?)::HUGEINT
FROM marts.fct_usage_daily
WHERE event_date BETWEEN ? AND ?
GROUP BY customer_id
"""

_FAILURES_SQL = """
SELECT customer_id, count(*)
FROM marts.fct_payments
WHERE event_date BETWEEN ? AND ? AND outcome = 'failed' AND resolved_at < ?
GROUP BY customer_id
"""

_LOSS_SQL = """
WITH invoices AS (
    SELECT invoice_id, max(CASE WHEN attempt_number = 1 THEN amount_minor END) AS amount,
           bool_or(is_final_failure) AS lost
    FROM marts.fct_payments
    WHERE event_date BETWEEN ? AND ?
    GROUP BY invoice_id
)
SELECT sum(amount)::HUGEINT, sum(CASE WHEN lost THEN amount ELSE 0 END)::HUGEINT
FROM invoices WHERE amount IS NOT NULL
"""

_SERVICE_SQL = """
SELECT region_id, avg(error_rate::DECIMAL(38, 12))::DOUBLE,
       avg(latency_p95_ms::DECIMAL(38, 6))::DOUBLE
FROM marts.fct_service_metrics_hourly
WHERE event_date BETWEEN ? AND ?
GROUP BY region_id
"""

_PRICE_CHANGES_SQL = """
SELECT DISTINCT customer_id, event_date
FROM marts.fct_customer_price_changes
WHERE event_date BETWEEN ? AND ?
ORDER BY customer_id, event_date
"""

_OUTCOME_SQL = """
SELECT customer_id, CAST(churned_at AS DATE), churn_reason
FROM marts.dim_customer
WHERE churned_at >= ? AND churned_at < ?
"""

EARLIEST = date(1970, 1, 1)


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    if not path.exists():
        raise WarehouseUnavailable(f"warehouse not found: {path}")
    con = duckdb.connect(str(path), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def load_snapshot(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    cfg: Features,
    *,
    price_history_days: int = 365,
) -> Snapshot:
    """Everything known at the start of ``as_of`` about the customers active then."""
    try:
        return _snapshot(con, as_of, cfg, price_history_days)
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable(f"customer marts missing (run dbt): {exc}") from exc


def _window(as_of: date, days: int) -> list[date]:
    return [as_of - timedelta(days=days), as_of - timedelta(days=1)]


def _snapshot(
    con: duckdb.DuckDBPyConnection, as_of: date, cfg: Features, price_history_days: int
) -> Snapshot:
    subs = {
        c: (t, int(f))
        for c, t, f in con.execute(
            _SUBSCRIPTION_SQL, [EARLIEST, as_of - timedelta(days=1), as_of]
        ).fetchall()
    }
    # customers = subscribers: a prospect that never converted has no subscription and is not
    # a customer who can churn
    rows = [r for r in con.execute(_ACTIVE_SQL, [as_of, as_of]).fetchall() if r[0] in subs]
    ids = tuple(r[0] for r in rows)
    index = {c: i for i, c in enumerate(ids)}
    n = len(ids)
    spend = _window(as_of, cfg.spend_window_days)
    usage = con.execute(_USAGE_SQL, [*spend, *spend]).fetchall()
    products = tuple(sorted({r[1] for r in usage}))
    value = np.zeros((n, len(products)))
    cost = np.zeros((n, len(products)))
    for cid, product, hours, v, c in usage:
        i = index.get(cid)
        if i is not None:
            value[i, products.index(product)] += float(int(v))
            cost[i, products.index(product)] += int(c) / int(hours)
    recent, prior = _trend(con, as_of, cfg.trend_half_days, index)
    fail_window = _window(as_of, cfg.failure_window_days)
    failures = np.zeros(n)
    for cid, count in con.execute(_FAILURES_SQL, [*fail_window, as_of]).fetchall():
        if cid in index:
            failures[index[cid]] = float(count)
    invoiced, lost = con.execute(_LOSS_SQL, fail_window).fetchone() or (None, None)
    service = {
        r: (float(e), float(p95))
        for r, e, p95 in con.execute(
            _SERVICE_SQL, _window(as_of, cfg.service_window_days)
        ).fetchall()
    }
    changes: dict[str, list[date]] = defaultdict(list)
    for cid, day in con.execute(_PRICE_CHANGES_SQL, _window(as_of, price_history_days)).fetchall():
        if cid in index:
            changes[cid].append(day)
    region = tuple(r[1] for r in rows)
    return Snapshot(
        as_of=as_of,
        products=products,
        ids=ids,
        region=region,
        industry=tuple(r[2] for r in rows),
        tier=tuple(subs[c][0] for c in ids),
        fee_minor=np.array([float(subs[c][1]) for c in ids]),
        tenure_days=np.array(
            [float((r[4] or 0) + max((as_of - r[5]).days, 0)) for r in rows], dtype=np.float64
        ),
        value_micros=value,
        cost_micros=cost,
        recent_value_micros=recent,
        prior_value_micros=prior,
        failed_attempts=failures,
        region_error_rate=np.array([service.get(r, (np.nan, np.nan))[0] for r in region]),
        region_latency_p95_ms=np.array([service.get(r, (np.nan, np.nan))[1] for r in region]),
        payment_loss_rate=float(int(lost or 0)) / float(int(invoiced)) if invoiced else 0.0,
        spend_window_days=cfg.spend_window_days,
        price_change_dates={c: tuple(d) for c, d in sorted(changes.items())},
        initial_tier=tuple(r[6] for r in rows),
    )


def _trend(
    con: duckdb.DuckDBPyConnection, as_of: date, half: int, index: dict[str, int]
) -> tuple[F64, F64]:
    split = as_of - timedelta(days=half)
    recent, prior = np.zeros(len(index)), np.zeros(len(index))
    window = _window(as_of, 2 * half)
    for cid, r, p in con.execute(_TREND_SQL, [split, split, *window]).fetchall():
        i = index.get(cid)
        if i is not None:
            recent[i] = float(int(r or 0))
            prior[i] = float(int(p or 0))
    return recent, prior


def load_outcomes(
    con: duckdb.DuckDBPyConnection, ids: tuple[str, ...], as_of: date, window_days: int
) -> Outcomes:
    """Churn of the snapshot's customers in [as_of, as_of + window_days). Labels only."""
    end = as_of + timedelta(days=window_days)
    churn = {c: (d, reason) for c, d, reason in con.execute(_OUTCOME_SQL, [as_of, end]).fetchall()}
    exposure = np.full(len(ids), float(window_days))
    vol, inv = np.zeros(len(ids)), np.zeros(len(ids))
    for i, c in enumerate(ids):
        if c in churn:
            day, reason = churn[c]
            exposure[i] = float((day - as_of).days + 1)
            if reason == "voluntary":
                vol[i] = 1.0
            else:
                inv[i] = 1.0
    return Outcomes(exposure, vol, inv)
