"""Dunning decisions: the model policy, with the deterministic baseline as its fallback.

``RecoveryDecider.decide`` answers, after each failed attempt of an invoice: retry when (or
stop), and which access stage the customer is in. The model policy plans retries from the
champion's P(collectible by t) (``planner``); the baseline is the fixed schedule of
``policy.toml``. The baseline is used, and the reason recorded, whenever the model cannot be
trusted for this decision:

=========================  ===========================================================
``model_unavailable``      no artifact loaded (missing, corrupt, checksum mismatch)
``model_stale``            artifact older than ``model.max_age_days`` at decision time
``features_unavailable``   the customer's history could not be read
``unknown_category``       a categorical value the model never saw
``model_output_invalid``   non-finite or out-of-range probabilities
=========================  ===========================================================

The core dunning flow therefore never depends on the model being present (CLAUDE.md s23).
Hard bounds (``max_attempts``, ``horizon_days``) hold for both policies by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from praxis.domain.dunning import DunningState
from praxis.recovery.artifact import RecoveryArtifact
from praxis.recovery.config import RecoveryPolicy
from praxis.recovery.features import RecoveryFeatures, UnknownCategory
from praxis.recovery.planner import baseline_next_day, plan_retries

F64 = NDArray[np.float64]
_STAGE_RANK = {
    DunningState.PAST_DUE: 0,
    DunningState.GRACE: 1,
    DunningState.RESTRICTED: 2,
    DunningState.SUSPENDED: 3,
}


class PolicyKind(StrEnum):
    MODEL = "model"
    BASELINE = "baseline"


@dataclass(frozen=True, slots=True)
class DecisionContext:
    features: RecoveryFeatures | None
    attempts_made: int
    now_elapsed_days: float  # days since the first failure, at the latest failure
    last_attempt_elapsed_days: float
    amount_minor: int
    decided_at: datetime
    current_stage: DunningState | None = None


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: Literal["retry", "stop"]
    retry_elapsed_days: float | None
    stage: DunningState
    policy_kind: PolicyKind
    policy_version: str
    model_version: str | None
    fallback_reason: str | None
    expected_value_minor: float | None
    p_next_success: float | None
    planned_retry_days: tuple[int, ...] = ()


def _no_relax(stage: DunningState, current: DunningState | None) -> DunningState:
    """Access only tightens until the invoice is paid."""
    if current is None or current not in _STAGE_RANK:
        return stage
    return stage if _STAGE_RANK[stage] >= _STAGE_RANK[current] else current


class RecoveryDecider:
    def __init__(
        self, policy: RecoveryPolicy, artifact: RecoveryArtifact | None, *, use_model: bool = True
    ) -> None:
        self.policy = policy
        self.artifact = artifact if use_model else None
        self._grid = np.arange(policy.bounds.horizon_days + 1, dtype=np.float64)

    # -------------------------------------------------------------------- public
    def decide(self, ctx: DecisionContext) -> RecoveryDecision:
        if ctx.attempts_made < 1:
            raise ValueError("a dunning decision follows at least one failed attempt")
        artifact, features = self.artifact, ctx.features
        reason = self._unusable(ctx)
        if reason is None and artifact is not None and features is not None:
            try:
                curve = self.curve(artifact, features)
            except UnknownCategory:
                reason = "unknown_category"
            else:
                if curve is not None:
                    return self._model_decision(ctx, artifact, curve)
                reason = "model_output_invalid"
        return self.baseline(ctx, fallback_reason=reason)

    def curve(self, artifact: RecoveryArtifact, features: RecoveryFeatures) -> F64 | None:
        """Champion P(C <= d) for d = 0..horizon, or None when the output is unusable."""
        raw = artifact.champion.prob_collectible([features], self._grid)[0]
        if not np.all(np.isfinite(raw)) or raw.min() < -1e-9 or raw.max() > 1 + 1e-9:
            return None
        curve = np.maximum.accumulate(np.clip(raw, 0.0, 1.0))
        curve[0] = 0.0
        return curve

    def baseline(
        self, ctx: DecisionContext, *, fallback_reason: str | None = None
    ) -> RecoveryDecision:
        b = self.policy.bounds
        nxt = baseline_next_day(self.policy, ctx.attempts_made, ctx.last_attempt_elapsed_days)
        retry = nxt is not None and ctx.attempts_made < b.max_attempts and nxt <= b.horizon_days
        if not retry:
            stage = DunningState.SUSPENDED
        elif ctx.attempts_made == 1:
            stage = DunningState.GRACE
        else:
            stage = DunningState.RESTRICTED
        return RecoveryDecision(
            action="retry" if retry else "stop",
            retry_elapsed_days=nxt if retry else None,
            stage=_no_relax(stage, ctx.current_stage),
            policy_kind=PolicyKind.BASELINE,
            policy_version=self.policy.version,
            model_version=None,
            fallback_reason=fallback_reason,
            expected_value_minor=None,
            p_next_success=None,
        )

    # ------------------------------------------------------------------- private
    def _unusable(self, ctx: DecisionContext) -> str | None:
        if self.artifact is None:
            return "model_unavailable"
        age = ctx.decided_at - self.artifact.created_at
        if age > timedelta(days=self.policy.model.max_age_days):
            return "model_stale"
        if ctx.features is None:
            return "features_unavailable"
        return None

    def _model_decision(
        self, ctx: DecisionContext, artifact: RecoveryArtifact, curve: F64
    ) -> RecoveryDecision:
        plan = plan_retries(
            curve,
            now=ctx.now_elapsed_days,
            attempts_made=ctx.attempts_made,
            amount_minor=ctx.amount_minor,
            policy=self.policy,
        )
        day = plan.next_retry_day
        if day is None:
            stage = DunningState.SUSPENDED
        elif ctx.now_elapsed_days < self.policy.bounds.grace_days:
            stage = DunningState.GRACE
        else:
            stage = DunningState.RESTRICTED
        return RecoveryDecision(
            action="retry" if day is not None else "stop",
            retry_elapsed_days=float(day) if day is not None else None,
            stage=_no_relax(stage, ctx.current_stage),
            policy_kind=PolicyKind.MODEL,
            policy_version=self.policy.version,
            model_version=artifact.model_version,
            fallback_reason=None,
            expected_value_minor=plan.expected_value_minor,
            p_next_success=plan.p_next_success if day is not None else None,
            planned_retry_days=plan.retry_days,
        )
