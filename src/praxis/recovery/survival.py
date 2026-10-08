"""Time-to-collectible model: mixture-cure Weibull, interval-censored maximum likelihood.

For an episode with features z (intercept + reference-coded categories + standardised numerics):

    P(C <= t | z) = q(z) * F(t | z),   q = sigmoid(z . b_cure)
    F(t | z) = 1 - exp(-(t / lambda)^k),   lambda = exp(z . b_scale),   k = exp(s[reason])

``q`` is the probability that the payment ever becomes collectible ("cure"), F the time it
takes when it does. Retries only bracket C (``Episode.interval``), so each episode contributes

    q * (F(R) - F(L))        recovered: C in (L, R]
    1 - q * F(L)             right-censored at L (never recovered, or cut off by the extract)

Censoring is therefore handled by the likelihood itself, and the training cutoff needs no
special case. Only P(C <= t) for t inside the observed retry times is identified; ``q`` alone
is an extrapolation beyond the last retry and is never used for a decision on its own.

The gradient is analytic (tested against finite differences); fitting is deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize

from praxis.recovery.dataset import Episode
from praxis.recovery.features import METHODS, NUMERIC, REASONS, TIERS, RecoveryFeatures, encode

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]
NAME = "survival_cure_weibull"
_EPS = 1e-12


class FitError(RuntimeError):
    """The optimiser did not converge: no artifact is produced from this fit."""


def design_names() -> list[str]:
    return [
        "intercept",
        *(f"reason={r}" for r in REASONS[1:]),
        *(f"tier={t}" for t in TIERS[1:]),
        *(f"method={m}" for m in METHODS[1:]),
        *NUMERIC,
    ]


def _design(rows: Sequence[RecoveryFeatures], mean: F64, sd: F64) -> tuple[F64, I64]:
    full = encode(rows)
    nr, nt, nm = len(REASONS), len(TIERS), len(METHODS)
    cats = np.hstack(
        [full[:, 1:nr], full[:, nr + 1 : nr + nt], full[:, nr + nt + 1 : nr + nt + nm]]
    )
    numeric = (full[:, nr + nt + nm :] - mean) / sd
    z = np.hstack([np.ones((len(rows), 1)), cats, numeric])
    reason = np.argmax(full[:, :nr], axis=1).astype(np.int64)
    return z, reason


def _weibull(t: F64, log_scale: F64, log_shape: F64) -> tuple[F64, F64, F64]:
    """F(t), dF/dlog(lambda), dF/dlog(k); t may be 0 (F = 0) or inf (F = 1), both flat."""
    finite = np.isfinite(t) & (t > 0)
    safe_t = np.where(finite, t, 1.0)
    k = np.exp(log_shape)
    log_ratio = np.log(safe_t) - log_scale
    z = np.clip(k * log_ratio, -700.0, 50.0)  # u = exp(z); exp(-u) underflows to 0 beyond 50
    u = np.exp(z)
    u_surv = np.exp(z - u)  # u * exp(-u), computed without inf * 0
    cdf = np.where(finite, -np.expm1(-u), np.where(np.isinf(t), 1.0, 0.0))
    d_scale = np.where(finite, -k * u_surv, 0.0)
    d_shape = np.where(finite, k * log_ratio * u_surv, 0.0)
    return cdf, d_scale, d_shape


@dataclass
class CureWeibull:
    names: list[str]
    mean: F64
    sd: F64
    b_cure: F64
    b_scale: F64
    log_shape: F64  # one per reason, REASONS order
    converged: bool = False
    iterations: int = 0
    log_likelihood: float = math.nan

    name = NAME

    # ---------------------------------------------------------------- fitting
    @classmethod
    def fit(
        cls, episodes: Sequence[Episode], *, ridge: float, max_iter: int, gtol: float
    ) -> CureWeibull:
        if not episodes:
            raise FitError("no episodes to fit")
        raw = encode([e.features for e in episodes])[:, -len(NUMERIC) :]
        mean, sd = raw.mean(axis=0), raw.std(axis=0)
        sd = np.where(sd > 1e-9, sd, 1.0)
        z, reason = _design([e.features for e in episodes], mean, sd)
        bounds = np.array([e.interval for e in episodes], dtype=np.float64)
        p = z.shape[1]
        model = cls(design_names(), mean, sd, np.zeros(p), np.zeros(p), np.zeros(len(REASONS)))
        x0 = np.concatenate([np.zeros(p), np.full(p, 0.0), np.zeros(len(REASONS))])
        x0[p] = math.log(5.0)  # scale intercept: a few days

        def objective(theta: F64) -> tuple[float, F64]:
            return model._neg_loglik(theta, z, reason, bounds, ridge)

        res = minimize(
            objective,
            x0,
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": max_iter, "gtol": gtol, "maxcor": 30},
        )
        if not res.success:
            raise FitError(f"survival fit did not converge: {res.message}")
        model._set(res.x)
        model.converged = True
        model.iterations = int(res.nit)
        model.log_likelihood = float(-res.fun * len(episodes))
        return model

    def _set(self, theta: F64) -> None:
        p = len(self.names)
        self.b_cure = np.asarray(theta[:p], dtype=np.float64)
        self.b_scale = np.asarray(theta[p : 2 * p], dtype=np.float64)
        self.log_shape = np.asarray(theta[2 * p :], dtype=np.float64)

    def _neg_loglik(
        self, theta: F64, z: F64, reason: I64, bounds: F64, ridge: float
    ) -> tuple[float, F64]:
        p = z.shape[1]
        b_cure, b_scale, log_shape = theta[:p], theta[p : 2 * p], theta[2 * p :]
        eta = z @ b_cure
        q = 1.0 / (1.0 + np.exp(-eta))
        log_scale = z @ b_scale
        ls = log_shape[reason]
        lower, upper = bounds[:, 0], bounds[:, 1]
        f_lo, ds_lo, dk_lo = _weibull(lower, log_scale, ls)
        f_hi, ds_hi, dk_hi = _weibull(upper, log_scale, ls)
        recovered = np.isfinite(upper)

        mass = np.maximum(f_hi - f_lo, _EPS)
        surv = np.maximum(1.0 - q * f_lo, _EPS)
        ll = np.where(recovered, np.log(np.maximum(q, _EPS)) + np.log(mass), np.log(surv))
        # Where a clamp is active the objective is flat, so is its gradient (consistency).
        live_mass = (f_hi - f_lo) > _EPS
        live_surv = (1.0 - q * f_lo) > _EPS
        d_eta = np.where(recovered, 1.0 - q, np.where(live_surv, -q * (1.0 - q) * f_lo / surv, 0))
        d_lscale = np.where(
            recovered,
            np.where(live_mass, (ds_hi - ds_lo) / mass, 0.0),
            np.where(live_surv, -q * ds_lo / surv, 0.0),
        )
        d_lshape = np.where(
            recovered,
            np.where(live_mass, (dk_hi - dk_lo) / mass, 0.0),
            np.where(live_surv, -q * dk_lo / surv, 0.0),
        )

        n = len(z)
        penalty_mask = np.ones(p)
        penalty_mask[0] = 0.0
        # Ridge = a weak N(0, 1/ridge) prior on the SUMMED log likelihood; the objective is
        # divided by n only for numerical scale, so the prior does not grow with n.
        value = (
            -ll.sum()
            + 0.5 * ridge * (np.sum(penalty_mask * b_cure**2) + np.sum(penalty_mask * b_scale**2))
        ) / n
        grad = np.concatenate(
            [
                (-(z.T @ d_eta) + ridge * penalty_mask * b_cure) / n,
                (-(z.T @ d_lscale) + ridge * penalty_mask * b_scale) / n,
                -np.bincount(reason, weights=d_lshape, minlength=len(REASONS)) / n,
            ]
        )
        return float(value), grad

    # ------------------------------------------------------------- prediction
    def components(self, rows: Sequence[RecoveryFeatures]) -> tuple[F64, F64, F64]:
        """(q, log lambda, log k) per row."""
        z, reason = _design(rows, self.mean, self.sd)
        q = 1.0 / (1.0 + np.exp(-(z @ self.b_cure)))
        return q, z @ self.b_scale, self.log_shape[reason]

    def prob_collectible(self, rows: Sequence[RecoveryFeatures], t: F64) -> F64:
        """P(C <= t) for every row (n) and elapsed time (m): an (n, m) array."""
        q, log_scale, log_shape = self.components(rows)
        grid = np.broadcast_to(np.asarray(t, dtype=np.float64), (len(rows), len(t)))
        cdf, _, _ = _weibull(grid, log_scale[:, None], log_shape[:, None])
        out: F64 = q[:, None] * cdf
        return out

    def prob_at(self, rows: Sequence[RecoveryFeatures], t: F64) -> F64:
        """P(C <= t_i) for row i at its own elapsed time t_i: an (n,) array."""
        q, log_scale, log_shape = self.components(rows)
        cdf, _, _ = _weibull(np.asarray(t, dtype=np.float64), log_scale, log_shape)
        out: F64 = q * cdf
        return out

    # ------------------------------------------------------------ persistence
    def state(self) -> dict[str, Any]:
        return {
            "name": NAME,
            "names": self.names,
            "mean": self.mean.tolist(),
            "sd": self.sd.tolist(),
            "b_cure": self.b_cure.tolist(),
            "b_scale": self.b_scale.tolist(),
            "log_shape": self.log_shape.tolist(),
            "reasons": list(REASONS),
            "converged": self.converged,
            "iterations": self.iterations,
            "log_likelihood": self.log_likelihood,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> CureWeibull:
        if state.get("names") != design_names() or state.get("reasons") != list(REASONS):
            raise ValueError("survival state does not match this feature design")
        return cls(
            names=list(state["names"]),
            mean=np.asarray(state["mean"], dtype=np.float64),
            sd=np.asarray(state["sd"], dtype=np.float64),
            b_cure=np.asarray(state["b_cure"], dtype=np.float64),
            b_scale=np.asarray(state["b_scale"], dtype=np.float64),
            log_shape=np.asarray(state["log_shape"], dtype=np.float64),
            converged=bool(state["converged"]),
            iterations=int(state["iterations"]),
            log_likelihood=float(state["log_likelihood"]),
        )
