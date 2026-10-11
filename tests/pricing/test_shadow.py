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
        # Phase 9 guardrails are re-derived from the record's INPUTS, not its guardrail block
        (lambda r: r["inputs"]["churn"]["segments"][0].update(churn_exposure=600.0 * 9),
         "segment churn guardrail (growth)"),
        (lambda r: r["inputs"]["churn"]["segments"][0].update(window_churn=599.0),
         "churn-risk ceiling (growth)"),
        (lambda r: r["inputs"].update(price_change_counts={"3": 1}), "price-change frequency"),
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


def test_long_term_compliance_ignores_small_segments_and_cuts() -> None:
    rec = copy.deepcopy(change_record())
    seg = rec["inputs"]["churn"]["segments"][0]
    seg.update(exposed_customers=5.0, window_churn=5.0, churn_exposure=50.0)  # < 20 customers
    rec["inputs"].update(price_change_counts={"2": 7})  # 2 + 1 = 3: at the limit, not over
    assert compliance(rec, policy()) == []


def test_truth_values_extra_churners_at_the_decisions_segment_clv() -> None:
    from praxis.science.pricing_shadow import _segment_clv

    prob = problem()
    assert _segment_clv(prob, "growth") == 4e8
    assert _segment_clv(prob, "enterprise") == 0.0  # the product had no such customers


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
    # the per-tier split (current tier on each day) adds up to the total
    assert acc.extra_by_tier.shape == (3, len(world.tiers))
    assert acc.extra_by_tier.sum(axis=1) == pytest.approx(acc.extra_churn)
    assert np.all(np.isfinite(contrib)) and not math.isclose(contrib[0], contrib[2])


def _row(as_of: str, product: str, status: str, price: int | None, delta: float) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "product": product,
        "status": status,
        "reason_codes": ["x"],
        "chosen_price_micros": price,
        "true_delta_objective_per_day": delta,
        "true_delta_contribution_per_day": delta + 1.0,
        "true_extra_churn_per_exposed_window": 0.001,
    }


def test_compare_reports_shows_what_the_later_method_did_with_each_earlier_change(
    tmp_path: Any,
) -> None:
    import json

    from praxis.science.__main__ import main as science_main
    from praxis.science.pricing_shadow import compare_reports

    world = {"world_config_hash": "w", "seed": 42}
    before = {
        **world,
        "policy_version": "policy-a",
        "rows": [
            _row("2026-07-20", "api", "change", 420, -5.0),  # lost value: altered later
            _row("2026-07-20", "gpu", "change", 50, 2.0),  # gained: kept
            _row("2026-07-20", "cpu", "hold", None, 0.0),  # not a change: ignored
            _row("2026-07-27", "api", "change", 420, -1.0),  # not decided later: skipped
        ],
    }
    after = {
        **world,
        "policy_version": "policy-b",
        "status_counts": {"hold": 1, "change": 1},
        "rows": [
            _row("2026-07-20", "api", "hold", None, 0.0),
            _row("2026-07-20", "gpu", "change", 50, 2.0),
        ],
    }
    out = compare_reports(before, after)
    assert (out["before_changes"], out["before_losing_changes"]) == (2, 1)
    assert (out["altered"], out["losing_altered"]) == (1, 1)
    assert out["before_true_delta_per_day"] == -3.0 and out["after_true_delta_per_day"] == 2.0
    assert out["rows"][0]["after"]["status"] == "hold" and out["rows"][0]["altered"]

    a, b, o = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "out" / "c.json"
    a.write_text(json.dumps(before))
    b.write_text(json.dumps(after))
    assert (
        science_main(["pricing-compare", "--before", str(a), "--after", str(b), "--out", str(o)])
        == 0
    )
    assert json.loads(o.read_text())["losing_altered"] == 1
    b.write_text(json.dumps({**after, "seed": 43}))
    assert (
        science_main(["pricing-compare", "--before", str(a), "--after", str(b), "--out", str(o)])
        == 1
    )
