"""Churn hazard models (piecewise-constant daily hazards; pure numpy, deterministic).

* ``PoissonHazard``: a Poisson GLM with a log exposure offset, i.e. a piecewise-exponential
  proportional-hazards model over non-overlapping snapshot windows. Ridge on the standardised
  slopes, Newton-Raphson to a fixed tolerance (``praxis.glm``; no randomness, no solver
  dependency). The covariance is the inverse penalised information (Laplace approximation).
* ``RateTable``: the baseline. One daily hazard per segment (tier), pooled toward the overall
  rate with a prior weight in person-days. Also used for involuntary churn.

Both predict a DAILY hazard per customer; ``deviance`` scores them on the same footing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from praxis import glm

F64 = NDArray[np.float64]


class HazardModel(Protocol):
    @property
    def name(self) -> str: ...

    def predict(self, x: F64, tiers: Sequence[str]) -> F64:
        """Daily hazard per row."""
        ...


class FitError(RuntimeError):
    """The model could not be fitted (no data, no exposure, divergence)."""


@dataclass(frozen=True)
class PoissonHazard:
    features: tuple[str, ...]
    mean: F64  # standardisation (training means; NaN inputs are imputed to them)
    scale: F64
    coef: F64  # intercept first, then one slope per standardised feature
    cov: F64  # inverse penalised information
    iterations: int
    name: str = "poisson_glm"

    def design(self, x: F64) -> F64:
        z = (x - self.mean) / self.scale
        z = np.where(np.isfinite(z), z, 0.0)
        out: F64 = np.column_stack([np.ones(len(x)), z])
        return out

    def predict(self, x: F64, tiers: Sequence[str] = ()) -> F64:
        out: F64 = np.exp(self.design(x) @ self.coef)
        return out

    def state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "features": list(self.features),
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "coef": self.coef.tolist(),
            "cov": self.cov.tolist(),
            "iterations": self.iterations,
        }

    @classmethod
    def from_state(cls, s: dict[str, Any]) -> PoissonHazard:
        if s.get("name") != "poisson_glm":
            raise ValueError("not a poisson_glm state")
        return cls(
            tuple(s["features"]),
            np.array(s["mean"], dtype=np.float64),
            np.array(s["scale"], dtype=np.float64),
            np.array(s["coef"], dtype=np.float64),
            np.array(s["cov"], dtype=np.float64),
            int(s["iterations"]),
        )


def fit_poisson(
    x: F64,
    events: F64,
    exposure: F64,
    features: Sequence[str],
    *,
    ridge: float,
    max_iter: int,
    tol: float,
) -> PoissonHazard:
    """Penalised maximum likelihood of ``events ~ Poisson(exposure * exp(b0 + z b))``."""
    if len(x) == 0 or float(exposure.sum()) <= 0:
        raise FitError("no person-time to fit")
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(x, axis=0) if np.isfinite(x).any() else np.zeros(x.shape[1])
        sd = np.nanstd(x, axis=0) if np.isfinite(x).any() else np.ones(x.shape[1])
    mean = np.where(np.isfinite(mean), mean, 0.0)
    scale = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
    model = PoissonHazard(tuple(features), mean, scale, np.zeros(x.shape[1] + 1), np.eye(1), 0)
    design = model.design(x)
    penalty = np.full(design.shape[1], ridge)
    penalty[0] = 0.0
    try:
        fit = glm.fit_poisson(design, events, exposure, penalty=penalty, max_iter=max_iter, tol=tol)
    except glm.FitError as exc:
        raise FitError(str(exc)) from exc
    return PoissonHazard(tuple(features), mean, scale, fit.coef, fit.cov, fit.iterations)


@dataclass(frozen=True)
class RateTable:
    """Daily hazard per segment, shrunk toward the overall rate by ``prior_weight`` person-days."""

    rates: dict[str, float]
    overall: float
    prior_weight: float
    name: str = "tier_rate_table"

    def predict(self, x: F64, tiers: Sequence[str]) -> F64:
        return np.array([self.rates.get(t, self.overall) for t in tiers], dtype=np.float64)

    def state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rates": dict(sorted(self.rates.items())),
            "overall": self.overall,
            "prior_weight": self.prior_weight,
        }

    @classmethod
    def from_state(cls, s: dict[str, Any]) -> RateTable:
        return cls(
            {str(k): float(v) for k, v in s["rates"].items()},
            float(s["overall"]),
            float(s["prior_weight"]),
            str(s.get("name", "tier_rate_table")),
        )


def fit_rate_table(
    tiers: Sequence[str],
    events: F64,
    exposure: F64,
    *,
    prior_weight: float,
    name: str = "tier_rate_table",
) -> RateTable:
    total_e = float(exposure.sum())
    if total_e <= 0:
        raise FitError("no person-time to fit")
    overall = float(events.sum()) / total_e
    sums: dict[str, list[float]] = {}
    for t, y, e in zip(tiers, events.tolist(), exposure.tolist(), strict=True):
        acc = sums.setdefault(t, [0.0, 0.0])
        acc[0] += y
        acc[1] += e
    rates = {
        t: (y + prior_weight * overall) / (e + prior_weight) for t, (y, e) in sorted(sums.items())
    }
    return RateTable(rates, overall, prior_weight, name)


def deviance(events: F64, expected: F64) -> float:
    """Poisson deviance of ``expected`` counts (lower is better)."""
    mu = np.maximum(expected, 1e-300)
    term = np.where(events > 0, events * np.log(np.where(events > 0, events, 1.0) / mu), 0.0)
    return float(2.0 * np.sum(term - (events - mu)))
