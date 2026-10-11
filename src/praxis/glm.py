"""Poisson regression with an exposure offset (foundation numerics; pure numpy, deterministic).

Shared by the churn hazard (``praxis.customer``) and the causal churn price response
(``praxis.elasticity.churn``), which are independent siblings in the layer contract; the solver
lives here so neither imports the other.

    events_i ~ Poisson(exposure_i * exp(design_i . b))

Penalised Newton-Raphson (ridge ``penalty`` per coefficient, 0 = unpenalised) to a fixed step
tolerance, started at the overall log rate. Returns the model-based covariance (inverse
penalised information) and the robust sandwich (HC0) covariance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

F64 = NDArray[np.float64]


class FitError(RuntimeError):
    """The model could not be fitted (no person-time, divergence, no convergence)."""


@dataclass(frozen=True)
class PoissonFit:
    coef: F64
    cov: F64  # inverse penalised information (model-based)
    cov_robust: F64  # sandwich: I^-1 (sum s_i s_i') I^-1
    iterations: int
    log_likelihood: float


def fit_poisson(
    design: F64,
    events: F64,
    exposure: F64,
    *,
    penalty: F64 | None = None,
    start_column: int | None = 0,
    max_iter: int = 100,
    tol: float = 1e-10,
) -> PoissonFit:
    """``start_column``: the column started at the overall log rate (an intercept), if any."""
    if len(design) == 0 or float(exposure.sum()) <= 0 or np.any(exposure <= 0):
        raise FitError("no person-time to fit (exposure must be positive)")
    k = design.shape[1]
    pen = np.zeros(k) if penalty is None else np.asarray(penalty, dtype=np.float64)
    offset = np.log(exposure)
    b = np.zeros(k)
    if start_column is not None:
        b[start_column] = math.log((float(events.sum()) + 0.5) / float(exposure.sum()))
    for it in range(1, max_iter + 1):
        mu = np.exp(design @ b + offset)
        grad = design.T @ (events - mu) - pen * b
        info = design.T @ (design * mu[:, None]) + np.diag(pen)
        try:
            step = np.linalg.solve(info, grad)
        except np.linalg.LinAlgError as exc:
            raise FitError("singular information matrix") from exc
        b = b + step
        if not np.all(np.isfinite(b)):
            raise FitError("Newton iterations diverged")
        if float(np.max(np.abs(step))) < tol:
            return _finish(design, events, offset, pen, b, it)
    raise FitError(f"no convergence in {max_iter} iterations")


def _finish(design: F64, events: F64, offset: F64, pen: F64, b: F64, iterations: int) -> PoissonFit:
    eta = design @ b + offset
    mu = np.exp(eta)
    info = design.T @ (design * mu[:, None]) + np.diag(pen)
    inv = np.linalg.inv(info)
    scores = design * (events - mu)[:, None]
    robust = inv @ (scores.T @ scores) @ inv
    loglik = float(np.sum(events * eta - mu))
    return PoissonFit(b, inv, robust, iterations, loglik)
