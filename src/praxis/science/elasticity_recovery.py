"""Ground-truth recovery evaluation for the price-elasticity analysis (Phase 5).

This module is the ONLY place that joins elasticity estimates with the simulator's latent
elasticities. Model code (``praxis.elasticity``) can never import it (import contract).

Truth for a set of units S = sum_u w_u e_u / sum_u w_u, with e_u the customer's latent
elasticity and w_u the unit's design weight pi (1 - pi) (log ratio)^2: exactly the estimand of
the fixed-effect OLS slope over S (docs/elasticity.md). Sets: pooled = units with an observed
outcome; tier / industry / cell = units in the hierarchical model.
"""

from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from praxis.simulator.config import TIERS, SimulationConfig

F64 = NDArray[np.float64]
DEFAULT_ACCEPTANCE = Path(__file__).resolve().parents[3] / "configs/elasticity/acceptance.toml"
CELL_SEPARATOR = "|"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Recovery(_Cfg):
    require_validity: bool
    require_diagnostics: bool
    sign_interval: float = Field(gt=0, lt=1)
    pooled_max_rel_error: float = Field(gt=0)
    pooled_max_abs_z: float = Field(gt=0)
    tier_max_rel_error: float = Field(gt=0)
    ordering_min_separation_sd: float = Field(gt=0)
    ordering_min_posterior_prob: float = Field(gt=0.5, lt=1)
    cell_interval: float = Field(gt=0, lt=1)
    cell_min_coverage: float = Field(gt=0, le=1)
    pooling_must_not_increase_rmse: bool


class Contamination(_Cfg):
    rate_abs_tolerance: float = Field(gt=0)
    iv_max_rel_error: float = Field(gt=0)
    itt_must_attenuate: bool


class Acceptance(_Cfg):
    recovery: Recovery
    contamination: Contamination


def load_acceptance(path: Path | None = None) -> Acceptance:
    raw = tomllib.loads((path or DEFAULT_ACCEPTANCE).read_text(encoding="utf-8"))
    return Acceptance.model_validate(raw)


class TruthError(RuntimeError):
    """Ground truth does not belong to the analysed world."""


@dataclass(frozen=True)
class Truth:
    """Latent elasticity per customer index plus the world's labels."""

    elasticity: F64
    tier: NDArray[np.int8]
    industry: NDArray[np.int8]
    industries: tuple[str, ...]

    @classmethod
    def load(cls, npz: Path, world: SimulationConfig) -> Self:
        with np.load(npz) as data:
            if str(data["config_hash"]) != world.config_hash:
                raise TruthError("ground truth was generated from a different world config")
            return cls(
                np.asarray(data["elasticity"], dtype=np.float64),
                np.asarray(data["tier"]),
                np.asarray(data["industry"]),
                tuple(i.id for i in world.industries),
            )


def _index(customer_id: str) -> int:
    return int(customer_id.split("_", 1)[1])


@dataclass(frozen=True)
class UnitTruth:
    """Analysed units joined to ground truth (labels verified against the world)."""

    elasticity: F64
    weight: F64
    observed: NDArray[np.bool_]
    in_model: NDArray[np.bool_]
    tier: NDArray[np.str_]
    industry: NDArray[np.str_]
    product: NDArray[np.str_]

    @classmethod
    def join(cls, units: list[dict[str, Any]], truth: Truth) -> Self:
        idx = np.array([_index(u["customer"]) for u in units], dtype=np.int64)
        tier = np.array([u["tier"] for u in units])
        industry = np.array([u["industry"] for u in units])
        if (tier != np.array(TIERS)[truth.tier[idx]]).any() or (
            industry != np.array(truth.industries)[truth.industry[idx]]
        ).any():
            raise TruthError("unit labels disagree with ground truth (wrong world or id mapping)")
        return cls(
            truth.elasticity[idx],
            np.array([u["design_weight"] for u in units], dtype=np.float64),
            np.array([u["delta"] is not None for u in units]),
            np.array([bool(u["in_model"]) for u in units]),
            tier,
            industry,
            np.array([u["product"] for u in units]),
        )

    def mean(self, mask: NDArray[np.bool_]) -> float:
        w = self.weight[mask]
        return float(self.elasticity[mask] @ w / w.sum())

    def by(self, labels: NDArray[np.str_]) -> dict[str, float]:
        m = self.in_model
        return {str(k): self.mean(m & (labels == k)) for k in np.unique(labels[m]).tolist()}

    def cells(self) -> dict[str, float]:
        labels = np.char.add(np.char.add(self.tier, CELL_SEPARATOR), self.industry)
        return self.by(labels)


@dataclass(frozen=True)
class Result:
    name: str
    passed: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _rel(est: float, truth: float) -> float:
    return abs(est - truth) / abs(truth)


