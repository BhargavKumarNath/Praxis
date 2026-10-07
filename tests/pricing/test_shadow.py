"""Shadow evaluation building blocks (praxis.science.pricing_shadow), without a full world."""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np
import pytest

from praxis.pricing.decision import Status
from praxis.pricing.optimiser import optimise
from praxis.science.pricing_shadow import (
    Results,
    Scored,
    Shadow,
    acceptance_checks,
    captured_share,
    collect_truth,
    compliance,
    load_shadow_config,
    naive_price,
    problem_from_record,
    record_complete,
    reported_metrics,
)
from praxis.simulator.config import load_config
from tests.pricing.helpers import open_policy, policy, problem, single_segment


def change_record() -> dict[str, Any]:
    d = optimise(problem(), policy())
    assert d.status is Status.CHANGE
    return d.record()


def test_a_record_reproduces_its_decision() -> None:
    """Audit reproducibility: re-optimising the recorded inputs gives the same decision."""
    d = optimise(problem(), policy())
    again = optimise(problem_from_record(d.record()), policy())
    assert again.record_sha256 == d.record_sha256


def test_compliance_accepts_the_optimisers_changes() -> None:
    assert compliance(change_record(), policy()) == []
    hold = optimise(single_segment(-2.0, 100_000.0, 200_000), open_policy()).record()
    assert compliance(hold, open_policy()) == []


@pytest.mark.parametrize(
    ("edit", "violation"),
    [
        (
            lambda r: r.update(chosen_price_micros=r["constraints"]["price_ceiling_micros"] + 100),
            "price bounds",
        ),
        (lambda r: r.update(chosen_price_micros=int(r["current_price_micros"] * 1.2)), "max step"),
        (lambda r: r["constraints"].update(in_cooldown=True), "cooldown"),
        (lambda r: r["inputs"].update(anchor_price_micros=100_000), "extrapolation"),
        (lambda r: r["guardrails"].update(projected_incremental_churn=0.5), "churn guardrail"),
        (lambda r: r["guardrails"].update(min_net_margin=0.01), "margin floor"),
        (lambda r: r["guardrails"]["checks"].update(capacity=False), "guardrail outcome"),
        (lambda r: r.update(candidates=[]), "chosen candidate infeasible"),
    ],
)  # fmt: skip
def test_compliance_catches_every_doctored_record(edit: Any, violation: str) -> None:
    rec = copy.deepcopy(change_record())
    edit(rec)
    assert violation in compliance(rec, policy())


def test_record_completeness() -> None:
    rec = change_record()
    assert record_complete(rec) == []
    broken = copy.deepcopy(rec)
    del broken["guardrails"]
    broken["lineage"].pop("market_snapshot", None)
    broken["reason_codes"] = []
    broken["chosen_price_micros"] = None
    missing = record_complete(broken)
    assert {"guardrails", "reason_codes (empty)", "chosen_price_micros"} <= set(missing)
    unavailable = optimise(problem(current_price_micros=0), policy()).record()
    assert record_complete(unavailable) == []
    unavailable["errors"] = []
    assert record_complete(unavailable) == ["errors (empty)"]


def test_naive_baseline_is_the_unconstrained_lerner_price() -> None:
    prob = single_segment(-2.0, 100_000.0, 180_000)
    assert naive_price(prob, 1) == pytest.approx(200_000, rel=5e-3)


def _scored(rec: dict[str, Any], delta: float, contrib: float, churn: float) -> Scored:
    prices = [c["price_micros"] for c in rec["candidates"]]
    chosen = rec["chosen_price_micros"] or rec["current_price_micros"]
    true_delta = {p: (delta if p == chosen else 0.0) for p in prices}
    return Scored(
        rec,
        true_delta,
        {p: (contrib if p == chosen else 0.0) for p in prices},
        {p: (churn if p == chosen else 0.0) for p in prices},
        None,
    )


def test_acceptance_on_constructed_results() -> None:
    d = optimise(problem(), policy())
    rec = d.record()
    pred = rec["prediction"]["delta_contribution_mean"]
    acc = load_shadow_config().acceptance.model_copy(update={"min_changes_for_calibration": 1})
    stress_ok = [{"case": "x", "passed": True}]
    good = Results([d], [_scored(rec, pred, pred, 0.001)], 0, stress_ok, "p")
    checks = {c["name"]: c for c in acceptance_checks(good, policy(), acc)}
    assert all(c["passed"] for c in checks.values()), checks
    assert reported_metrics(good)["change_share"] == 1.0
    bad = Results(
        [d], [_scored(rec, -1.0, pred * 5, 0.5)], 1, [{"case": "x", "passed": False}], "p"
    )
    failed = {c["name"] for c in acceptance_checks(bad, policy(), acc) if not c["passed"]}
    assert failed == {
        "no_shadow_execution",
        "no_harm",
        "direction",
        "prediction_calibration",
        "churn_guardrail_truth",
        "stress_fail_safe",
    }


def test_holding_everything_is_safe_but_reported() -> None:
    """No change: the movement checks are vacuous, the report shows what was missed."""
    d = optimise(single_segment(-2.0, 100_000.0, 200_000), open_policy())
    assert d.status is Status.HOLD
    rec = d.record()
    prices = [c["price_micros"] for c in rec["candidates"]]
    scored = Scored(rec, dict.fromkeys(prices, 3.0) | {200_000: 0.0}, {}, {}, None)
    res = Results([d], [scored], 0, [], "p")
    acc = load_shadow_config().acceptance
    assert all(c["passed"] for c in acceptance_checks(res, open_policy(), acc))
    rep = reported_metrics(res)
    assert rep["change_share"] == 0.0 and rep["captured_share"] == 0.0
    assert rep["missed_true_delta_per_day"] == 3.0


def test_captured_share_definition() -> None:
    assert captured_share(5.0, 10.0) == 0.5
    assert captured_share(0.0, 0.0) == 1.0
    assert captured_share(-1.0, 0.0) == 0.0


def test_truth_collector_scores_candidate_prices_with_the_engines_equations() -> None:
    world = load_config().with_overrides(n_customers=200, days=14)
    ref = world.products[0].ref_price_micros
    prices = [int(ref / 1.05), ref, int(ref * 1.05)]
    (acc,) = collect_truth(
        world,
        3,
        [(7, world.products[0].id, prices, 0.0)],
        Shadow(first_cycle_day=28, cycles=1, cycle_days=7),
    )
    assert acc.days == 7 and acc.exposed > 0
    contrib = acc.contribution / acc.days
    # demand falls with price; churn rises with price; the status quo has no extra churn
    assert acc.extra_churn[1] == 0.0
    assert acc.extra_churn[2] > 0 > acc.extra_churn[0]
    assert np.all(np.isfinite(contrib)) and not math.isclose(contrib[0], contrib[2])
