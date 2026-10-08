"""Probability-quality and survival-assumption metrics (truth-free; observed outcomes only)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import stats

F64 = NDArray[np.float64]
_EPS = 1e-12


def brier(p: F64, y: F64) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(p: F64, y: F64) -> float:
    q = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def reliability(p: F64, y: F64, bins: int = 10) -> list[dict[str, float]]:
    """Equal-count bins of the predictions: mean prediction vs observed rate."""
    order = np.argsort(p, kind="stable")
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk) == 0:
            continue
        out.append(
            {
                "n": float(len(chunk)),
                "mean_predicted": float(p[chunk].mean()),
                "observed": float(y[chunk].mean()),
            }
        )
    return out


def ece(p: F64, y: F64, bins: int = 10) -> float:
    """Expected calibration error over equal-count bins (weighted mean |pred - obs|)."""
    table = reliability(p, y, bins)
    n = sum(b["n"] for b in table)
    return float(sum(b["n"] * abs(b["mean_predicted"] - b["observed"]) for b in table) / n)


def pr_auc(p: F64, y: F64) -> float:
    """Average precision (step-wise area under the precision-recall curve)."""
    positives = float(y.sum())
    if positives == 0:
        return math.nan
    order = np.argsort(-p, kind="stable")
    ys = y[order]
    tp = np.cumsum(ys)
    precision = tp / np.arange(1, len(ys) + 1)
    return float(np.sum(precision * ys) / positives)


def segment_calibration(
    p: F64,
    y: F64,
    segments: Sequence[str],
    *,
    min_rows: int,
    tolerance: float,
    z: float,
) -> list[dict[str, Any]]:
    """Per segment: |mean predicted - observed| vs max(tolerance, z * binomial SE)."""
    labels = np.asarray(segments)
    out = []
    for seg in sorted(set(segments)):
        mask = labels == seg
        n = int(mask.sum())
        if n < min_rows:
            continue
        pred, obs = float(p[mask].mean()), float(y[mask].mean())
        se = math.sqrt(max(pred * (1 - pred), _EPS) / n)
        allowed = max(tolerance, z * se)
        out.append(
            {
                "segment": seg,
                "n": n,
                "mean_predicted": pred,
                "observed": obs,
                "abs_error": abs(pred - obs),
                "allowed": allowed,
                "passed": abs(pred - obs) <= allowed,
            }
        )
    return out


def interval_concordance(risk_time: F64, lower: F64, upper: F64) -> float:
    """Harrell-type concordance for interval-censored times.

    ``risk_time`` is a predicted time scale (e.g. the predicted median time to collectible;
    larger = later). A pair (i, j) is comparable when i's interval lies entirely before j's
    (upper_i <= lower_j): i became collectible first. Concordant when risk_i < risk_j.
    """
    order = np.argsort(lower, kind="stable")
    lo_sorted = lower[order]
    concordant = 0.0
    comparable = 0.0
    for i in np.flatnonzero(np.isfinite(upper)).tolist():
        start = int(np.searchsorted(lo_sorted, upper[i], side="left"))
        later = order[start:]
        if len(later) == 0:
            continue
        r = risk_time[later]
        comparable += len(later)
        concordant += float(np.sum(r > risk_time[i]) + 0.5 * np.sum(r == risk_time[i]))
    return concordant / comparable if comparable else math.nan


def current_status_fit(
    predicted: F64,
    outcome: F64,
    reasons: Sequence[str],
    elapsed: F64,
    *,
    min_rows: int,
    tolerance: float,
    z: float,
) -> list[dict[str, Any]]:
    """Survival assumption check on first-retry rows.

    With randomised retry timing, the empirical success rate of first retries at elapsed e is
    a model-free (current-status) estimate of P(C <= e) for that cohort. Compare the model's
    mean prediction with it, cell by cell (reason x whole days of e).
    """
    keys = [f"{r}|{round(t)}" for r, t in zip(reasons, elapsed.tolist(), strict=True)]
    rows = segment_calibration(
        predicted, outcome, keys, min_rows=min_rows, tolerance=tolerance, z=z
    )
    for row in rows:
        reason, day = row.pop("segment").split("|")
        row["reason"], row["elapsed_days"] = reason, int(day)
    return rows


def uniform_assignment_p(values: Sequence[int], choices: Sequence[int]) -> float:
    """Chi-square goodness of fit of the observed gap counts to a uniform design."""
    counts = np.array([sum(1 for v in values if v == c) for c in choices], dtype=np.float64)
    if counts.sum() == 0:
        return math.nan
    return float(stats.chisquare(counts).pvalue)


def independence_p(groups: Sequence[str], values: Sequence[int]) -> float:
    """Chi-square test that the gap assignment is independent of the group (e.g. reason)."""
    g_levels = sorted(set(groups))
    v_levels = sorted(set(values))
    table = np.zeros((len(g_levels), len(v_levels)))
    for g, v in zip(groups, values, strict=True):
        table[g_levels.index(g), v_levels.index(v)] += 1
    table = table[table.sum(axis=1) > 0][:, table.sum(axis=0) > 0]
    if table.shape[0] < 2 or table.shape[1] < 2:
        return math.nan
    return float(stats.chi2_contingency(table).pvalue)