def _gates(report: dict[str, Any], skip: Callable[[dict[str, Any]], bool]) -> Result:
    failed = [
        f"{c['experiment']}:{c['name']}"
        for c in report["validity"]["checks"]
        if c["gate"] and not c["passed"] and not skip(c)
    ]
    return Result("validity_gates", not failed, {"failed": failed})


def _sign(report: dict[str, Any], level: float) -> Result:
    """Pooled OLS CI and every tier's central interval at ``level`` lie below zero."""
    pooled = report["estimates"]["pooled"]
    highs = {"pooled": pooled["ci_high"]}
    if not math.isclose(level, 0.95):
        raise ValueError("the report carries 95% intervals; sign_interval must be 0.95")
    for tier, s in report["hierarchical"]["tier"].items():
        highs[f"tier:{tier}"] = s["ci95_high"]
    return Result("sign", all(h < 0 for h in highs.values()), {"interval_high": highs})


def _pooled(report: dict[str, Any], truth: float, a: Recovery) -> Result:
    p = report["estimates"]["pooled"]
    rel, z = _rel(p["estimate"], truth), abs(p["estimate"] - truth) / p["se"]
    detail = {"estimate": p["estimate"], "se": p["se"], "truth": truth, "rel_error": rel, "z": z}
    return Result(
        "pooled_magnitude", rel <= a.pooled_max_rel_error and z <= a.pooled_max_abs_z, detail
    )


def _tiers(report: dict[str, Any], truths: dict[str, float], a: Recovery) -> Result:
    hier = report["hierarchical"]["tier"]
    rows = {
        t: {"estimate": hier[t]["mean"], "truth": v, "rel_error": _rel(hier[t]["mean"], v)}
        for t, v in truths.items()
    }
    ok = set(rows) == set(hier) and all(
        r["rel_error"] <= a.tier_max_rel_error for r in rows.values()
    )
    return Result("tier_magnitude", ok, rows)


def _ordering(report: dict[str, Any], truths: dict[str, dict[str, float]], a: Recovery) -> Result:
    rows, identifiable = [], 0
    for level in ("tier", "industry"):
        for pair in report["hierarchical"]["pairwise"][level]:
            diff = truths[level][pair["a"]] - truths[level][pair["b"]]
            if abs(diff) < a.ordering_min_separation_sd * pair["sd_difference"]:
                continue
            identifiable += 1
            p_correct = pair["p_a_less_than_b"] if diff < 0 else 1.0 - pair["p_a_less_than_b"]
            ok = (pair["mean_difference"] < 0) == (
                diff < 0
            ) and p_correct >= a.ordering_min_posterior_prob
            rows.append(
                {
                    "level": level,
                    "a": pair["a"],
                    "b": pair["b"],
                    "truth_difference": diff,
                    "estimated_difference": pair["mean_difference"],
                    "p_correct": p_correct,
                    "passed": ok,
                }
            )
    passed = identifiable > 0 and all(r["passed"] for r in rows)
    return Result("segment_ordering", passed, {"identifiable_pairs": identifiable, "pairs": rows})


def _rmse(est: dict[str, float], truth: dict[str, float]) -> float:
    return math.sqrt(sum((est[k] - truth[k]) ** 2 for k in truth) / len(truth))


def _cells(report: dict[str, Any], truths: dict[str, float], a: Recovery) -> list[Result]:
    hier = report["hierarchical"]["cells"]
    if not math.isclose(report["hierarchical"]["sampler"]["interval"], a.cell_interval):
        raise ValueError("cell interval in the report differs from the acceptance")
    covered = {
        c: hier[c]["interval_low"] <= v <= hier[c]["interval_high"] for c, v in truths.items()
    }
    share = sum(covered.values()) / len(covered)
    unpooled = {c: report["estimates"]["cell"][c]["estimate"] for c in truths}
    pooled = {c: hier[c]["mean"] for c in truths}
    eb = {c: report["empirical_bayes"]["cells"][c]["estimate"] for c in truths}
    rmse = {
        "unpooled": _rmse(unpooled, truths),
        "hierarchical": _rmse(pooled, truths),
        "empirical_bayes": _rmse(eb, truths),
    }
    pooling_ok = not a.pooling_must_not_increase_rmse or rmse["hierarchical"] <= rmse["unpooled"]
    return [
        Result(
            "cell_interval_coverage",
            share >= a.cell_min_coverage,
            {"coverage": share, "cells": len(covered), "covered": covered},
        ),
        Result("pooling_justified", pooling_ok, {"rmse_vs_truth": rmse}),
    ]


