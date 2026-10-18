"""Distribution drift statistics (pure): reference profiles and the population stability index.

PSI = sum_b (p_b - q_b) * ln(p_b / q_b) over bins fixed on the reference sample (deciles by
default). Rule of thumb: < 0.1 stable, 0.1-0.25 moderate, > 0.25 a material shift. Bins are
fixed at registration time and stored with the version, so later windows are compared with
exactly the same edges. Empty bins get a small floor so the index stays finite.

PSI is noisy on small samples: with no change at all, n * m / (n + m) * PSI is approximately
chi-square with B - 1 degrees of freedom. A shift therefore fires only when the PSI exceeds
BOTH the materiality threshold and the no-change bound chi2_{1-alpha}(B - 1) * (1/n + 1/m).
The sample sizes are EFFECTIVE sizes: the number of distinct finite values, since a region's
context value is repeated for every series of that region and must not count many times.
The approximation needs about five expected observations per bin, so a column with fewer
than ``MIN_PER_BIN * bins`` effective observations on either side is reported as
``insufficient`` and never fires.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.stats import chi2

F64 = NDArray[np.float64]
FLOOR = 1e-4
MIN_PER_BIN = 5


def bin_edges(values: F64, bins: int) -> list[float]:
    """Interior quantile edges of the finite reference values (deduplicated)."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return []
    qs = np.quantile(finite, np.linspace(0.0, 1.0, bins + 1)[1:-1])
    return sorted({float(q) for q in qs})


def proportions(values: F64, edges: Sequence[float]) -> list[float]:
    finite = values[np.isfinite(values)]
    counts = np.bincount(
        np.searchsorted(np.asarray(edges, dtype=np.float64), finite, side="right"),
        minlength=len(edges) + 1,
    ).astype(np.float64)
    total = counts.sum()
    return [float(c) for c in (counts / total if total > 0 else counts)]


def psi(reference: Sequence[float], current: Sequence[float]) -> float:
    p = np.maximum(np.asarray(reference, dtype=np.float64), FLOOR)
    q = np.maximum(np.asarray(current, dtype=np.float64), FLOOR)
    if p.shape != q.shape:
        raise ValueError("reference and current must use the same bins")
    return float(np.sum((q - p) * np.log(q / p)))


def effective_n(values: F64) -> int:
    """Distinct finite values: repeated copies of one observation count once."""
    finite = values[np.isfinite(values)]
    return int(np.unique(finite).size)


def noise_bound(bins: int, n_reference: int, n_current: int, alpha: float) -> float:
    """PSI a no-change comparison exceeds with probability ~alpha (chi-square approximation)."""
    if bins < 2 or n_reference < 1 or n_current < 1:
        return float("inf")
    return float(chi2.ppf(1.0 - alpha, bins - 1) * (1.0 / n_reference + 1.0 / n_current))


def profile(columns: Mapping[str, F64], bins: int) -> dict[str, dict[str, Any]]:
    """Reference profile: per column, the bin edges, proportions and effective size."""
    out = {}
    for name, values in sorted(columns.items()):
        edges = bin_edges(values, bins)
        out[name] = {
            "edges": edges,
            "reference": proportions(values, edges),
            "n": effective_n(values),
        }
    return out


def compare(
    reference: Mapping[str, Any],
    columns: Mapping[str, F64],
    *,
    threshold: float,
    alpha: float,
) -> dict[str, Any]:
    """PSI per profiled column; ``fired`` = above the threshold AND the no-change bound."""
    values, bounds, insufficient = {}, {}, []
    for name, ref in sorted(reference.items()):
        if name not in columns:
            continue
        bins, n_ref, n_cur = len(ref["reference"]), int(ref["n"]), effective_n(columns[name])
        if min(n_ref, n_cur) < MIN_PER_BIN * bins:
            insufficient.append(name)
            continue
        current = proportions(columns[name], ref["edges"])
        values[name] = round(psi(ref["reference"], current), 6)
        bounds[name] = round(noise_bound(bins, n_ref, n_cur, alpha), 6)
    fired = sorted(n for n, v in values.items() if v > threshold and v > bounds[n])
    return {
        "psi": values,
        "noise_bound": bounds,
        "insufficient": insufficient,
        "threshold": threshold,
        "fired": fired,
    }
