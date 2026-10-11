"""Market snapshot and problem building: exact values, no future data, explicit failures."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from praxis.customer.artifact import ArtifactError as CustomerArtifactError
from praxis.customer.warehouse import WarehouseUnavailable as CustomerWarehouseUnavailable
from praxis.forecasting.artifact import ArtifactError
from praxis.forecasting.panel import SeriesKey
from praxis.forecasting.service import (
    ForecastPoint,
    ForecastResult,
    ForecastUnavailable,
    Freshness,
    Source,
)
from praxis.forecasting.warehouse import connect
from praxis.pricing.config import Mode, PricingPolicy
from praxis.pricing.evidence import PricingEvidence, ProductEvidence
from praxis.pricing.inputs import (
    CutoffFeatureSource,
    Forecaster,
    WarehouseProblemSource,
    decision_clock,
)
from praxis.pricing.problem import Blocked, PricingProblem, TierElasticity
from praxis.pricing.store import MemoryDecisionStore
from praxis.pricing.warehouse import load_market
from tests.customer import helpers as customer_helpers
from tests.pricing import market_db
from tests.pricing.helpers import policy

AS_OF = market_db.AS_OF
LEVELS = (0.1, 0.5, 0.9)


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    return market_db.build(tmp_path / "wh.duckdb")


def evidence() -> PricingEvidence:
    tiers = {"growth": TierElasticity(-1.2, 0.03), "enterprise": TierElasticity(-0.7, 0.03)}
    api = ProductEvidence(
        "api_requests", "px", date(2026, 2, 2), 28, 0.1823, 2.0, 0.5, 3.0, 1.0, 0.2,
        (("hierarchical", 2.0, 0.5), ("complete_pooling", 1.0, 0.3), ("no_pooling", 3.0, 1.0)),
    )  # fmt: skip
    return PricingEvidence(
        "elasticity-hier-test", "units-test", tiers, {"api_requests": api}, "churn-test"
    )


def forecast(
    freshness: Freshness = Freshness.FRESH, products: tuple[str, ...] = ("api_requests",)
) -> ForecastResult:
    points = [
        ForecastPoint(
            SeriesKey("eu_west", product, tier), h, AS_OF + timedelta(days=h - 1), base,
            {0.1: base * 0.8, 0.5: base, 0.9: base * 1.2},
        )
        for product in products
        for tier, base in (("growth", 100.0), ("enterprise", 50.0))
        for h in range(1, 8)
    ]  # fmt: skip
    return ForecastResult(
        model_name="demand_forecaster",
        model_version="demand-hybrid-test",
        feature_version="demand_features.v1",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        feature_date=AS_OF - timedelta(days=1),
        feature_cutoff=datetime(2026, 7, 20, tzinfo=UTC),
        feature_lag_days=1 if freshness is Freshness.FRESH else 3,
        freshness=freshness,
        source=Source.MODEL if freshness is Freshness.FRESH else Source.FALLBACK,
        fallback_reason=None if freshness is Freshness.FRESH else "stale_features",
        quantile_levels=LEVELS,
        points=points,
    )


class Stub:
    def __init__(self, result: ForecastResult | None = None, error: Exception | None = None):
        self.result, self.error = result, error

    def forecast(
        self, series: object = None, horizons: object = None, planned_prices: object = None
    ) -> ForecastResult:
        if self.error is not None:
            raise self.error
        assert horizons == list(range(1, 8)) and planned_prices is None  # current prices only
        assert self.result is not None
        return self.result


def stub(
    result: ForecastResult | None = None, error: Exception | None = None
) -> Callable[[date], Forecaster]:
    return lambda _as_of: Stub(result, error)


def fail(exc: Exception) -> Callable[[date], object]:
    def raise_(_as_of: date) -> object:
        raise exc

    return raise_


def two_products() -> PricingPolicy:
    return policy(
        products={
            "api_requests": {"floor_micros": 200_000, "ceiling_micros": 800_000},
            "gpu_minutes": {"floor_micros": 22_500, "ceiling_micros": 90_000},
        }
    )


def source(warehouse: Path, **kw: object) -> WarehouseProblemSource:
    fields: dict[str, object] = {
        "policy": two_products(),
        "warehouse": warehouse,
        "forecaster": stub(forecast()),
        "evidence": evidence(),
        "customers": lambda _as_of: customer_helpers.values(),
        "store": MemoryDecisionStore(),
    }
    fields.update(kw)
    return WarehouseProblemSource(**fields)  # type: ignore[arg-type]


# ----------------------------------------------------------------------- market
def test_market_snapshot_values_and_cutoff(warehouse: Path) -> None:
    con = connect(warehouse)
    m = load_market(con, AS_OF, two_products().valuation)
    con.close()
    assert m.cutoff == date(2026, 7, 19)
    # the poisoned future days (x10 cost, 0.99 utilisation, x10 usage) are never read
    assert m.unit_cost == {
        ("eu_west", "api_requests"): 120_000.0,
        ("eu_west", "gpu_minutes"): 30_000.0,
    }
    assert m.utilization == {"eu_west": pytest.approx(0.68)}
    assert m.served_share[("eu_west", "api_requests", "growth")] == pytest.approx(100 / 110)
    assert m.served_share[("eu_west", "api_requests", "enterprise")] == 1.0
    assert m.payment_loss_rate == pytest.approx(30_000 / 40_000)
    assert m.plan.price_on("api_requests", m.cutoff) == 368_000  # 2026-07-20's price unseen
    assert m.version() == m.version() and m.version().startswith("market-")


def test_empty_market_gives_unknowns(tmp_path: Path) -> None:
    path = market_db.build(tmp_path / "wh.duckdb")
    con = duckdb.connect(str(path))
    con.execute("DELETE FROM marts.fct_payments; DELETE FROM marts.fct_usage_daily")
    con.close()
    con = connect(path)
    m = load_market(con, AS_OF, two_products().valuation)
    con.close()
    assert m.payment_loss_rate is None


# --------------------------------------------------------------------- problems
def test_builds_a_problem_from_forecast_evidence_and_market(warehouse: Path) -> None:
    store = MemoryDecisionStore()
    out = source(warehouse, store=store).problems(AS_OF)
    api = out["api_requests"]
    assert isinstance(api, PricingProblem)
    assert api.current_price_micros == 368_000
    assert api.last_change == date(2026, 7, 2)
    assert api.anchor_price_micros == 400_000  # list price on the test's assignment date
    assert api.evidence_date == date(2026, 2, 2)
    seg = {s.tier: s for s in api.segments}
    assert seg["growth"].demand == pytest.approx(100.0) and seg[
        "growth"
    ].demand_high == pytest.approx(120.0)
    assert seg["growth"].served_share == pytest.approx(100 / 110)
    assert api.forecast_relative_width == pytest.approx(0.4)
    assert api.regions["eu_west"].unit_cost_micros == 120_000.0
    # Phase 9: the bill response applied to each customer's calibrated hazard and api share
    c = api.churn
    assert (c.response, c.response_sd) == (2.0, 0.5)  # pooled posterior, no calibration
    # robust class: the other posteriors ride along; guardrails take the most pessimistic bound
    assert c.alternatives == ((1.0, 0.3), (3.0, 1.0))
    assert c.response_upper == pytest.approx(3.0 + 1.6449 * 1.0)
    assert c.bill_share == 0.2
    churn_seg = {g.segment: g for g in c.segments}
    q = [1 - math.exp(-28 * h) for h in (0.002, 0.002, 0.0005, 0.003)]
    # Laspeyres exposure: api is at 368,000 vs 400,000 when tested (ratio 0.92)
    from praxis.pricing.inputs import reference_shares

    shares = reference_shares(
        customer_helpers.values(), {"api_requests": 0.92, "gpu_minutes": 1.0}, evidence().tiers
    )[:, 0]
    assert shares[0] < 0.75  # a cut product: today's spend overstates the tested basket
    growth = churn_seg["growth"]  # c1 and c2
    assert growth.exposed_customers == 2.0
    assert growth.window_churn == pytest.approx(q[0] + q[1])
    assert growth.churn_exposure == pytest.approx(shares[0] * q[0] + shares[1] * q[1])
    w0, w1 = shares[0] * q[0], shares[1] * q[1]
    weighted = (5e9 * w0 + 7e9 * w1) / (w0 + w1)
    assert growth.clv_micros == pytest.approx(weighted)  # the CLV of whom the price would lose
    assert churn_seg["enterprise"].churn_exposure == pytest.approx(q[2])
    assert churn_seg["starter"].exposed_customers == 0.0  # c4 never used api_requests
    # customers by price-change dates in the last 90 days: c1 2, c2 0 (200 days ago), c3 3
    assert api.price_change_counts == {0: 1, 2: 1, 3: 1}
    assert set(api.lineage) >= {
        "forecast_model_version",
        "elasticity_model_version",
        "market_snapshot",
        "customer_model_version",
        "customer_snapshot",
        "churn_response_version",
    }
    assert api.lineage["forecast_freshness"] == "fresh"
    # gpu has a price but no forecast series in this stub
    gpu = out["gpu_minutes"]
    assert isinstance(gpu, Blocked) and gpu.reason == "forecast_missing"


def test_executed_changes_count_for_the_cooldown(warehouse: Path) -> None:
    from tests.pricing.test_store import change

    store = MemoryDecisionStore()
    d = change(Mode.EXECUTE)
    store.record(d)
    done = store.execute(d.decision_id, "pricing-service")
    api = source(warehouse, store=store).problems(AS_OF)["api_requests"]
    assert isinstance(api, PricingProblem)
    assert api.last_change == max(date(2026, 7, 2), done.executed_at.date())


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"forecaster": stub(forecast(Freshness.STALE))}, "forecast_stale"),
        (
            {"forecaster": stub(error=ForecastUnavailable("features_unavailable", "old"))},
            "forecast_unavailable",
        ),
        ({"forecaster": stub(error=ArtifactError("gone"))}, "model_unavailable"),
        ({"evidence": None}, "model_unavailable"),
        ({"customers": None}, "model_unavailable"),
        ({"customers": fail(CustomerArtifactError("checksum"))}, "model_unavailable"),
        ({"customers": fail(CustomerWarehouseUnavailable("no marts"))}, "input_unavailable"),
    ],
)  # fmt: skip
def test_unavailable_inputs_block_every_product(
    warehouse: Path, kwargs: dict[str, object], reason: str
) -> None:
    out = source(warehouse, **kwargs).problems(AS_OF)
    assert all(isinstance(b, Blocked) and b.reason == reason for b in out.values())


def test_missing_warehouse_blocks_every_product(tmp_path: Path) -> None:
    out = source(tmp_path / "missing.duckdb").problems(AS_OF)
    assert all(isinstance(b, Blocked) and b.reason == "input_unavailable" for b in out.values())


def test_missing_cost_or_price_is_explicit(tmp_path: Path) -> None:
    out = source(market_db.build(tmp_path / "a.duckdb", with_cost=False)).problems(AS_OF)
    assert (
        isinstance(out["api_requests"], Blocked)
        and out["api_requests"].reason == "cost_unavailable"
    )
    both = stub(forecast(products=("api_requests", "gpu_minutes")))
    out = source(market_db.build(tmp_path / "b.duckdb", gpu_price=False), forecaster=both).problems(
        AS_OF
    )
    assert (
        isinstance(out["gpu_minutes"], Blocked) and out["gpu_minutes"].reason == "price_unavailable"
    )


def test_untested_products_have_no_anchor(warehouse: Path) -> None:
    both = stub(forecast(products=("api_requests", "gpu_minutes")))
    gpu = source(warehouse, forecaster=both).problems(AS_OF)["gpu_minutes"]
    assert isinstance(gpu, PricingProblem)
    assert gpu.anchor_price_micros is None and gpu.evidence_date is None
    assert gpu.churn.response == 0.0 and gpu.last_change is None


def test_cutoff_source_and_decision_clock() -> None:
    seen: list[date] = []

    class Inner:
        def snapshot(self, series: object, *, not_after: date) -> None:
            seen.append(not_after)

    src = CutoffFeatureSource(Inner(), date(2026, 7, 19))
    src.snapshot([], not_after=date(2026, 7, 25))
    src.snapshot([], not_after=date(2026, 7, 10))
    assert seen == [date(2026, 7, 19), date(2026, 7, 10)]
    assert decision_clock(AS_OF)().date() == AS_OF


def test_reference_shares_value_the_tested_basket_at_todays_prices() -> None:
    """Laspeyres exposure: a raised product's share is restored to the tested basket's."""
    from praxis.pricing.inputs import reference_shares

    values = customer_helpers.values()
    tiers = {t: TierElasticity(-1.0, 0.0) for t in ("starter", "growth", "enterprise")}
    same = reference_shares(values, {"api_requests": 1.0, "gpu_minutes": 1.0}, tiers)
    assert same == pytest.approx(values.snapshot.shares())
    raised = reference_shares(values, {"api_requests": 1.1, "gpu_minutes": 1.0}, tiers)
    # c1: api 30 / gpu 10 at today's prices; with e = -1 the tested basket costs 30 x 1.1 today
    assert raised[0, 0] == pytest.approx(33.0 / 43.0)
    assert raised[3].tolist() == [0.0, 1.0]  # c4 never used api


def test_book_elasticity_adds_the_books_concentration_to_the_posterior() -> None:
    from dataclasses import replace

    from praxis.pricing.inputs import book_elasticity

    ev = replace(evidence(), heterogeneity={"growth": 0.25, "enterprise": 0.0})
    values = customer_helpers.values()
    out = book_elasticity("api_requests", values, ev)
    # growth api spend: c1 30, c2 20 -> weights 0.6 / 0.4, sum w^2 = 0.52
    eta = math.sqrt(0.25 * 0.52)
    assert out["growth"].mean == -1.2
    assert out["growth"].sd == pytest.approx(math.hypot(0.03, eta))
    assert out["enterprise"].sd == pytest.approx(0.03)  # no heterogeneity measured
    assert book_elasticity("other_product", values, ev)["growth"].sd == pytest.approx(0.03)