def evaluate_recovery(report: dict[str, Any], units: UnitTruth, a: Recovery) -> list[Result]:
    tier_truth = units.by(units.tier)
    industry_truth = units.by(units.industry)
    results = [
        _sign(report, a.sign_interval),
        _pooled(report, units.mean(units.observed), a),
        _tiers(report, tier_truth, a),
        _ordering(report, {"tier": tier_truth, "industry": industry_truth}, a),
        *_cells(report, units.cells(), a),
    ]
    if a.require_validity:
        results.append(_gates(report, lambda _c: False))
    if a.require_diagnostics:
        diag = report["hierarchical"]["diagnostics"]
        results.append(Result("bayesian_diagnostics", bool(diag["passed"]), diag["gates"]))
    return results


def evaluate_contamination(
    report: dict[str, Any], units: UnitTruth, fractions: dict[str, float], a: Contamination
) -> list[Result]:
    """For a world where some tests' control units were charged the treatment price."""
    by_test = {
        c["experiment"]: c for c in report["validity"]["checks"] if c["name"] == "contamination"
    }
    rates = {
        exp: {"configured": f, "detected": by_test[exp]["detail"]["by_arm"]["control"]["rate"]}
        for exp, f in fractions.items()
    }
    rate_ok = all(
        abs(r["detected"] - r["configured"]) <= a.rate_abs_tolerance for r in rates.values()
    )
    truth = units.mean(units.observed)
    iv = report["estimates"]["pooled_iv"]
    iv_rel = _rel(iv["estimate"], truth)
    per_test = report["estimates"]["per_test"]
    attenuation = {
        exp: {"itt": per_test[exp]["itt"]["estimate"], "iv": per_test[exp]["iv"]["estimate"]}
        for exp in fractions
    }
    attenuated = all(abs(v["itt"]) < abs(v["iv"]) for v in attenuation.values())
    contaminated = set(fractions)
    return [
        Result("contamination_detected", rate_ok, {"rates": rates}),
        Result(
            "iv_recovers_elasticity",
            iv_rel <= a.iv_max_rel_error,
            {
                "iv": iv["estimate"],
                "truth": truth,
                "rel_error": iv_rel,
                "itt_pooled": report["estimates"]["pooled"]["estimate"],
            },
        ),
        Result("itt_attenuated", attenuated or not a.itt_must_attenuate, attenuation),
        _gates(report, lambda c: c["name"] == "contamination" and c["experiment"] in contaminated),
    ]


def failure_cases(report: dict[str, Any], units: UnitTruth) -> dict[str, Any]:
    """Reported only: estimators that are expected to be wrong, measured against truth."""
    truth = units.mean(units.observed)
    per_test = report["estimates"]["per_test"]
    naive = {
        exp: {"naive_pre_post": v["naive_pre_post"]["estimate"], "itt": v["itt"]["estimate"]}
        for exp, v in per_test.items()
        if v["naive_pre_post"] and v["itt"]
    }
    ols_tiers = {t: v["estimate"] for t, v in report["estimates"]["tier"].items()}
    # Raises and cuts were tested on DIFFERENT products (different customer mixes), so the two
    # dose estimates must be read against their own truths, not against each other.
    raised = {v["product"] for v in per_test.values() if v["log_price_ratio"] > 0}
    is_raise = np.isin(units.product, sorted(raised))
    return {
        "pooled_truth": truth,
        "naive_pre_post_vs_randomised": naive,
        "dose": {
            k: {
                "estimate": v["estimate"],
                "truth": units.mean(units.observed & (is_raise == (k == "raise"))),
            }
            for k, v in report["estimates"]["dose"].items()
        },
        "ols_tier_estimates": ols_tiers,
        "tier_truth": units.by(units.tier),
    }


def contamination_fractions(world: SimulationConfig) -> dict[str, float]:
    return {
        iv.id: iv.contamination_fraction
        for iv in world.pricing.interventions
        if iv.contamination_fraction > 0
    }


def evaluate(
    report: dict[str, Any],
    units_rows: list[dict[str, Any]],
    truth: Truth,
    world: SimulationConfig,
    acceptance: Acceptance,
) -> dict[str, Any]:
    units = UnitTruth.join(units_rows, truth)
    fractions = contamination_fractions(world)
    results: Iterable[Result] = (
        evaluate_contamination(report, units, fractions, acceptance.contamination)
        if fractions
        else evaluate_recovery(report, units, acceptance.recovery)
    )
    rows = [r.to_dict() for r in results]
    return {
        "notice": "SYNTHETIC: recovery of simulator ground truth; not real business results",
        "mode": "contamination" if fractions else "recovery",
        "data_version": report["data_version"],
        "world_config_hash": world.config_hash,
        "checks": rows,
        "failure_cases": failure_cases(report, units),
        "passed": all(r["passed"] for r in rows),
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
