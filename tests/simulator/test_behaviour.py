"""Behavioural sanity (required_test.md section 7), scored against ground truth.

Tolerances, fixed BEFORE running (1,000 customers, seed 42):
* Price intervention (+30% on api_requests, days 14-42, 50% treated by hashed assignment):
  difference-in-differences of per-customer log usage (treated minus control) must be
  negative and within 0.12 of the ground-truth prediction mean(elasticity_treated) * ln(1.3).
  Price-sensitive customers (elasticity < -1.5) must respond at least 0.10 more negatively
  than insensitive ones (elasticity > -0.8).
* First-attempt payment failure rate: observed vs sum(1 - reliability)/n within 4 sigma of a
  Poisson-binomial; least reliable half fails at least 2x as often as the most reliable half.
* Weekly seasonality: observed normalised day-of-week load profile within 0.08 (absolute, on
  a mean-1 profile) of the profile implied by ground-truth amplitudes and peaks.
* Capacity shock (x0.5 capacity): shocked-region mean utilisation >= 1.6x its unshocked level,
  mean latency >= 1.5x, mean error rate higher; unshocked regions' mean utilisation within
  +/-15% of their unshocked level.
* Demand spike (x1.8): spiked-region utilisation >= 1.5x; other regions within +/-15%.
* Money integrity: invoice amount == tier fee + round(sum(units * unit_price) / 10,000) exactly.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
import pytest

from praxis.simulator.config import TIERS
from praxis.simulator.validation import StreamValidator
from tests.simulator.sim_helpers import Collected, collect, scenario

ALL_TYPES = None  # keep everything


def day_of(event: dict[str, Any], start: date) -> int:
    return (date.fromisoformat(event["occurred_at"][:10]) - start).days


# ------------------------------------------------------------------ whole-stream checks
def test_default_run_passes_full_stream_validation_and_covers_all_event_types() -> None:
    cfg = scenario(n_customers=300, days=40)
    validator = StreamValidator(cfg.billing.max_attempts, schema_every=1)
    from praxis.events.payloads import PAYLOAD_MODELS
    from praxis.simulator.engine import Engine
    from praxis.simulator.population import generate_population

    pop = generate_population(cfg, 11)
    for event in Engine(cfg, 11, pop).run():
        validator.feed(event)
    assert set(validator.counts) == set(PAYLOAD_MODELS), "every contracted event type is emitted"
    assert validator.counts["churn.observed"] > 0
    assert validator.counts["payment.failed"] > 0


def test_events_expose_no_latent_parameters(base: Collected) -> None:
    banned = {
        "elasticity",
        "churn_sens",
        "service_sens",
        "pay_reliability",
        "growth",
        "base_load",
        "season_amp",
        "dispersion",
        "risky_payer",
        "noisy_volume",
        "compute_intensity",
    }
    for etype, events in base.events.items():
        sample = events[:: max(1, len(events) // 200)]
        for e in sample:
            assert banned.isdisjoint(e["payload"]), etype
            assert e["is_synthetic"] is True


def test_no_negative_or_impossible_usage(base: Collected) -> None:
    for e in base.events["usage.observed"]:
        p = e["payload"]
        assert p["units"] >= 1 and p["throttled_units"] >= 0 and p["unit_price_micros"] > 0
    for e in base.events["service.metric_observed"]:
        p = e["payload"]
        assert math.isfinite(p["utilization"]) and p["utilization"] >= 0
        assert 0 <= p["error_rate"] <= 1


def test_involuntary_churn_follows_a_final_failed_payment(base: Collected) -> None:
    final_failed = {e["event_id"] for e in base.events["payment.failed"] if e["payload"]["final"]}
    involuntary = [
        e for e in base.events["churn.observed"] if e["payload"]["reason"] == "involuntary_payment"
    ]
    assert all(e["causation_id"] in final_failed for e in involuntary)


# ---------------------------------------------------------------- price response -------
@pytest.fixture(scope="module")
def price_experiment() -> tuple[Collected, float]:
    mult = 1.3
    cfg = scenario(
        pricing={
            "interventions": [
                {
                    "id": "exp_api_up30",
                    "product": "api_requests",
                    "start_day": 14,
                    "end_day": 42,
                    "treated_fraction": 0.5,
                    "price_multiplier": mult,
                    "salt": "phase1-test",
                }
            ]
        },
    )
    return collect(cfg, keep={"usage.observed", "price.exposed", "churn.observed"}), mult


def test_controlled_price_increase_reduces_demand_with_known_magnitude(
    price_experiment: tuple[Collected, float],
) -> None:
    run, mult = price_experiment
    pop, cfg = run.population, run.config
    start = cfg.run.start_date
    api = [p.id for p in cfg.products].index("api_requests")
    units = np.zeros((pop.n, cfg.run.days))
    idx = {cid: i for i, cid in enumerate(pop.ids)}
    for e in run.events["usage.observed"]:
        if e["payload"]["product"] == "api_requests":
            units[idx[e["entity_id"]], day_of(e, start)] = e["payload"]["units"]
    churned = {idx[e["entity_id"]] for e in run.events["churn.observed"]}
    steady = np.array(
        [(not pop.is_new[i]) and i not in churned and pop.mix[i, api] > 0 for i in range(pop.n)]
    )
    pre = units[:, 0:14].mean(axis=1)
    during = units[:, 14:42].mean(axis=1)
    keep = steady & (pre >= 5)
    delta = np.log((during + 1) / (pre + 1))
    treated_ids = {
        e["entity_id"] for e in run.events["price.exposed"] if e["payload"]["arm"] == "treatment"
    }
    control_ids = {
        e["entity_id"] for e in run.events["price.exposed"] if e["payload"]["arm"] == "control"
    }
    treated = np.array([pop.ids[i] in treated_ids for i in range(pop.n)])
    control = np.array([pop.ids[i] in control_ids for i in range(pop.n)])
    assert not np.any(treated & control), "arms are mutually exclusive"
    t, c = keep & treated, keep & control
    assert t.sum() > 100 and c.sum() > 100
    did = float(delta[t].mean() - delta[c].mean())
    predicted = float(pop.elasticity[t].mean() * math.log(mult))
    assert did < 0, "price increase must reduce average demand"
    assert abs(did - predicted) <= 0.12, (did, predicted)

    sens = pop.elasticity < -1.5
    insens = pop.elasticity > -0.8
    effect_sens = delta[t & sens].mean() - delta[c & sens].mean()
    effect_insens = delta[t & insens].mean() - delta[c & insens].mean()
    assert effect_sens <= effect_insens - 0.10, (effect_sens, effect_insens)


def test_experiment_assignment_and_exposure_logging(
    price_experiment: tuple[Collected, float],
) -> None:
    run, mult = price_experiment
    cfg = run.config
    api_list = next(p.ref_price_micros for p in cfg.products if p.id == "api_requests")
    exposures = [e for e in run.events["price.exposed"] if e["payload"]["experiment_id"]]
    assert exposures, "experiment exposures are logged"
    for e in exposures:
        p = e["payload"]
        assert p["product"] == "api_requests" and p["experiment_id"] == "exp_api_up30"
        expected = round(api_list * (mult if p["arm"] == "treatment" else 1.0))
        assert p["unit_price_micros"] == expected
    per_customer: dict[str, set[str]] = defaultdict(set)
    for e in exposures:
        per_customer[e["entity_id"]].add(e["payload"]["arm"])
    assert all(len(arms) == 1 for arms in per_customer.values()), "no customer in both arms"
    arms = [next(iter(a)) for a in per_customer.values()]
    share = arms.count("treatment") / len(arms)
    assert abs(share - 0.5) <= 4 * math.sqrt(0.25 / len(arms))


# -------------------------------------------------------------- payment reliability ------
def test_lower_payment_reliability_means_more_failures(base: Collected) -> None:
    pop = base.population
    idx = {cid: i for i, cid in enumerate(pop.ids)}
    attempts = [e for e in base.events["payment.attempted"] if e["payload"]["attempt_number"] == 1]
    failed = {
        e["payload"]["invoice_id"]
        for e in base.events["payment.failed"]
        if e["payload"]["attempt_number"] == 1
    }
    rel = np.array([pop.pay_reliability[idx[e["entity_id"]]] for e in attempts])
    fail = np.array([e["payload"]["invoice_id"] in failed for e in attempts])
    n = len(attempts)
    assert n > 1000
    expected = float((1 - rel).sum() / n)
    sigma = math.sqrt(float((rel * (1 - rel)).sum())) / n
    assert abs(float(fail.mean()) - expected) <= 4 * sigma, (fail.mean(), expected, sigma)
    order = np.argsort(rel)
    half = n // 2
    low_rate, high_rate = fail[order[:half]].mean(), fail[order[half:]].mean()
    assert low_rate >= 2 * high_rate, (low_rate, high_rate)


# --------------------------------------------------------------------- seasonality ------
def test_known_weekly_seasonality_appears_in_aggregates(base: Collected) -> None:
    pop, cfg = base.population, base.config
    start = cfg.run.start_date
    weights = np.array([p.load_weight for p in cfg.products])
    prod_idx = {p.id: i for i, p in enumerate(cfg.products)}
    load = np.zeros(7)
    days_seen = np.zeros(7)
    for d in range(cfg.run.days):
        days_seen[(start.weekday() + d) % 7] += 1
    for e in base.events["usage.observed"]:
        p = e["payload"]
        load[(start.weekday() + day_of(e, start)) % 7] += (
            p["units"] * weights[prod_idx[p["product"]]]
        )
    observed = load / days_seen
    observed /= observed.mean()
    existing = ~pop.is_new
    expected = np.zeros(7)
    for dow in range(7):
        factor = 1 + pop.season_amp[existing] * np.cos(
            2 * np.pi * (dow - pop.season_peak[existing]) / 7
        )
        expected[dow] = float((pop.base_load[existing] * factor).sum())
    expected /= expected.mean()
    assert float(np.abs(observed - expected).max()) <= 0.08, (observed, expected)
    assert observed.max() / observed.min() > 1.15, "seasonality must be visible, not flat"


# ------------------------------------------------------------ capacity / spike / outage --
def _region_means(run: Collected, region: str, lo: int, hi: int) -> dict[str, float]:
    start = run.config.run.start_date
    rows = [
        e["payload"]
        for e in run.events["service.metric_observed"]
        if e["payload"]["region_id"] == region and lo <= day_of(e, start) < hi
    ]
    return {
        k: float(np.mean([r[k] for r in rows]))
        for k in ("utilization", "latency_p50_ms", "error_rate")
    }


def test_capacity_shock_affects_the_intended_variables() -> None:
    cfg = scenario(
        infrastructure={
            "capacity_shocks": [
                {"region": "us_east", "start_day": 20, "end_day": 32, "capacity_multiplier": 0.5}
            ]
        }
    )
    run = collect(cfg, keep={"service.metric_observed", "usage.observed"})
    shock = _region_means(run, "us_east", 20, 32)
    calm = _region_means(run, "us_east", 5, 18)
    assert shock["utilization"] >= 1.6 * calm["utilization"]
    assert shock["latency_p50_ms"] >= 1.5 * calm["latency_p50_ms"]
    assert shock["error_rate"] > calm["error_rate"]
    for other in ("eu_west", "us_west", "ap_southeast", "eu_central"):
        a, b = _region_means(run, other, 20, 32), _region_means(run, other, 5, 18)
        assert abs(a["utilization"] / b["utilization"] - 1) <= 0.15, other
    start = cfg.run.start_date
    thr = [
        e["payload"]
        for e in run.events["usage.observed"]
        if e["payload"]["region_id"] == "us_east" and 20 <= day_of(e, start) < 32
    ]
    thr_calm = [
        e["payload"]
        for e in run.events["usage.observed"]
        if e["payload"]["region_id"] == "us_east" and 5 <= day_of(e, start) < 18
    ]

    def throttle_share(rows: list[dict[str, Any]]) -> float:
        thr = sum(int(r["throttled_units"]) for r in rows)
        return thr / sum(int(r["units"]) + int(r["throttled_units"]) for r in rows)

    assert throttle_share(thr) > 0.10, "overload must throttle served demand"
    assert throttle_share(thr_calm) < 0.02, "no material throttling outside the shock"


def test_demand_spike_raises_load_only_where_scheduled() -> None:
    cfg = scenario(
        infrastructure={
            "demand_spikes": [
                {"region": "eu_west", "start_day": 20, "end_day": 27, "multiplier": 1.8}
            ]
        }
    )
    run = collect(cfg, keep={"service.metric_observed"})
    spike, calm = _region_means(run, "eu_west", 20, 27), _region_means(run, "eu_west", 6, 18)
    assert spike["utilization"] >= 1.5 * calm["utilization"]
    for other in ("us_east", "us_west", "ap_southeast", "eu_central"):
        a, b = _region_means(run, other, 20, 27), _region_means(run, other, 6, 18)
        assert abs(a["utilization"] / b["utilization"] - 1) <= 0.15, other


def test_product_outage_and_structural_unavailability() -> None:
    cfg = scenario(
        infrastructure={
            "product_outages": [
                {"region": "us_east", "product": "gpu_minutes", "start_day": 20, "end_day": 30}
            ]
        }
    )
    run = collect(cfg, keep={"usage.observed", "service.metric_observed"})
    start = cfg.run.start_date
    gpu_use = [e for e in run.events["usage.observed"] if e["payload"]["product"] == "gpu_minutes"]
    assert gpu_use
    assert not [
        e for e in gpu_use if e["payload"]["region_id"] == "us_east" and 20 <= day_of(e, start) < 30
    ], "no GPU served during the outage"
    assert [e for e in gpu_use if e["payload"]["region_id"] == "us_east" and day_of(e, start) < 20]
    assert not [e for e in gpu_use if e["payload"]["region_id"] == "ap_southeast"]
    marks = [
        e["payload"]
        for e in run.events["service.metric_observed"]
        if e["payload"]["region_id"] == "us_east" and 20 <= day_of(e, start) < 30
    ]
    assert all("gpu_minutes" not in m["available_products"] for m in marks)


def test_global_price_change_flows_to_unit_prices() -> None:
    cfg = scenario(
        days=20,
        pricing={"price_changes": [{"product": "api_requests", "day": 10, "multiplier": 1.25}]},
    )
    run = collect(cfg, keep={"usage.observed"})
    start = cfg.run.start_date
    base_price = next(p.ref_price_micros for p in cfg.products if p.id == "api_requests")
    for e in run.events["usage.observed"]:
        if e["payload"]["product"] == "api_requests":
            want = base_price if day_of(e, start) < 10 else round(base_price * 1.25)
            assert e["payload"]["unit_price_micros"] == want


# ----------------------------------------------------------------- money integrity ------
def test_invoice_amounts_equal_usage_times_price_exactly(base: Collected) -> None:
    cfg, start = base.config, base.config.run.start_date
    fee = {t.id: t.base_fee_minor for t in cfg.tiers}
    sub_day = {
        e["entity_id"]: day_of(e, start)
        for e in base.events["subscription.started"]
        if e["payload"]["origin"] == "new"
    }
    usage: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for e in base.events["usage.observed"]:
        if e["entity_id"] in sub_day:
            usage[e["entity_id"]].append(
                (day_of(e, start), e["payload"]["units"] * e["payload"]["unit_price_micros"])
            )
    first_invoice: dict[str, dict[str, Any]] = {}
    for e in base.events["invoice.created"]:
        if e["entity_id"] in sub_day and e["entity_id"] not in first_invoice:
            first_invoice[e["entity_id"]] = e
    assert len(first_invoice) >= 10
    for cid, inv in first_invoice.items():
        d_inv = day_of(inv, start)
        micros = sum(v for d, v in usage[cid] if sub_day[cid] <= d < d_inv)
        assert (
            inv["payload"]["amount_minor"]
            == (micros + 5000) // 10_000 + fee[inv["payload"]["tier"]]
        )
        assert all(isinstance(v, int) for _, v in usage[cid]), "money is integer"
    assert TIERS  # contracted tiers are importable
