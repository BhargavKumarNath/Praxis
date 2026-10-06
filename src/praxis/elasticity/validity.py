"""Experiment validity checks (required_test.md s11 "Experiment validity").

Each check returns a ``Check``. ``gate=True`` checks must pass before any estimate is trusted
(the CLI refuses to publish an artifact otherwise); ``gate=False`` checks are reported flags.

Gates: sample-ratio mismatch, assignment audit (logged arm == recomputed arm), exposure
logging completeness, contamination / price errors, omnibus pre-treatment balance, missing
outcomes. Reported: per-covariate standardised mean differences, cross-test interference
(factorial interaction), guardrail metrics, and SRM among late (post-assignment) arrivals,
which is EXPECTED to be unbalanced when price affects conversion and is why late arrivals are
never eligible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import stats

from praxis.elasticity.config import ExperimentDesign, Validity
from praxis.elasticity.dataset import (
    ARM_MISMATCH,
    CONFLICTING,
    CONTAMINATED,
    EXPOSURE_MISSING,
    PRICE_ERROR,
    ExposureCensus,
    UnitTable,
)

F64 = NDArray[np.float64]


@dataclass(frozen=True)
class Check:
    name: str
    experiment: str
    passed: bool
    gate: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "experiment": self.experiment,
            "passed": self.passed,
            "gate": self.gate,
            "detail": self.detail,
        }


def srm_check(n_control: int, n_treatment: int, pi: float, alpha: float, exp: str) -> Check:
    """Chi-square goodness of fit of arm counts against the designed split."""
    total = n_control + n_treatment
    if total == 0:
        return Check("srm", exp, False, True, {"reason": "no eligible units"})
    expected = [total * (1.0 - pi), total * pi]
    p = float(stats.chisquare([n_control, n_treatment], f_exp=expected).pvalue)
    detail = {"control": n_control, "treatment": n_treatment, "expected_share": pi, "p": p}
    return Check("srm", exp, p >= alpha, True, detail)


def _status_count(units: UnitTable, *statuses: str) -> int:
    return int(np.isin(units.exposure_status, list(statuses)).sum())


def assignment_check(units: UnitTable, exp: str) -> Check:
    bad = _status_count(units, ARM_MISMATCH)
    return Check("assignment", exp, bad == 0, True, {"arm_mismatches": bad, "units": units.n})


def exposure_check(units: UnitTable, exp: str) -> Check:
    missing = _status_count(units, EXPOSURE_MISSING)
    conflicting = _status_count(units, CONFLICTING)
    detail = {"missing": missing, "conflicting": conflicting, "units": units.n}
    return Check("exposure_logging", exp, missing + conflicting == 0, True, detail)


def contamination_check(units: UnitTable, max_rate: float, exp: str) -> Check:
    """Units charged the other arm's price (by assigned arm) and prices matching no arm."""
    contaminated = units.exposure_status == CONTAMINATED
    by_arm = {}
    for arm, sel in (
        ("control", ~units.assigned_treatment),
        ("treatment", units.assigned_treatment),
    ):
        n = int(sel.sum())
        by_arm[arm] = {"units": n, "rate": float(contaminated[sel].mean()) if n else 0.0}
    price_errors = _status_count(units, PRICE_ERROR)
    worst = max(v["rate"] for v in by_arm.values())
    detail = {"by_arm": by_arm, "price_errors": price_errors}
    return Check("contamination", exp, worst <= max_rate and price_errors == 0, True, detail)


def covariates(units: UnitTable) -> tuple[F64, list[str]]:
    """Pre-treatment covariates: pre-period log demand, tenure flag, tier/industry/region."""
    cols: list[F64] = [units.pre_log_rate, units.is_existing.astype(float)]
    names = ["pre_log_rate", "is_existing"]
    for attr in ("tier", "industry", "region"):
        values = getattr(units, attr)
        for level in sorted(set(values.tolist()))[1:]:
            cols.append((values == level).astype(float))
            names.append(f"{attr}={level}")
    return np.stack(cols, axis=1), names


