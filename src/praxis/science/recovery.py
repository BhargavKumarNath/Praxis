"""Phase 8 evaluation: recovery models and dunning policy against simulator ground truth.

The only place where the recovery models meet the latent cure times (ADR 0012 / 0015). It

1. scores the champion, the challenger and the rate-table baseline on the observed first
   retries of the evaluation window (Brier, log loss, ECE, reliability, PR-AUC, segments);
2. checks the survival assumptions: randomised gap assignment (identification), the
   parametric fit against the model-free current-status estimate (training window), and the
   model's P(C <= t) against the TRUE mixture per reason (evaluation window);
3. replays every evaluation episode closed-loop under the baseline, the champion policy,
   the challenger policy, the model-unavailable fallback and an oracle, with the TRUE cure
   time deciding each retry, and values the outcomes with ``policy.toml``;
4. applies ``configs/recovery/acceptance.toml``.

Simplification (ADR 0015): a replay does not feed retries back into the simulated customer
(burden -> churn). Failed retries are reported as disruption and costed in the valuation.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from praxis.domain.dunning import DunningState
from praxis.recovery import metrics
from praxis.recovery.artifact import RecoveryArtifact, latest_artifact, load_artifact
from praxis.recovery.classifier import RateTable, first_retry_rows
from praxis.recovery.config import RecoveryPolicy
from praxis.recovery.dataset import Episode, build_episodes, failed_between
from praxis.recovery.policy import DecisionContext, RecoveryDecider
from praxis.recovery.survival import CureWeibull
from praxis.recovery.train import assignment_audit, survival_fit_check
from praxis.recovery.warehouse import load_histories
from praxis.simulator.config import SimulationConfig
from praxis.simulator.engine import Engine, RecoveryTruth
from praxis.simulator.population import generate_population

F64 = NDArray[np.float64]
_REPO = Path(__file__).resolve().parents[3]
DEFAULT_ACCEPTANCE = _REPO / "configs" / "recovery" / "acceptance.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Protocol(_Cfg):
    selection_cutoff_day: int = Field(gt=0)
    train_cutoff_day: int = Field(gt=0)
    eval_end_day: int = Field(gt=0)


class ModelGates(_Cfg):
    max_ece: float = Field(gt=0)  # reported reference (ADR 0015 amendment)
    ece_null_reps: int = Field(ge=100)
    ece_null_seed: int
    segment_min_rows: int = Field(ge=1)
    segment_tolerance: float = Field(gt=0)
    segment_z: float = Field(gt=0)
    champion_beats_table_brier: bool
    champion_beats_table_pr_auc: bool
    champion_beats_base_rate: bool


class SurvivalGates(_Cfg):
    fit_cell_min_rows: int = Field(ge=1)
    fit_tolerance: float = Field(gt=0)
    fit_z: float = Field(gt=0)
    min_assignment_p: float = Field(gt=0, lt=1)
    truth_grid_days: tuple[int, ...]
    max_truth_error: float = Field(gt=0)
    truth_min_episodes: int = Field(ge=1)


class PolicyGates(_Cfg):
    bootstrap_reps: int = Field(ge=100)
    bootstrap_seed: int
    require_net_gain: bool
    max_recovery_rate_drop: float = Field(ge=0)
    min_recovered_revenue_ratio: float = Field(gt=0)
    require_fallback_equals_baseline: bool
    max_bound_violations: int = Field(ge=0)


class Acceptance(_Cfg):
    protocol: Protocol
    models: ModelGates
    survival: SurvivalGates
    policy: PolicyGates


def load_acceptance(path: Path | None = None) -> Acceptance:
    return Acceptance.model_validate(
        tomllib.loads((path or DEFAULT_ACCEPTANCE).read_text(encoding="utf-8"))
    )


class EvaluationError(RuntimeError):
    """Inputs do not belong together (world, artifact, protocol)."""


# ------------------------------------------------------------------------------ truth
def collect_truth(world: SimulationConfig, seed: int) -> dict[str, RecoveryTruth]:
    """Re-run the world with a recovery observer: latent cure time of every failed invoice."""
    if world.billing.recovery is None:
        raise EvaluationError("the world has no recovery ground truth (billing.recovery)")
    truth: dict[str, RecoveryTruth] = {}

    def observe(t: RecoveryTruth) -> None:
        truth[t.invoice_id] = t

    engine = Engine(world, seed, generate_population(world, seed), recovery_observer=observe)
    for _ in engine.run():
        pass
    return truth


# ----------------------------------------------------------------------------- replay
@dataclass(frozen=True)
class Outcome:
    recovered: bool
    recovery_day: float | None
    retries: int
    failed_retries: int
    net_value: float
    revenue: int
    restricted_days: float
    suspended_days: float
    schedule: tuple[float, ...]
    bound_violation: bool


Decide = Callable[[DecisionContext], Any]


def replay(
    episode: Episode, truth: RecoveryTruth, decide: Decide, policy: RecoveryPolicy
) -> Outcome:
    """Closed loop: decide, let the TRUE cure time settle the retry, re-decide on failure."""
    b, v = policy.bounds, policy.valuation
    attempts, now, stage = 1, 0.0, DunningState.PAST_DUE
    schedule: list[float] = []
    spans: list[tuple[float, DunningState]] = []
    recovered_at: float | None = None
    violation = False
    while True:
        decision = decide(
            DecisionContext(
                features=episode.features,
                attempts_made=attempts,
                now_elapsed_days=now,
                last_attempt_elapsed_days=now,
                amount_minor=episode.amount_minor,
                decided_at=episode.failed_at + timedelta(days=now),
                current_stage=stage,
            )
        )
        stage = decision.stage
        spans.append((now, stage))
        if decision.action != "retry":
            break
        day = float(decision.retry_elapsed_days)
        attempts += 1
        violation |= attempts > b.max_attempts or day > b.horizon_days or day <= now
        schedule.append(day)
        if truth.cured and day >= truth.cure_days:
            recovered_at = day
            break
        now = day
    end = recovered_at if recovered_at is not None else float(b.horizon_days)
    bounds_ = [s[0] for s in spans[1:]] + [end]
    restricted = sum(
        max(0.0, hi - lo)
        for (lo, st), hi in zip(spans, bounds_, strict=True)
        if st is DunningState.RESTRICTED
    )
    suspended = sum(
        max(0.0, hi - lo)
        for (lo, st), hi in zip(spans, bounds_, strict=True)
        if st is DunningState.SUSPENDED
    )
    failed = len(schedule) - (1 if recovered_at is not None else 0)
    gain = (
        episode.amount_minor * (1.0 - v.delay_cost_per_day * recovered_at) if recovered_at else 0.0
    )
    net = gain - v.retry_cost_minor * len(schedule) - v.failed_retry_cost_minor * failed
    return Outcome(
        recovered=recovered_at is not None,
        recovery_day=recovered_at,
        retries=len(schedule),
        failed_retries=failed,
        net_value=float(net),
        revenue=episode.amount_minor if recovered_at is not None else 0,
        restricted_days=restricted,
        suspended_days=suspended,
        schedule=tuple(schedule),
        bound_violation=violation,
    )


def oracle(episode: Episode, truth: RecoveryTruth, policy: RecoveryPolicy) -> float:
    """Best achievable net value knowing C: one retry on the first whole day >= C, if worth it."""
    if not truth.cured:
        return 0.0
    day = max(1, math.ceil(truth.cure_days - 1e-9))
    if day > policy.bounds.horizon_days:
        return 0.0
    v = policy.valuation
    value = episode.amount_minor * (1.0 - v.delay_cost_per_day * day) - v.retry_cost_minor
    return max(0.0, value)


def summarise(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    n = len(outcomes)
    rec = [o for o in outcomes if o.recovered]
    return {
        "episodes": n,
        "recovery_rate": len(rec) / n if n else math.nan,
        "recovered_revenue_minor": sum(o.revenue for o in outcomes),
        "net_value_minor": float(sum(o.net_value for o in outcomes)),
        "mean_time_to_recovery_days": float(np.mean([o.recovery_day for o in rec]))
        if rec
        else math.nan,
        "retries": sum(o.retries for o in outcomes),
        "failed_retries": sum(o.failed_retries for o in outcomes),
        "failed_retries_per_episode": sum(o.failed_retries for o in outcomes) / n
        if n
        else math.nan,
        "restricted_days_per_episode": sum(o.restricted_days for o in outcomes) / n
        if n
        else math.nan,
        "bound_violations": sum(o.bound_violation for o in outcomes),
    }


def paired_bootstrap(diff: F64, reps: int, seed: int) -> tuple[float, float, float]:
    """Mean of ``diff`` and its percentile 95% CI (episodes resampled with replacement)."""
    rng = np.random.default_rng(seed)
    n = len(diff)
    means = np.array([diff[rng.integers(0, n, n)].mean() for _ in range(reps)])
    return float(diff.mean()), float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


# ---------------------------------------------------------------------------- report
def _check(name: str, passed: bool, **detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), **detail}


@dataclass(frozen=True)
class Inputs:
    world: SimulationConfig
    seed: int
    db: Path
    models: Path
    policy: RecoveryPolicy
    acceptance: Acceptance


def _day(world: SimulationConfig, day: int) -> datetime:
    return datetime.combine(world.run.start_date, time(0), tzinfo=UTC) + timedelta(days=day)


def _model_metrics(
    artifact: RecoveryArtifact, episodes: Sequence[Episode], gates: ModelGates
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kept, elapsed, label = first_retry_rows(episodes)
    rows = [e.features for e in kept]
    surv, clf = artifact.survival, artifact.classifier
    preds = {
        surv.name: surv.prob_at(rows, elapsed),
        clf.name: clf.predict(rows, elapsed),
        RateTable.name: artifact.table.predict(rows, elapsed),
        "base_rate": np.full(len(rows), artifact.table.overall),
    }
    out: dict[str, Any] = {"rows": len(rows), "observed_rate": float(label.mean())}
    for name, p in preds.items():
        out[name] = {
            "brier": metrics.brier(p, label),
            "log_loss": metrics.log_loss(p, label),
            "ece": metrics.ece(p, label),
            "pr_auc": metrics.pr_auc(p, label),
            "reliability": metrics.reliability(p, label),
        }
    champ = str(artifact.manifest["champion"])
    p = preds[champ]
    segs = {
        "reason": [e.features.reason for e in kept],
        "tier": [e.features.tier for e in kept],
    }
    segments = {
        k: metrics.segment_calibration(
            p,
            label,
            v,
            min_rows=gates.segment_min_rows,
            tolerance=gates.segment_tolerance,
            z=gates.segment_z,
        )
        for k, v in segs.items()
    }
    out["champion_segments"] = segments
    c, t, base = out[champ], out[RateTable.name], out["base_rate"]
    null_p95 = ece_null_quantile(p, gates.ece_null_reps, gates.ece_null_seed)
    out["champion_ece_null_p95"] = null_p95
    checks = [
        _check(
            "champion_ece_consistent_with_calibration",
            c["ece"] <= null_p95,
            value=c["ece"],
            null_p95=null_p95,
            reference_max=gates.max_ece,
            below_reference=c["ece"] <= gates.max_ece,
        ),
        _check(
            "champion_segment_calibration",
            all(s["passed"] for rows_ in segments.values() for s in rows_),
            failed=[s for rows_ in segments.values() for s in rows_ if not s["passed"]],
        ),
    ]
    if gates.champion_beats_table_brier:
        checks.append(
            _check(
                "champion_brier_vs_table",
                c["brier"] <= t["brier"],
                champion=c["brier"],
                table=t["brier"],
            )
        )
    if gates.champion_beats_table_pr_auc:
        checks.append(
            _check(
                "champion_pr_auc_vs_table",
                c["pr_auc"] >= t["pr_auc"],
                champion=c["pr_auc"],
                table=t["pr_auc"],
            )
        )
    if gates.champion_beats_base_rate:
        checks.append(
            _check(
                "champion_brier_vs_base_rate",
                c["brier"] < base["brier"],
                champion=c["brier"],
                base_rate=base["brier"],
            )
        )
    return out, checks


def ece_null_quantile(p: F64, reps: int, seed: int, q: float = 0.95) -> float:
    """``q``-quantile of ECE when outcomes really are Bernoulli(p): the noise floor at this n."""
    rng = np.random.default_rng(seed)
    draws = [metrics.ece(p, (rng.random(len(p)) < p).astype(np.float64)) for _ in range(reps)]
    return float(np.quantile(draws, q))


def _truth_recovery(
    artifact: RecoveryArtifact,
    episodes: Sequence[Episode],
    truth: dict[str, RecoveryTruth],
    gates: SurvivalGates,
) -> dict[str, Any]:
    grid = np.array(gates.truth_grid_days, dtype=np.float64)
    out: dict[str, Any] = {}
    for name in (artifact.survival.name, artifact.classifier.name):
        model = artifact.model(name)
        per_reason: dict[str, Any] = {}
        for reason in sorted({e.features.reason for e in episodes}):
            eps = [e for e in episodes if e.features.reason == reason]
            pred = model.prob_collectible([e.features for e in eps], grid).mean(axis=0)
            true = np.array(
                [[truth[e.invoice_id].collectible_by(t) for t in grid] for e in eps]
            ).mean(axis=0)
            per_reason[reason] = {
                "episodes": len(eps),
                "predicted": pred.round(4).tolist(),
                "true": true.round(4).tolist(),
                "max_abs_error": float(np.max(np.abs(pred - true))),
            }
        gated = {k: v for k, v in per_reason.items() if v["episodes"] >= gates.truth_min_episodes}
        out[name] = {
            "grid_days": list(gates.truth_grid_days),
            "per_reason": per_reason,
            "gated_reasons": sorted(gated),
            "max_abs_error": max((r["max_abs_error"] for r in gated.values()), default=0.0),
            "max_abs_error_all_reasons": max(r["max_abs_error"] for r in per_reason.values()),
        }
    return out


def _survival(
    inputs: Inputs,
    artifact: RecoveryArtifact,
    train_eps: Sequence[Episode],
    eval_eps: Sequence[Episode],
    truth: dict[str, RecoveryTruth],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    g = inputs.acceptance.survival
    rec = inputs.world.billing.recovery
    if rec is None:
        raise EvaluationError("the world has no recovery ground truth (billing.recovery)")
    audit = assignment_audit([*train_eps, *eval_eps], rec.gap_choices_days)
    cells = survival_fit_check(
        artifact.survival,
        train_eps,
        min_rows=g.fit_cell_min_rows,
        tolerance=g.fit_tolerance,
        z=g.fit_z,
    )
    truth_fit = _truth_recovery(artifact, eval_eps, truth, g)
    lower = np.array([e.interval[0] for e in eval_eps])
    upper = np.array([e.interval[1] for e in eval_eps])
    concordance = {}
    for name in (artifact.survival.name, artifact.classifier.name):
        # later expected collectibility = larger "time": use 1 - P(C <= 10)
        p10 = artifact.model(name).prob_collectible(
            [e.features for e in eval_eps], np.array([10.0])
        )[:, 0]
        concordance[name] = metrics.interval_concordance(1.0 - p10, lower, upper)
    champ = str(artifact.manifest["champion"])
    report = {
        "assignment_audit": audit,
        "current_status_fit_training": cells,
        "truth_recovery": truth_fit,
        "concordance_eval": concordance,  # reported only (one metric among several)
        "censored_eval_share": float(np.mean(~np.isfinite(upper))),
    }
    checks = [
        _check(
            "gap_assignment_uniform",
            audit["uniform_p"] >= g.min_assignment_p,
            p=audit["uniform_p"],
        ),
        _check(
            "gap_assignment_independent_of_reason",
            audit["independent_of_reason_p"] >= g.min_assignment_p,
            p=audit["independent_of_reason_p"],
        ),
        _check(
            "survival_current_status_fit",
            bool(cells) and all(c["passed"] for c in cells),
            cells=len(cells),
            failed=[c for c in cells if not c["passed"]],
        ),
        _check(
            "champion_truth_recovery",
            bool(truth_fit[champ]["gated_reasons"])
            and truth_fit[champ]["max_abs_error"] <= g.max_truth_error,
            max_abs_error=truth_fit[champ]["max_abs_error"],
            max=g.max_truth_error,
            gated_reasons=truth_fit[champ]["gated_reasons"],
        ),
    ]
    return report, checks


def _policies(
    inputs: Inputs,
    artifact: RecoveryArtifact,
    episodes: Sequence[Episode],
    truth: dict[str, RecoveryTruth],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy, gates = inputs.policy, inputs.acceptance.policy
    champion = RecoveryDecider(policy, artifact)
    challenger_art = replace(
        artifact,
        manifest={**artifact.manifest, "champion": artifact.manifest["challenger"]},
    )
    challenger = RecoveryDecider(policy, challenger_art)
    fallback = RecoveryDecider(policy, None)
    deciders: dict[str, Decide] = {
        "baseline": champion.baseline,
        "champion": champion.decide,
        "challenger": challenger.decide,
        "fallback_model_unavailable": fallback.decide,
    }
    outcomes = {
        name: [replay(e, truth[e.invoice_id], fn, policy) for e in episodes]
        for name, fn in deciders.items()
    }
    oracle_total = float(sum(oracle(e, truth[e.invoice_id], policy) for e in episodes))
    report: dict[str, Any] = {n: summarise(o) for n, o in outcomes.items()}
    base, champ = report["baseline"], report["champion"]
    diff = np.array(
        [
            c.net_value - b.net_value
            for c, b in zip(outcomes["champion"], outcomes["baseline"], strict=True)
        ]
    )
    mean, lo, hi = paired_bootstrap(diff, gates.bootstrap_reps, gates.bootstrap_seed)
    report["champion_minus_baseline_net_value"] = {"mean_per_episode": mean, "ci95": [lo, hi]}
    report["oracle_net_value_minor"] = oracle_total
    for name in ("baseline", "champion", "challenger"):
        report[name]["captured_share_of_oracle"] = (
            report[name]["net_value_minor"] / oracle_total if oracle_total > 0 else math.nan
        )
    same = all(
        f.schedule == b.schedule
        for f, b in zip(outcomes["fallback_model_unavailable"], outcomes["baseline"], strict=True)
    )
    violations = sum(report[n]["bound_violations"] for n in ("champion", "challenger", "baseline"))
    rev_ratio = (
        champ["recovered_revenue_minor"] / base["recovered_revenue_minor"]
        if base["recovered_revenue_minor"]
        else math.nan
    )
    checks = [
        _check(
            "policy_net_value_gain",
            (lo > 0) or not gates.require_net_gain,
            mean_per_episode=mean,
            ci95=[lo, hi],
        ),
        _check(
            "policy_recovery_rate",
            champ["recovery_rate"] >= base["recovery_rate"] - gates.max_recovery_rate_drop,
            champion=champ["recovery_rate"],
            baseline=base["recovery_rate"],
        ),
        _check(
            "policy_recovered_revenue",
            rev_ratio >= gates.min_recovered_revenue_ratio,
            ratio=rev_ratio,
        ),
        _check(
            "fallback_equals_baseline",
            same or not gates.require_fallback_equals_baseline,
        ),
        _check("policy_bounds", violations <= gates.max_bound_violations, violations=violations),
    ]
    return report, checks


def evaluate(inputs: Inputs) -> dict[str, Any]:
    proto = inputs.acceptance.protocol
    path = latest_artifact(inputs.models)
    if path is None:
        raise EvaluationError(f"no recovery artifact under {inputs.models}")
    artifact = load_artifact(path)
    train_cutoff = _day(inputs.world, proto.train_cutoff_day)
    if datetime.fromisoformat(artifact.manifest["train_cutoff"]) != train_cutoff:
        raise EvaluationError("artifact train_cutoff does not match the pre-registered protocol")
    if artifact.manifest["policy_version"] != inputs.policy.version:
        raise EvaluationError("artifact was trained under another dunning policy version")
    truth = collect_truth(inputs.world, inputs.seed)
    world_end = _day(inputs.world, inputs.world.run.days)
    all_eps = build_episodes(load_histories(inputs.db, world_end))
    eval_eps = failed_between(all_eps, train_cutoff, _day(inputs.world, proto.eval_end_day))
    missing = [e.invoice_id for e in eval_eps if e.invoice_id not in truth]
    if missing or not eval_eps:
        raise EvaluationError(f"truth missing for {len(missing)} of {len(eval_eps)} episodes")
    train_eps = build_episodes(load_histories(inputs.db, train_cutoff))
    model_report, model_checks = _model_metrics(artifact, eval_eps, inputs.acceptance.models)
    surv_report, surv_checks = _survival(inputs, artifact, train_eps, eval_eps, truth)
    policy_report, policy_checks = _policies(inputs, artifact, eval_eps, truth)
    checks = [*model_checks, *surv_checks, *policy_checks]
    return {
        "is_synthetic": True,
        "seed": inputs.seed,
        "config_hash": inputs.world.config_hash,
        "model_version": artifact.model_version,
        "champion": artifact.manifest["champion"],
        "challenger": artifact.manifest["challenger"],
        "policy_version": inputs.policy.version,
        "evaluation_episodes": len(eval_eps),
        "evaluation_recovered_true_share": float(
            np.mean([truth[e.invoice_id].cured for e in eval_eps])
        ),
        "models": model_report,
        "survival": surv_report,
        "policy": policy_report,
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "survival_model": CureWeibull.name,
    }
