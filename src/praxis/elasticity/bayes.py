"""Hierarchical Bayesian elasticity model (PyMC), two-stage over tier x industry cells.

Stage 1 (``estimators.within_slopes``) gives each cell an unpooled slope ``b_c`` with a
cluster-robust standard error ``se_c``. Stage 2 pools them toward an additive structure:

    b_c     ~ Normal(theta_c, se_c)
    theta_c ~ Normal(m_c, sigma_cell),   m_c = mu + a_tier[c] + b_industry[c]
    a_tier ~ ZeroSumNormal(tier_sd),  b_industry ~ ZeroSumNormal(industry_sd)   (fixed scales)
    sigma_cell ~ HalfNormal(cell_sd)                                            (learned)

The partial pooling is of the tier x industry INTERACTIONS: a cell borrows strength from its
tier and industry main effects, by an amount learned from the data (sigma_cell). Main-effect
scales are fixed, weakly informative priors: a between-group SD learned from three tiers or six
industries is barely identified, and on the development and synthetic worlds every learned-scale
variant funnelled (divergences) in one world or the other (ADR 0012).

Computation: theta_c is integrated out analytically, b_c ~ Normal(m_c, sqrt(se_c^2 +
sigma_cell^2)), and drawn afterwards from its exact conjugate normal conditional for every
posterior draw (Rao-Blackwellisation), so NUTS never sees the cell-level funnel.

A fit is accepted only if every diagnostic passes: R-hat, bulk and tail ESS, divergences,
posterior predictive p-values, and prior sensitivity of the important parameters (pooled and
tier elasticities). Sampling completing is not evidence of anything.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Sequence
from typing import Any

import arviz as az
import numpy as np
import pymc as pm
from numpy.typing import NDArray

from praxis.elasticity.config import Diagnostics, ElasticityConfig, Hierarchical, Priors
from praxis.elasticity.estimators import CellInput

F64 = NDArray[np.float64]
VAR_NAMES = ("mu", "sigma_cell", "a_tier", "b_industry")
_PPC_SEED_OFFSET = 1
_THETA_SEED_OFFSET = 2


def _sample(cells: Sequence[CellInput], priors: Priors, h: Hierarchical) -> tuple[Any, F64]:
    """NUTS over the hyperparameters; returns (inference data, theta draws [chain, draw, cell])."""
    tiers = sorted({c.tier for c in cells})
    inds = sorted({c.industry for c in cells})
    tier_idx = np.array([tiers.index(c.tier) for c in cells])
    ind_idx = np.array([inds.index(c.industry) for c in cells])
    b_obs = np.array([c.estimate for c in cells])
    se = np.array([c.se for c in cells])
    coords = {"tier": tiers, "industry": inds, "cell": [c.cell for c in cells]}
    with pm.Model(coords=coords):
        mu = pm.Normal("mu", priors.mu_mean, priors.mu_sd)
        s_c = pm.HalfNormal("sigma_cell", priors.cell_sd)
        a = pm.ZeroSumNormal("a_tier", sigma=priors.tier_sd, dims="tier")
        b = pm.ZeroSumNormal("b_industry", sigma=priors.industry_sd, dims="industry")
        pm.Deterministic("m", mu + a[tier_idx] + b[ind_idx], dims="cell")
        pm.Normal(
            "b_obs",
            mu + a[tier_idx] + b[ind_idx],
            pm.math.sqrt(se**2 + s_c**2),
            observed=b_obs,
        )
        logging.getLogger("pymc").setLevel(logging.ERROR)
        idata = pm.sample(
            draws=h.draws,
            tune=h.tune,
            chains=h.chains,
            cores=1,
            target_accept=h.target_accept,
            random_seed=h.seed,
            progressbar=False,
            compute_convergence_checks=False,
        )
    post = idata["posterior"]
    m = np.asarray(post["m"].values, dtype=np.float64)  # (chain, draw, cell)
    tau2 = np.asarray(post["sigma_cell"].values, dtype=np.float64)[..., None] ** 2
    precision = 1.0 / se**2 + 1.0 / tau2
    mean = (b_obs / se**2 + m / tau2) / precision
    rng = np.random.default_rng(h.seed + _THETA_SEED_OFFSET)
    theta: F64 = mean + rng.normal(size=mean.shape) / np.sqrt(precision)
    return idata, theta


def _flatten(theta: F64) -> F64:
    out: F64 = theta.reshape(-1, theta.shape[-1])
    return out


def summarise(draws: F64, interval: float) -> dict[str, float]:
    tail = (1.0 - interval) / 2.0
    lo, hi, lo95, hi95 = np.quantile(draws, [tail, 1.0 - tail, 0.025, 0.975]).tolist()
    return {
        "mean": float(draws.mean()),
        "sd": float(draws.std(ddof=1)),
        "interval_low": lo,
        "interval_high": hi,
        "ci95_low": lo95,
        "ci95_high": hi95,
        "p_negative": float((draws < 0).mean()),
    }


def aggregate(theta: F64, weights: F64, labels: Sequence[str]) -> dict[str, F64]:
    """Weighted average of cell draws per label (weights = design-variance unit weights)."""
    out: dict[str, F64] = {}
    lab = np.array(labels)
    for name in sorted(set(labels)):
        m = lab == name
        out[name] = theta[:, m] @ weights[m] / weights[m].sum()
    return out


def pairwise(groups: dict[str, F64]) -> list[dict[str, Any]]:
    rows = []
    for a, b in itertools.combinations(sorted(groups), 2):
        diff = groups[a] - groups[b]
        rows.append(
            {
                "a": a,
                "b": b,
                "mean_difference": float(diff.mean()),
                "sd_difference": float(diff.std(ddof=1)),
                "p_a_less_than_b": float((diff < 0).mean()),
            }
        )
    return rows


def _flat(tree: Any, names: Sequence[str]) -> F64:
    """Every element of every parameter's statistic (NaN propagates into min / max)."""
    return np.concatenate([np.asarray(tree[v].values, dtype=np.float64).ravel() for v in names])