def balance_check(units: UnitTable, alpha: float, smd_flag: float, exp: str) -> Check:
    """Omnibus difference-in-means chi-square (gate) plus per-covariate SMD (reported)."""
    x, names = covariates(units)
    t = units.assigned_treatment
    if t.sum() < 2 or (~t).sum() < 2:
        return Check("balance", exp, False, True, {"reason": "fewer than two units in an arm"})
    xt, xc = x[t], x[~t]
    diff = xt.mean(axis=0) - xc.mean(axis=0)
    cov = np.atleast_2d(np.cov(xt, rowvar=False)) / len(xt) + np.atleast_2d(
        np.cov(xc, rowvar=False)
    ) / len(xc)
    stat = float(diff @ np.linalg.pinv(cov) @ diff)
    dof = int(np.linalg.matrix_rank(cov))
    p = float(stats.chi2.sf(stat, dof)) if dof > 0 else 1.0
    pooled_sd = np.sqrt((xt.var(axis=0, ddof=1) + xc.var(axis=0, ddof=1)) / 2.0)
    smd = np.divide(diff, pooled_sd, out=np.zeros_like(diff), where=pooled_sd > 0)
    flagged = [n for n, s in zip(names, smd.tolist(), strict=True) if abs(s) > smd_flag]
    detail = {
        "chi2": stat,
        "dof": dof,
        "p": p,
        "max_abs_smd": float(np.max(np.abs(smd))),
        "smd": dict(zip(names, smd.tolist(), strict=True)),
        "smd_flagged": flagged,
    }
    return Check("balance", exp, p >= alpha, True, detail)


def missing_outcome_check(units: UnitTable, v: Validity, exp: str) -> Check:
    t, miss = units.assigned_treatment, units.missing_outcome
    rate_t = float(miss[t].mean()) if t.any() else 0.0
    rate_c = float(miss[~t].mean()) if (~t).any() else 0.0
    table = [
        [int(miss[t].sum()), int((~miss[t]).sum())],
        [int(miss[~t].sum()), int((~miss[~t]).sum())],
    ]
    p = float(stats.fisher_exact(table).pvalue) if miss.any() else 1.0
    passed = max(rate_t, rate_c) <= v.max_missing_rate and abs(rate_t - rate_c) <= (
        v.max_missing_rate_diff
    )
    detail = {"rate_treatment": rate_t, "rate_control": rate_c, "difference_p": p}
    return Check("missing_outcomes", exp, passed, True, detail)


def late_arrival_srm(census: ExposureCensus, pi: float, alpha: float, exp: str) -> Check:
    """Reported only: post-assignment arrivals are selected by price (conversion)."""
    c, t = census.late_by_arm["control"], census.late_by_arm["treatment"]
    check = srm_check(c, t, pi, alpha, exp)
    return Check("late_arrival_srm", exp, check.passed, False, check.detail)


def ols_cluster(y: F64, x: F64, cluster: NDArray[np.generic]) -> tuple[F64, F64]:
    """OLS coefficients and CR1 cluster-robust covariance."""
    c = np.unique(cluster, return_inverse=True)[1]
    n, k = x.shape
    g = int(c.max()) + 1
    xtx_inv = np.linalg.pinv(x.T @ x)
    beta: F64 = xtx_inv @ x.T @ y
    scores = x * (y - x @ beta)[:, None]
    per_cluster = np.zeros((g, k))
    np.add.at(per_cluster, c, scores)
    correction = g / (g - 1) * (n - 1) / (n - k)
    cov: F64 = correction * xtx_inv @ (per_cluster.T @ per_cluster) @ xtx_inv
    return beta, cov


