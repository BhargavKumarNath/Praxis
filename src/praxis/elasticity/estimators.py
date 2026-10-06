"""Frequentist elasticity estimators on the unit table.

All estimators regress ``delta`` (the change in log demand rate, post vs pre) on the log price
ratio. Randomised assignment makes the slope a causal elasticity; the same regression on
observational price changes would not be (ADR 0012).

* ``within_slopes``: OLS with fixed effects absorbed by within-group demeaning (FWL) and one
  slope per segment; cluster-robust (CR1) standard errors by customer, t(G-1) intervals.
  Fixed-effect groups are nested in segments, so segment slopes are independent and each
  equals the design-variance-weighted mean of its tests' effects (the documented estimand).
* ``within_iv``: the same with the ASSIGNED log ratio instrumenting the log price actually
  charged (recovers the elasticity under non-compliance / contamination).
* ``naive_pre_post``: treated units only, no control group. Deliberately confounded by the
  common time trend; reported as a failure case, never used for decisions.
* ``empirical_bayes``: partial pooling of cell slopes toward an additive tier + industry fit
  (Paule-Mandel between-cell variance). A closed-form cross-check of the hierarchical model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import stats

F64 = NDArray[np.float64]
_EPS = 1e-12


@dataclass(frozen=True)
class SlopeEstimate:
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    n_units: int
    n_clusters: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _group_codes(*keys: NDArray[np.generic]) -> NDArray[np.int64]:
    stacked = np.stack([np.unique(k, return_inverse=True)[1] for k in keys], axis=1)
    codes: NDArray[np.int64] = np.unique(stacked, axis=0, return_inverse=True)[1].reshape(-1)
    return codes


def demean(values: F64, groups: NDArray[np.int64]) -> F64:
    """Subtract the group mean (absorbs one fixed effect per group)."""
    n_g = int(groups.max()) + 1 if groups.size else 0
    sums = np.bincount(groups, weights=values, minlength=n_g)
    counts = np.bincount(groups, minlength=n_g)
    out: F64 = values - (sums / np.maximum(counts, 1))[groups]
    return out


def _cluster_meat(score: F64, cluster: NDArray[np.int64]) -> float:
    per_cluster = np.bincount(cluster, weights=score)
    return float(np.sum(per_cluster**2))


def _t_interval(est: float, se: float, n_clusters: int, level: float) -> tuple[float, float]:
    q = float(stats.t.ppf(0.5 + level / 2.0, df=max(n_clusters - 1, 1)))
    return est - q * se, est + q * se


def within_slopes(
    y: F64,
    x: F64,
    fe: Sequence[NDArray[np.generic]],
    segment: NDArray[np.generic],
    cluster: NDArray[np.generic],
    *,
    ci_level: float,
    min_units: int = 2,
) -> dict[str, SlopeEstimate]:
    """One slope of ``y`` on ``x`` per segment, fixed effects = ``fe`` x segment."""
    groups = _group_codes(*fe, segment)
    clusters = np.unique(cluster, return_inverse=True)[1]
    out: dict[str, SlopeEstimate] = {}
    for seg in np.unique(segment).tolist():
        m = segment == seg
        est = _slope(y[m], x[m], groups[m], clusters[m], ci_level, min_units)
        if est is not None:
            out[str(seg)] = est
    return out


def _slope(
    y: F64,
    x: F64,
    groups: NDArray[np.int64],
    clusters: NDArray[np.int64],
    ci_level: float,
    min_units: int,
) -> SlopeEstimate | None:
    g = np.unique(groups, return_inverse=True)[1]
    c = np.unique(clusters, return_inverse=True)[1]
    n, n_cl, n_fe = y.size, int(c.max()) + 1 if c.size else 0, int(g.max()) + 1 if g.size else 0
    xt, yt = demean(x, g), demean(y, g)
    sxx = float(xt @ xt)
    if n < min_units or n_cl < 2 or sxx < _EPS or n - n_fe - 1 < 1:
        return None
    beta = float(xt @ yt) / sxx
    resid = yt - beta * xt
    k = n_fe + 1
    correction = n_cl / (n_cl - 1) * (n - 1) / (n - k)
    se = math.sqrt(correction * _cluster_meat(xt * resid, c)) / sxx
    lo, hi = _t_interval(beta, se, n_cl, ci_level)
    return SlopeEstimate(beta, se, lo, hi, n, n_cl)


@dataclass(frozen=True)
class IVEstimate:
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    first_stage: float  # d(log price charged) / d(assigned log ratio)
    first_stage_f: float
    n_units: int
    n_clusters: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def within_iv(
    y: F64,
    d: F64,
    z: F64,
    fe: Sequence[NDArray[np.generic]],
    cluster: NDArray[np.generic],
    *,
    ci_level: float,
) -> IVEstimate | None:
    """Just-identified IV (Wald / 2SLS) with absorbed fixed effects and cluster-robust SE."""
    g = _group_codes(*fe)
    c = np.unique(cluster, return_inverse=True)[1]
    yt, dt, zt = demean(y, g), demean(d, g), demean(z, g)
    szd, szz = float(zt @ dt), float(zt @ zt)
    n_cl = int(c.max()) + 1 if c.size else 0
    if n_cl < 2 or abs(szd) < _EPS or szz < _EPS:
        return None
    beta = float(zt @ yt) / szd
    resid = yt - beta * dt
    correction = n_cl / (n_cl - 1)
    se = math.sqrt(correction * _cluster_meat(zt * resid, c)) / abs(szd)
    pi = szd / szz
    pi_resid = dt - pi * zt
    pi_se = math.sqrt(correction * _cluster_meat(zt * pi_resid, c)) / szz
    lo, hi = _t_interval(beta, se, n_cl, ci_level)
    f_stat = (pi / pi_se) ** 2 if pi_se > 0 else math.inf
    return IVEstimate(beta, se, lo, hi, pi, f_stat, int(y.size), n_cl)


def naive_pre_post(delta_treated: F64, log_ratio: float, *, ci_level: float) -> SlopeEstimate:
    """Before/after on treated units only: attributes the whole change to the price."""
    n = int(delta_treated.size)
    if n < 2:
        raise ValueError("naive pre/post needs at least two treated units")
    est = float(delta_treated.mean()) / log_ratio
    se = float(delta_treated.std(ddof=1)) / math.sqrt(n) / abs(log_ratio)
    lo, hi = _t_interval(est, se, n, ci_level)
    return SlopeEstimate(est, se, lo, hi, n, n)


@dataclass(frozen=True)
class CellInput:
    """Unpooled cell slope plus its tier and industry labels."""

    cell: str
    tier: str
    industry: str
    estimate: float
    se: float


@dataclass(frozen=True)
class PooledCell:
    cell: str
    estimate: float
    sd: float
    shrinkage: float  # weight on the additive fit (0 = no pooling, 1 = complete pooling)


def _design(cells: Sequence[CellInput]) -> F64:
    tiers = sorted({c.tier for c in cells})
    inds = sorted({c.industry for c in cells})
    cols = [np.ones(len(cells))]
    cols += [np.array([c.tier == t for c in cells], dtype=float) for t in tiers[1:]]
    cols += [np.array([c.industry == i for c in cells], dtype=float) for i in inds[1:]]
    return np.stack(cols, axis=1)


def _wls(b: F64, x: F64, w: F64) -> F64:
    sw = np.sqrt(w)
    coef: F64 = np.linalg.lstsq(x * sw[:, None], b * sw, rcond=None)[0]
    return coef


def empirical_bayes(cells: Sequence[CellInput]) -> tuple[list[PooledCell], float]:
    """Shrink cell slopes toward an additive tier + industry fit; returns (cells, tau)."""
    b = np.array([c.estimate for c in cells])
    v = np.array([c.se for c in cells]) ** 2
    x = _design(cells)
    dof = len(cells) - np.linalg.matrix_rank(x)

    def q(tau2: float) -> float:
        w = 1.0 / (v + tau2)
        r = b - x @ _wls(b, x, w)
        return float(np.sum(w * r**2))

    tau2 = 0.0
    if dof > 0 and q(0.0) > dof:
        lo, hi = 0.0, float(np.var(b)) * 10.0 + 1.0
        for _ in range(200):  # Paule-Mandel: Q(tau^2) = dof, Q decreasing in tau^2
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if q(mid) > dof else (lo, mid)
        tau2 = 0.5 * (lo + hi)
    w = 1.0 / (v + tau2)
    fit = x @ _wls(b, x, w)
    shrink = v / (v + tau2)
    post = fit + (1.0 - shrink) * (b - fit)
    # Morris (1983): conditional variance plus the uncertainty of the fitted additive part.
    cov_fit = x @ np.linalg.pinv(x.T @ (x * w[:, None])) @ x.T
    sd = np.sqrt((1.0 - shrink) * v + shrink**2 * np.diag(cov_fit))
    out = [
        PooledCell(c.cell, float(post[i]), float(sd[i]), float(shrink[i]))
        for i, c in enumerate(cells)
    ]
    return out, math.sqrt(tau2)
