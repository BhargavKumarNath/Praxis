"""Pricing decision records: the audit trail of every optimiser run.

A ``Decision`` holds everything required to reconstruct and audit it (project_plan Phase 6
"Outputs"): input model versions, feature snapshot references, candidate prices with their
objective values, the constraints, the chosen price, rejected alternatives with reasons,
reason codes, the guardrail outcome and the policy version.

Only ``Status.CHANGE`` carries a new price. Whether that price may be executed is NOT a
property of the decision: it depends on the mode and on the record having been persisted
(``praxis.pricing.store``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from praxis.pricing.config import Mode

RECORD_FORMAT = 1


class Status(StrEnum):
    CHANGE = "change"  # a new, feasible price that is expected to improve the objective
    HOLD = "hold"  # keep the current price (it is the best feasible option, or cooldown)
    FROZEN = "frozen"  # uncertainty too high to move the price
    INFEASIBLE = "infeasible"  # no candidate satisfies every constraint
    UNAVAILABLE = "unavailable"  # inputs missing, stale or invalid: no decision possible


class Reason(StrEnum):
    OPTIMUM_INTERIOR = "optimum_interior"
    CURRENT_PRICE_INFEASIBLE = "current_price_infeasible"  # a move forced by the constraints
    NO_IMPROVEMENT = "no_improvement"
    COOLDOWN = "cooldown"
    INFEASIBLE = "infeasible"
    UNCERTAINTY_ELASTICITY_SD = "uncertainty_elasticity_sd"
    UNCERTAINTY_LOW_CONFIDENCE = "uncertainty_low_confidence"
    UNCERTAINTY_FORECAST_WIDTH = "uncertainty_forecast_width"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EVIDENCE_TOO_OLD = "evidence_too_old"
    IMPLAUSIBLE_ELASTICITY = "implausible_elasticity"
    FORECAST_STALE = "forecast_stale"
    FORECAST_MISSING = "forecast_missing"
    FORECAST_UNAVAILABLE = "forecast_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    COST_UNAVAILABLE = "cost_unavailable"
    PRICE_UNAVAILABLE = "price_unavailable"
    INPUT_UNAVAILABLE = "input_unavailable"
    INVALID_INPUT = "invalid_input"
    INVALID_OBJECTIVE = "invalid_objective"


@dataclass(frozen=True)
class Candidate:
    price_micros: int
    log_ratio: float
    expected_demand: float
    contribution: float
    churn_cost: float
    delta_mean: float
    delta_sd: float
    delta_p05: float
    delta_p95: float
    delta_contribution_mean: float
    prob_improvement: float
    risk_adjusted: float
    feasible: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    cycle_id: str
    product: str
    as_of: date
    mode: Mode
    policy_version: str
    status: Status
    current_price_micros: int | None
    chosen_price_micros: int | None
    reason_codes: tuple[str, ...]
    lineage: Mapping[str, str] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[Candidate, ...] = ()
    guardrails: Mapping[str, Any] = field(default_factory=dict)
    prediction: Mapping[str, Any] | None = None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.status is Status.CHANGE) != (self.chosen_price_micros is not None):
            raise ValueError("exactly the CHANGE status carries a chosen price")
        if self.status is Status.CHANGE and self.chosen_price_micros == self.current_price_micros:
            raise ValueError("a CHANGE must change the price")
        if not self.reason_codes:
            raise ValueError("every decision needs at least one reason code")

    @property
    def decision_id(self) -> str:
        """Deterministic: the same cycle, product, mode, policy and inputs give the same id."""
        key = {
            "cycle_id": self.cycle_id,
            "product": self.product,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "lineage": dict(sorted(self.lineage.items())),
        }
        return "dec-" + _sha(_dumps(key))[:20]

    @property
    def rejected(self) -> list[dict[str, Any]]:
        """Every candidate not chosen, with the reason it lost."""
        out = []
        for c in self.candidates:
            if c.price_micros == self.chosen_price_micros:
                continue
            if c.violations:
                reasons = list(c.violations)
            elif c.price_micros == self.current_price_micros:
                reasons = ["not_chosen_current_price"]
            else:
                reasons = ["lower_risk_adjusted_objective"]
            out.append({"price_micros": c.price_micros, "reasons": reasons})
        return out

    def record(self) -> dict[str, Any]:
        """The canonical, JSON-serialisable audit record."""
        return {
            "record_format": RECORD_FORMAT,
            "decision_id": self.decision_id,
            "cycle_id": self.cycle_id,
            "product": self.product,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "status": self.status.value,
            "current_price_micros": self.current_price_micros,
            "chosen_price_micros": self.chosen_price_micros,
            "reason_codes": list(self.reason_codes),
            "lineage": dict(sorted(self.lineage.items())),
            "inputs": dict(self.inputs),
            "constraints": dict(self.constraints),
            "candidates": [_candidate_json(c) for c in self.candidates],
            "rejected_alternatives": self.rejected,
            "guardrails": dict(self.guardrails),
            "prediction": None if self.prediction is None else dict(self.prediction),
            "errors": list(self.errors),
            "is_synthetic": True,
        }

    def record_json(self) -> str:
        return _dumps(self.record())

    @property
    def record_sha256(self) -> str:
        return _sha(self.record_json())


def _candidate_json(c: Candidate) -> dict[str, Any]:
    return {
        "price_micros": c.price_micros,
        "log_ratio": c.log_ratio,
        "expected_demand": c.expected_demand,
        "contribution": c.contribution,
        "churn_cost": c.churn_cost,
        "delta_mean": c.delta_mean,
        "delta_sd": c.delta_sd,
        "delta_p05": c.delta_p05,
        "delta_p95": c.delta_p95,
        "delta_contribution_mean": c.delta_contribution_mean,
        "prob_improvement": c.prob_improvement,
        "risk_adjusted": c.risk_adjusted,
        "feasible": c.feasible,
        "violations": list(c.violations),
    }


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cycle_id(policy_name: str, mode: Mode, as_of: date) -> str:
    return f"{policy_name}:{mode.value}:{as_of.isoformat()}"