def interference_check(
    units: UnitTable, k: int, x_mean: dict[int, float], alpha: float, exp: str
) -> Check:
    """Does a test's effect depend on the customer's assignment in the OTHER tests?

    ``z`` = sum over the customer's other tests of (assigned log ratio - its design mean
    pi * log ratio), i.e. pure randomisation noise. In a factorial design ``z`` is independent
    of ``x`` and of every customer characteristic; a non-zero ``z`` slope is a cross-price
    effect and a non-zero ``x * z`` interaction means the tests interfere.

    The centring matters: the UNcentred sum has a mean set by which other tests the customer is
    eligible for (their product mix), which correlates with elasticity, so ``x * z`` would pick
    up heterogeneity and flag interference that does not exist (found on Phase 5 worlds).
    """
    ok = ~units.missing_outcome
    own = ok & (units.experiment == k)
    others = units.experiment != k
    z_by_customer: dict[str, float] = {}
    for cid, e, xa in zip(
        units.customer[others].tolist(),
        units.experiment[others].tolist(),
        units.x_assigned[others].tolist(),
        strict=True,
    ):
        z_by_customer[cid] = z_by_customer.get(cid, 0.0) + xa - x_mean[e]
    x = units.x_assigned[own]
    z = np.array([z_by_customer.get(cid, 0.0) for cid in units.customer[own].tolist()])
    if own.sum() < 10 or np.ptp(z) == 0:
        return Check("interference", exp, True, False, {"reason": "no cross-test variation"})
    design = np.stack([np.ones_like(x), x, z, x * z], axis=1)
    beta, cov = ols_cluster(units.delta[own], design, units.customer[own])
    se = np.sqrt(np.diag(cov))
    p = 2.0 * stats.norm.sf(np.abs(beta / np.where(se > 0, se, np.inf)))
    detail = {
        "cross_price_slope": float(beta[2]),
        "cross_price_p": float(p[2]),
        "interaction": float(beta[3]),
        "interaction_p": float(p[3]),
    }
    return Check("interference", exp, bool(p[2] >= alpha and p[3] >= alpha), False, detail)


def guardrails(units: UnitTable) -> dict[str, Any]:
    """Per-arm guardrail metrics (reported; Phase 6 guardrails consume the same quantities)."""
    out: dict[str, Any] = {}
    for arm, sel in (
        ("control", ~units.assigned_treatment),
        ("treatment", units.assigned_treatment),
    ):
        n = int(sel.sum())
        requested = float(units.post_requested[sel].sum())
        days = units.active_days[sel].astype(float)
        out[arm] = {
            "units": n,
            "churn_rate": float(units.churned_in_window[sel].mean()) if n else math.nan,
            "served_share": float(units.post_served[sel].sum()) / requested
            if requested
            else math.nan,
            "revenue_per_active_day_micros": (
                float(np.mean(units.post_value_micros[sel] / days)) if n else math.nan
            ),
        }
    c, t = out["control"], out["treatment"]
    out["churn_rate_difference"] = t["churn_rate"] - c["churn_rate"]
    rev_c = c["revenue_per_active_day_micros"]
    out["revenue_ratio"] = t["revenue_per_active_day_micros"] / rev_c if rev_c else math.nan
    return out


def experiment_checks(
    units: UnitTable,
    k: int,
    designs: tuple[ExperimentDesign, ...],
    census: ExposureCensus,
    v: Validity,
) -> list[Check]:
    """Every check for test ``k`` (``units`` = all eligible units of every test)."""
    design = designs[k]
    x_mean = {i: d.treated_fraction * d.log_ratio for i, d in enumerate(designs)}
    own = units.subset(units.experiment == k)
    n_t = int(own.assigned_treatment.sum())
    pi, exp = design.treated_fraction, design.id
    return [
        srm_check(own.n - n_t, n_t, pi, v.srm_alpha, exp),
        assignment_check(own, exp),
        exposure_check(own, exp),
        contamination_check(own, v.max_contamination_rate, exp),
        balance_check(own, v.balance_alpha, v.smd_flag, exp),
        missing_outcome_check(own, v, exp),
        late_arrival_srm(census, pi, v.srm_alpha, exp),
        interference_check(units, k, x_mean, v.interference_alpha, exp),
    ]
