"""Retry planner (golden + properties) and the dunning decider with its baseline fallback."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import timedelta
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.domain.dunning import DunningState
from praxis.recovery.artifact import RecoveryArtifact
from praxis.recovery.config import load_policy
from praxis.recovery.planner import (
    baseline_next_day,
    conditional,
    plan_retries,
    schedule_value,
)
from praxis.recovery.policy import DecisionContext, PolicyKind, RecoveryDecider
from tests.recovery.conftest import CREATED
from tests.recovery.helpers import features

POLICY = load_policy()
H = POLICY.bounds.horizon_days


def step_curve(day: int, level: float) -> np.ndarray:
    return np.where(np.arange(H + 1) >= day, level, 0.0)


# ------------------------------------------------------------------------- planner
def test_golden_single_retry_on_a_step_curve() -> None:
    """Collectible at day 5 w.p. 0.5, never otherwise: one retry on day 5 (hand computed)."""
    plan = plan_retries(
        step_curve(5, 0.5), now=0.0, attempts_made=1, amount_minor=10_000, policy=POLICY
    )
    # 0.5 * 10000 * (1 - 0.004 * 5) - 30 - 150 * 0.5 = 4795
    assert plan.retry_days == (5,)
    assert plan.expected_value_minor == pytest.approx(4795.0)
    assert plan.p_next_success == pytest.approx(0.5)


def test_stops_when_no_retry_pays() -> None:
    plan = plan_retries(
        step_curve(5, 0.5), now=0.0, attempts_made=1, amount_minor=100, policy=POLICY
    )
    assert plan.retry_days == () and plan.expected_value_minor == 0.0
    assert plan.next_retry_day is None


def test_no_retry_beyond_bounds() -> None:
    curve = np.linspace(0, 0.9, H + 1)
    assert (
        plan_retries(curve, now=0.0, attempts_made=3, amount_minor=10**6, policy=POLICY).retry_days
        == ()
    )
    assert (
        plan_retries(
            curve, now=float(H), attempts_made=1, amount_minor=10**6, policy=POLICY
        ).retry_days
        == ()
    )
    with pytest.raises(ValueError, match="cover days"):
        plan_retries(curve[:5], now=0.0, attempts_made=1, amount_minor=1, policy=POLICY)


def test_conditional_curve() -> None:
    curve = step_curve(5, 0.5)
    np.testing.assert_allclose(conditional(curve, 6.0), 0.0)  # failed after 5: never
    cond = conditional(np.linspace(0, 1, H + 1), 10.0)
    assert cond[10] == 0.0 and cond[20] == pytest.approx(1.0)
    assert cond[15] == pytest.approx(0.5)


curves = st.lists(st.floats(0, 0.2), min_size=H, max_size=H).map(
    lambda inc: np.minimum(np.concatenate([[0.0], np.cumsum(inc)]), 0.99)
)


def brute_force(curve: np.ndarray, now: float, remaining: int, amount: int) -> float:
    best = 0.0
    days = range(int(now) + 1, H + 1)
    for m in range(1, remaining + 1):
        for sched in itertools.combinations(days, m):
            best = max(
                best, schedule_value(curve, sched, now=now, amount_minor=amount, policy=POLICY)
            )
    return best


@settings(max_examples=40, deadline=None)
@given(curve=curves, now=st.integers(0, 12), amount=st.integers(500, 200_000))
def test_plan_is_optimal_and_within_bounds(curve: np.ndarray, now: int, amount: int) -> None:
    plan = plan_retries(curve, now=float(now), attempts_made=1, amount_minor=amount, policy=POLICY)
    days = plan.retry_days
    assert len(days) <= POLICY.bounds.max_attempts - 1
    assert all(a < b for a, b in itertools.pairwise((now, *days)))  # after now, increasing
    assert all(d <= H for d in days)
    assert plan.expected_value_minor >= 0.0
    assert plan.expected_value_minor == pytest.approx(
        schedule_value(curve, days, now=float(now), amount_minor=amount, policy=POLICY)
    )
    assert plan.expected_value_minor == pytest.approx(brute_force(curve, now, 2, amount), abs=1e-6)


@settings(max_examples=40, deadline=None)
@given(curve=curves, amount=st.integers(2_000, 200_000))
def test_replanning_after_a_failure_is_never_worse(curve: np.ndarray, amount: int) -> None:
    """Closed loop vs open loop: the re-plan at the first retry's failure is at least as good."""
    plan = plan_retries(curve, now=0.0, attempts_made=1, amount_minor=amount, policy=POLICY)
    if len(plan.retry_days) < 1:
        return
    first = plan.retry_days[0]
    replan = plan_retries(
        curve, now=float(first), attempts_made=2, amount_minor=amount, policy=POLICY
    )
    rest = plan.retry_days[1:]
    open_loop = schedule_value(curve, rest, now=float(first), amount_minor=amount, policy=POLICY)
    assert replan.expected_value_minor >= open_loop - 1e-6