def convergence(idata: Any, theta: F64) -> dict[str, float]:
    """R-hat and ESS over every sampled parameter and every cell elasticity draw."""
    names = list(VAR_NAMES)
    cells = az.from_dict({"posterior": {"theta": theta}})
    rhat = np.concatenate(
        [_flat(az.rhat(idata, var_names=names), names), _flat(az.rhat(cells), ["theta"])]
    )
    bulk = np.concatenate(
        [
            _flat(az.ess(idata, var_names=names, method="bulk"), names),
            _flat(az.ess(cells, method="bulk"), ["theta"]),
        ]
    )
    tail = np.concatenate(
        [
            _flat(az.ess(idata, var_names=names, method="tail"), names),
            _flat(az.ess(cells, method="tail"), ["theta"]),
        ]
    )
    return {
        "rhat_max": float(np.max(rhat)),
        "ess_bulk_min": float(np.min(bulk)),
        "ess_tail_min": float(np.min(tail)),
        "divergences": float(np.asarray(idata["sample_stats"]["diverging"].values).sum()),
    }


def posterior_predictive(theta: F64, cells: Sequence[CellInput], seed: int) -> dict[str, float]:
    """Replicate b ~ Normal(theta, se) per draw; p = P(T(rep) >= T(observed))."""
    b = np.array([c.estimate for c in cells])
    se = np.array([c.se for c in cells])
    rng = np.random.default_rng(seed)
    rep = theta + rng.normal(size=theta.shape) * se
    chi_obs = (((b - theta) / se) ** 2).sum(axis=1)
    chi_rep = (((rep - theta) / se) ** 2).sum(axis=1)
    return {
        "chi2_discrepancy": float((chi_rep >= chi_obs).mean()),
        "sd_across_cells": float((rep.std(axis=1) >= b.std()).mean()),
        "min_cell": float((rep.min(axis=1) >= b.min()).mean()),
        "max_cell": float((rep.max(axis=1) >= b.max()).mean()),
    }


def _important(theta: F64, weights: F64, tiers: Sequence[str]) -> dict[str, F64]:
    out = {f"tier:{k}": v for k, v in aggregate(theta, weights, tiers).items()}
    out["pooled"] = theta @ weights / weights.sum()
    return out


def _gates(
    conv: dict[str, float], ppc: dict[str, float], shift: float, d: Diagnostics
) -> dict[str, bool]:
    lo, hi = d.ppc_p_range
    return {
        "rhat": conv["rhat_max"] <= d.rhat_max,
        "ess_bulk": conv["ess_bulk_min"] >= d.ess_bulk_min,
        "ess_tail": conv["ess_tail_min"] >= d.ess_tail_min,
        "divergences": conv["divergences"] <= d.max_divergences,
        "posterior_predictive": all(lo <= p <= hi for p in ppc.values()),
        "prior_sensitivity": shift <= d.prior_sensitivity_max_shift_sd,
    }


def fit_hierarchical(
    cells: Sequence[CellInput], weights: F64, cfg: ElasticityConfig
) -> dict[str, Any]:
    """Fit, summarise and diagnose. ``weights`` = summed design weights per cell."""
    h = cfg.hierarchical
    idata, theta_chains = _sample(cells, h.priors, h)
    theta = _flatten(theta_chains)
    tiers = [c.tier for c in cells]
    inds = [c.industry for c in cells]
    tier_draws = aggregate(theta, weights, tiers)
    ind_draws = aggregate(theta, weights, inds)
    pooled = theta @ weights / weights.sum()
    conv = convergence(idata, theta_chains)
    ppc = posterior_predictive(theta, cells, h.seed + _PPC_SEED_OFFSET)

    base = _important(theta, weights, tiers)
    sensitivity: dict[str, Any] = {}
    worst = 0.0
    for name, alt in sorted(h.sensitivity.items()):
        alt_theta = _flatten(_sample(cells, alt, h)[1])
        shifts = {
            k: abs(float(v.mean() - base[k].mean())) / float(base[k].std(ddof=1))
            for k, v in _important(alt_theta, weights, tiers).items()
        }
        worst = max(worst, *shifts.values())
        sensitivity[name] = {"shift_in_posterior_sd": shifts}
    gates = _gates(conv, ppc, worst, cfg.diagnostics)
    return {
        "cells": {c.cell: summarise(theta[:, i], h.interval) for i, c in enumerate(cells)},
        "tier": {k: summarise(v, h.interval) for k, v in tier_draws.items()},
        "industry": {k: summarise(v, h.interval) for k, v in ind_draws.items()},
        "pooled": summarise(pooled, h.interval),
        "pairwise": {"tier": pairwise(tier_draws), "industry": pairwise(ind_draws)},
        "diagnostics": {
            "convergence": conv,
            "posterior_predictive_p": ppc,
            "prior_sensitivity": {"max_shift_sd": worst, "fits": sensitivity},
            "gates": gates,
            "passed": all(gates.values()),
        },
        "sampler": {
            "draws": h.draws,
            "tune": h.tune,
            "chains": h.chains,
            "target_accept": h.target_accept,
            "seed": h.seed,
            "interval": h.interval,
            "pymc": pm.__version__,
        },
    }
