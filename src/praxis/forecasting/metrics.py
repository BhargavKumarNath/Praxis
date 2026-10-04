"""Forecast metrics. Pure functions over arrays; no model or I/O knowledge.

Value weights make errors comparable across products: ``w`` is GBP per unit of the row's
product. Weighted errors are normalised by ``sum(w * y)`` so they read as fractions of
the value forecast (vWAPE) and stay comparable across slices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.forecasting.models import Prediction

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]


def vwape(y: F64, yhat: F64, w: F64) -> float:
    denom = float((w * y).sum())
    return float((w * np.abs(y - yhat)).sum()) / denom if denom > 0 else float("nan")


def pinball(y: F64, q: F64, level: float) -> F64:
    diff = y - q
    out: F64 = np.maximum(level * diff, (level - 1.0) * diff)
    return out


def weighted_pinball(y: F64, pred: Prediction, w: F64) -> float:
    """Mean over levels of value-weighted pinball loss, normalised like vWAPE."""
    denom = float((w * y).sum())
    if denom <= 0:
        return float("nan")
    losses = [float((w * pinball(y, pred.quantile(a), a)).sum()) for a in pred.levels]
    return float(np.mean(losses)) / denom


def coverage(y: F64, lo: F64, hi: F64) -> float:
    return float(np.mean((y >= lo) & (y <= hi))) if y.size else float("nan")


def summarize(y: F64, pred: Prediction, w: F64) -> dict[str, float]:
    """Every reported metric for one set of rows."""
    out = {
        "rows": float(y.size),
        "vwape": vwape(y, pred.point, w),
        "mae": float(np.mean(np.abs(y - pred.point))),
        "rmse": float(np.sqrt(np.mean((y - pred.point) ** 2))),
        "bias": float((w * (pred.point - y)).sum() / max(float((w * y).sum()), 1e-12)),
        "pinball": weighted_pinball(y, pred, w),
    }
    levels = pred.levels
    for lo, hi, label in ((0.25, 0.75, "50"), (0.1, 0.9, "80"), (0.05, 0.95, "90")):
        if lo in levels and hi in levels:
            out[f"coverage_{label}"] = coverage(y, pred.quantile(lo), pred.quantile(hi))
    return out


def take(pred: Prediction, mask: NDArray[np.bool_]) -> Prediction:
    return Prediction(pred.point[mask], pred.quantiles[mask], pred.levels, 0)


def sliced(
    y: F64, pred: Prediction, w: F64, groups: Mapping[str, NDArray[Any]]
) -> dict[str, dict[str, dict[str, float]]]:
    """``{group: {label: metrics}}`` for each grouping column (e.g. region, horizon)."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for group, labels in groups.items():
        out[group] = {}
        for label in sorted({str(v) for v in labels.tolist()}):
            mask = np.array([str(v) == label for v in labels.tolist()])
            out[group][label] = summarize(y[mask], take(pred, mask), w[mask])
    return out


def bootstrap_vwape_difference(
    y: F64,
    a: F64,
    b: F64,
    w: F64,
    blocks: I64,
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    """Block bootstrap of ``vwape(a) - vwape(b)``, resampling whole blocks (forecast origins).

    Rows sharing an origin are correlated (same week, same shocks), so resampling rows
    would overstate certainty.
    """
    ids = np.unique(blocks)
    pos = {int(b_): i for i, b_ in enumerate(ids.tolist())}
    block_of = np.array([pos[int(v)] for v in blocks.tolist()])
    num_a = np.bincount(block_of, weights=w * np.abs(y - a), minlength=ids.size)
    num_b = np.bincount(block_of, weights=w * np.abs(y - b), minlength=ids.size)
    den = np.bincount(block_of, weights=w * y, minlength=ids.size)
    rng = np.random.Generator(np.random.PCG64(seed))
    draws = rng.integers(0, ids.size, size=(samples, ids.size))
    diffs = (num_a[draws].sum(axis=1) - num_b[draws].sum(axis=1)) / den[draws].sum(axis=1)
    return {
        "estimate": vwape(y, a, w) - vwape(y, b, w),
        "ci95_low": float(np.quantile(diffs, 0.025)),
        "ci95_high": float(np.quantile(diffs, 0.975)),
        "blocks": float(ids.size),
        "samples": float(samples),
    }