def test_baseline_schedule() -> None:
    assert POLICY.baseline_schedule == (3, 10)
    assert baseline_next_day(POLICY, 1, 0.0) == 3
    assert baseline_next_day(POLICY, 2, 3.5) == 10.5
    assert baseline_next_day(POLICY, 3, 10.0) is None
    assert baseline_next_day(POLICY, 0, 0.0) is None


# ------------------------------------------------------------------------- decider
def ctx(attempts: int = 1, now: float = 0.0, **kw: Any) -> DecisionContext:
    base = {
        "features": features(np.random.default_rng(1), "insufficient_funds"),
        "attempts_made": attempts,
        "now_elapsed_days": now,
        "last_attempt_elapsed_days": now,
        "amount_minor": 19_900,
        "decided_at": CREATED + timedelta(days=1),
    }
    return DecisionContext(**{**base, **kw})


def test_baseline_follows_the_plan_text() -> None:
    d = RecoveryDecider(POLICY, None)
    first, second, third = d.decide(ctx(1, 0.0)), d.decide(ctx(2, 3.0)), d.decide(ctx(3, 10.0))
    assert (first.action, first.retry_elapsed_days, first.stage) == (
        "retry",
        3.0,
        DunningState.GRACE,
    )
    assert (second.action, second.retry_elapsed_days, second.stage) == (
        "retry",
        10.0,
        DunningState.RESTRICTED,
    )
    assert (third.action, third.retry_elapsed_days, third.stage) == (
        "stop",
        None,
        DunningState.SUSPENDED,
    )
    assert {x.policy_kind for x in (first, second, third)} == {PolicyKind.BASELINE}
    assert first.fallback_reason == "model_unavailable"
    late = d.decide(ctx(2, 15.0))  # 15 + 7 > horizon: never retry beyond the bound
    assert late.action == "stop"
    with pytest.raises(ValueError):
        d.decide(ctx(0))


def test_model_decision_and_record_fields(artifact: RecoveryArtifact) -> None:
    d = RecoveryDecider(POLICY, artifact).decide(ctx(1, 0.0))
    assert d.policy_kind is PolicyKind.MODEL and d.fallback_reason is None
    assert d.model_version == artifact.model_version and d.policy_version == POLICY.version
    assert d.action == "retry" and d.retry_elapsed_days is not None
    assert 0 < d.retry_elapsed_days <= H and d.planned_retry_days[0] == d.retry_elapsed_days
    assert 0.0 < (d.p_next_success or 0) <= 1.0 and (d.expected_value_minor or 0) > 0
    assert d.stage in (DunningState.GRACE, DunningState.RESTRICTED)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"decided_at": CREATED + timedelta(days=POLICY.model.max_age_days + 1)}, "model_stale"),
        ({"features": None}, "features_unavailable"),
    ],
)
def test_fallbacks(artifact: RecoveryArtifact, change: dict[str, Any], reason: str) -> None:
    d = RecoveryDecider(POLICY, artifact).decide(ctx(**change))
    assert d.policy_kind is PolicyKind.BASELINE and d.fallback_reason == reason
    assert d.retry_elapsed_days == 3.0  # exactly the baseline


def test_unknown_category_and_invalid_output_fall_back(
    artifact: RecoveryArtifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = RecoveryDecider(POLICY, artifact)
    odd = replace(ctx().features, tier="platinum")  # type: ignore[type-var]
    assert decider.decide(ctx(features=odd)).fallback_reason == "unknown_category"
    monkeypatch.setattr(
        type(artifact.survival),
        "prob_collectible",
        lambda self, rows, t: np.full((1, len(t)), np.nan),
    )
    assert decider.decide(ctx()).fallback_reason == "model_output_invalid"


def test_use_model_false_and_access_never_relaxes(artifact: RecoveryArtifact) -> None:
    assert RecoveryDecider(POLICY, artifact, use_model=False).decide(ctx()).fallback_reason == (
        "model_unavailable"
    )
    d = RecoveryDecider(POLICY, artifact).decide(ctx(current_stage=DunningState.RESTRICTED))
    assert d.stage in (DunningState.RESTRICTED, DunningState.SUSPENDED)
    b = RecoveryDecider(POLICY, None).decide(ctx(1, current_stage=DunningState.RESTRICTED))
    assert b.stage is DunningState.RESTRICTED
