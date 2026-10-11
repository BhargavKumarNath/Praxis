"""Point-in-time snapshots and labels from the marts: exact values, no future data."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from praxis.customer.config import load_config
from praxis.customer.features import FEATURES, feature_matrix
from praxis.customer.warehouse import (
    Snapshot,
    WarehouseUnavailable,
    connect,
    load_outcomes,
    load_snapshot,
)
from tests.customer import marts_db
from tests.customer.marts_db import API_COST, API_PRICE, AS_OF

CFG = load_config()


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    return marts_db.build(tmp_path / "wh.duckdb")


def test_snapshot_holds_subscribers_active_at_the_date(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features)
    con.close()
    # c3 churned before, c6 never subscribed, c7 arrives later
    assert s.ids == ("c1", "c2", "c4", "c5")
    # c2: enterprise from 2026-06-10; its 2026-07-25 change back is the future
    assert s.tier == ("growth", "enterprise", "growth", "starter")
    assert s.fee_minor.tolist() == [9900.0, 99900.0, 9900.0, 900.0]
    # tenure at AS_OF: tenure at the first event + days since
    assert s.tenure_days[0] == 100 + (AS_OF - date(2026, 1, 1)).days
    assert s.tenure_days[3] == (AS_OF - date(2026, 3, 1)).days
    assert s.products == ("api_requests", "gpu_minutes")


def test_snapshot_values_are_exact_and_never_read_the_future(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features)
    con.close()
    # c1: 10 units/day, doubled over the last 14 days of the 28-day spend window
    units = 14 * 10 + 14 * 20
    assert s.value_micros[0].tolist() == [units * API_PRICE, 0.0]
    assert s.cost_micros[0, 0] == units * API_COST
    assert s.recent_value_micros[0] == 14 * 20 * API_PRICE
    assert s.prior_value_micros[0] == 14 * 10 * API_PRICE
    # shares: c2 uses api 100 x 400k and gpu 4.5M per day
    assert s.shares()[1] == pytest.approx([40e6 / 44.5e6, 4.5e6 / 44.5e6])
    assert s.shares()[0].tolist() == [1.0, 0.0]
    # failures: two resolved before AS_OF; the one resolved after and the future one are unseen
    assert s.failed_attempts.tolist() == [2.0, 0.0, 0.0, 0.0]
    # service in the 7 days before AS_OF: mean of 0.001 / 0.002 hourly error rates
    assert s.region_error_rate[0] == pytest.approx(0.0015)
    assert s.region_latency_p95_ms[0] == pytest.approx(60.0)
    # payment loss over the failure window: i1 never final, i2 final but resolved later (by
    # event date it is in the window: loss is an invoice-level share of the window's invoices)
    assert 0.0 <= s.payment_loss_rate < 1.0
    # daily margin = (value - cost) / 28 + fee accrued per day
    fee = 9900 * 10_000 / 30
    assert s.daily_margin_micros()[0] == pytest.approx(units * (API_PRICE - API_COST) / 28 + fee)


def test_price_change_history_counts_distinct_dates_in_the_window(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features, price_history_days=365)
    con.close()
    assert s.price_change_dates["c1"] == (date(2026, 4, 1), date(2026, 6, 1), date(2026, 7, 1))
    # 90 days: 2026-04-01 is too old; two products on 2026-07-01 are one change date
    assert s.price_changes_within(90).tolist() == [2, 1, 0, 0]
    assert s.price_changes_within(365).tolist() == [3, 1, 0, 0]
    assert s.version() == s.version() and s.version().startswith("customers-")


def test_labels_define_voluntary_churn_in_the_window(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features)
    out = load_outcomes(con, s.ids, AS_OF, 28)
    con.close()
    assert out.voluntary.tolist() == [0.0, 0.0, 1.0, 0.0]  # c4
    assert out.involuntary.tolist() == [0.0, 0.0, 0.0, 1.0]  # c5 (competing risk)
    # days at risk end with any churn; the churn day itself is at risk
    assert out.exposure_days.tolist() == [28.0, 28.0, 11.0, 17.0]


def test_a_short_window_censors_later_churn(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features)
    out = load_outcomes(con, s.ids, AS_OF, 7)
    con.close()
    assert out.voluntary.sum() == 0 and out.involuntary.sum() == 0
    assert out.exposure_days.tolist() == [7.0] * 4


def test_features_ignore_the_poisoned_future(warehouse: Path, tmp_path: Path) -> None:
    """Leakage: deleting every row on or after AS_OF changes no feature."""
    con = connect(warehouse)
    before = feature_matrix(load_snapshot(con, AS_OF, CFG.features), 14)
    con.close()
    clean = marts_db.build(tmp_path / "clean.duckdb")
    con = duckdb.connect(str(clean))
    con.execute("SET TimeZone = 'UTC'")
    for table, col in (
        ("fct_usage_daily", "event_date"),
        ("fct_marginal_cost_daily", "event_date"),
        ("fct_payments", "event_date"),
        ("fct_service_metrics_hourly", "event_date"),
        ("fct_customer_price_changes", "event_date"),
        ("fct_subscriptions", "event_date"),
    ):
        con.execute(f"DELETE FROM marts.{table} WHERE {col} >= ?", [AS_OF])  # noqa: S608
    con.execute("UPDATE marts.dim_customer SET churned_at = NULL WHERE churned_at >= ?", [AS_OF])
    con.close()
    con = connect(clean)
    after = feature_matrix(load_snapshot(con, AS_OF, CFG.features), 14)
    con.close()
    assert np.array_equal(before, after)


def test_feature_values(warehouse: Path) -> None:
    con = connect(warehouse)
    s = load_snapshot(con, AS_OF, CFG.features)
    con.close()
    x = feature_matrix(s, 14)
    col = {name: x[:, i] for i, name in enumerate(FEATURES)}
    assert col["tier_growth"].tolist() == [1.0, 0.0, 1.0, 0.0]
    assert col["tier_enterprise"].tolist() == [0.0, 1.0, 0.0, 0.0]
    assert col["log_failed_attempts"][0] == pytest.approx(np.log(3.0))
    # c1 doubled its usage: log((20 x 0.4 + 1) / (10 x 0.4 + 1)) in GBP per day
    assert col["usage_trend"][0] == pytest.approx(np.log(9.0 / 5.0))
    assert col["log_region_error_rate"][0] == pytest.approx(np.log(0.0015))
    assert feature_matrix(load_empty(s), 14).shape == (0, len(FEATURES))


def load_empty(s: Snapshot) -> Snapshot:
    return replace(s, ids=(), tier=())


def test_exact_sums_whatever_the_row_order(tmp_path: Path) -> None:
    """Regression (Phase 6): a float SUM depended on aggregation order and changed versions."""
    path = marts_db.build(tmp_path / "wh.duckdb")
    con = duckdb.connect(str(path))
    con.execute("DELETE FROM marts.fct_usage_daily WHERE customer_id = 'c1'")
    for k, value in enumerate([2**53, 1, 1]):
        con.execute(
            "INSERT INTO marts.fct_usage_daily "
            "VALUES (?, 'c1', 'api_requests', 'eu_west', 0, 0, ?, 1)",
            [AS_OF - timedelta(days=1 + k), value],
        )
    con.close()
    con = connect(path)
    s = load_snapshot(con, AS_OF, CFG.features)
    con.close()
    assert int(s.value_micros[0, 0]) == 2**53 + 2  # float: (2^53 + 1) + 1 == 2^53


def test_missing_warehouse_or_marts_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(WarehouseUnavailable):
        connect(tmp_path / "missing.duckdb")
    duckdb.connect(str(tmp_path / "empty.duckdb")).close()
    con = connect(tmp_path / "empty.duckdb")
    with pytest.raises(WarehouseUnavailable, match="marts"):
        load_snapshot(con, AS_OF, CFG.features)
    con.close()
