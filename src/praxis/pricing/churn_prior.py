"""Meta-analytic-predictive prior for the churn response (ADR 0016 third amendment).

One 28-day test measures a product's bill response to about +-1.4 (pooled +-0.9) around a
response of ~1.5-2: a single test is too noisy to decide on with a flat prior. Conditional on
the system ACTING, the flat-prior posterior is biased: raises clear the confidence bar mainly
in worlds whose test happened to read low (development: the churn of the changed products
was under-predicted ~2.5x; held-out seed 202: pooled 0.05 +- 0.84). The established remedy
is to borrow strength from earlier experiments of the same kind (meta-analytic-predictive,
MAP, prior: Neuenschwander et al. 2010; robust MAP: Schmidli et al. 2014, Biometrics 70):

1. **Archive** (``build_archive``): the observable estimates (gamma, SE per product) of
   earlier churn-response documents, each verified by its own checksum. Never simulator truth.
   The experiment being decided on is refused if it is in the archive (no double counting).
2. **Predictive prior** per product (``predictive_prior``): normal random-effects
   meta-analysis of the archived estimates, flat mean, between-experiment SD tau ~
   half-Cauchy(tau_scale) integrated on a grid; the prior for a NEW experiment's true response
   is the predictive N(E[mu], Var(mu) + E[tau^2]) (the prior for theta_new, not for mu).
3. **Robust mixture** (``robust_posterior``): prior = (1 - w) MAP + w N(MAP mean, vague_sd^2),
   so a new experiment that conflicts with the archive moves the posterior toward its own data
   instead of being overruled. The 2-component posterior is summarised by its mean and SD;
   under conflict that SD is wide, so the decision freezes rather than trusting either side.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

ARCHIVE_FORMAT = 1


class ChurnPriorError(RuntimeError):
    """The archive is missing, corrupt or inconsistent with the evidence it should inform."""


def checksum(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "checksum"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def build_archive(docs: list[tuple[str, dict[str, Any]]], *, verify: Any) -> dict[str, Any]:
    """Archive of churn-response documents ``(label, doc)``; ``verify(doc)`` checks each.

    All documents must share the method, format and experiment registry; duplicates (same
    checksum) are refused.
    """
    if not docs:
        raise ChurnPriorError("an archive needs at least one churn-response document")
    keys = {(d.get("method"), d.get("response_format"), d.get("registry_hash")) for _, d in docs}
    if len(keys) != 1:
        raise ChurnPriorError("archived experiments must share method, format and registry")
    seen: set[str] = set()
    experiments = []
    for label, doc in docs:
        verify(doc)
        if doc["checksum"] in seen:
            raise ChurnPriorError(f"duplicate experiment in the archive: {label}")
        seen.add(doc["checksum"])
        experiments.append(
            {
                "source": label,
                "churn_response_checksum": doc["checksum"],
                "products": {
                    p: {"gamma": float(v["gamma"]), "se": float(v["se"])}
                    for p, v in sorted(doc["products"].items())
                },
            }
        )
    method, fmt, registry = next(iter(keys))
    out: dict[str, Any] = {
        "archive_format": ARCHIVE_FORMAT,
        "is_synthetic": True,
        "method": method,
        "response_format": fmt,
        "registry_hash": registry,
        "experiments": experiments,
    }
    out["checksum"] = checksum(out)
    return out


@dataclass(frozen=True)
class ChurnArchive:
    checksum: str
    registry_hash: str
    sources: frozenset[str]  # churn-response checksums
    estimates: dict[str, tuple[list[float], list[float]]]  # product -> (gammas, SEs)


def load_archive(path: Path) -> ChurnArchive:
    try:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChurnPriorError(f"churn archive unusable: {exc}") from exc
    if doc.get("archive_format") != ARCHIVE_FORMAT or checksum(doc) != doc.get("checksum"):
        raise ChurnPriorError("churn archive format or checksum mismatch")
    estimates: dict[str, tuple[list[float], list[float]]] = {}
    for exp in doc["experiments"]:
        for product, v in exp["products"].items():
            g, s = estimates.setdefault(product, ([], []))
            g.append(float(v["gamma"]))
            s.append(float(v["se"]))
    return ChurnArchive(
        str(doc["checksum"]),
        str(doc["registry_hash"]),
        frozenset(str(e["churn_response_checksum"]) for e in doc["experiments"]),
        estimates,
    )


F64 = NDArray[np.float64]


@dataclass(frozen=True)
class TauPosterior:
    """Normal random effects y_j ~ N(theta_j, v_j), theta_j ~ N(mu, tau^2), flat mu,
    tau ~ half-Cauchy(scale), on a deterministic tau grid (BDA3 s5.4). Given tau, mu is normal
    N(mu_hat, v_mu); ``weight`` is the posterior of tau on the grid."""

    tau: F64
    weight: F64
    mu: F64  # E[mu | tau]
    v_mu: F64  # Var(mu | tau)


def tau_posterior(
    estimates: list[float], ses: list[float], *, tau_scale: float, grid: int = 4000
) -> TauPosterior:
    y = np.asarray(estimates, dtype=np.float64)
    v = np.asarray(ses, dtype=np.float64) ** 2
    if len(y) == 0 or np.any(v <= 0) or not np.all(np.isfinite(y)):
        raise ChurnPriorError("random-effects pooling needs finite estimates with SEs > 0")
    if tau_scale <= 0:
        raise ChurnPriorError("tau prior scale must be positive")
    upper = 20.0 * max(tau_scale, float(np.sqrt(v.max())), float(np.ptp(y)))
    tau = np.linspace(upper / grid, upper, grid)  # tau > 0 (density finite at 0)
    w = 1.0 / (v[None, :] + tau[:, None] ** 2)  # (g, k)
    v_mu = 1.0 / w.sum(axis=1)
    mu = v_mu * (w * y[None, :]).sum(axis=1)
    log_post = (
        np.log(1.0 / (1.0 + (tau / tau_scale) ** 2))  # half-Cauchy prior (unnormalised)
        + 0.5 * np.log(v_mu)
        + 0.5 * np.log(w).sum(axis=1)
        - 0.5 * (w * (y[None, :] - mu[:, None]) ** 2).sum(axis=1)
    )
    weight = np.exp(log_post - log_post.max())
    return TauPosterior(tau, weight / weight.sum(), mu, v_mu)


def predictive_prior(
    estimates: list[float], ses: list[float], *, tau_scale: float
) -> tuple[float, float]:
    """(mean, SD) of a NEW experiment's true response given the archived estimates."""
    if len(estimates) < 2:
        raise ChurnPriorError("a predictive prior needs >= 2 archived estimates")
    post = tau_posterior(estimates, ses, tau_scale=tau_scale)
    mean = float((post.weight * post.mu).sum())
    # theta_new = mu + tau eps: E[Var | tau] + Var(E | tau)
    var = float((post.weight * (post.v_mu + post.tau**2 + (post.mu - mean) ** 2)).sum())
    return mean, math.sqrt(var)


def robust_posterior(
    y: float,
    se: float,
    prior_mean: float,
    prior_sd: float,
    *,
    vague_weight: float,
    vague_sd: float,
) -> tuple[float, float]:
    """(mean, SD) of the posterior of theta given y ~ N(theta, se^2) and the robust MAP prior."""
    if se <= 0 or prior_sd <= 0 or vague_sd <= 0 or not 0 <= vague_weight < 1:
        raise ChurnPriorError("robust posterior needs positive SDs and a weight in [0, 1)")
    comps = ((1.0 - vague_weight, prior_mean, prior_sd), (vague_weight, prior_mean, vague_sd))
    logs, means, variances = [], [], []
    for w, m, s in comps:
        if w <= 0:
            continue
        total = s * s + se * se
        logs.append(math.log(w) - 0.5 * math.log(total) - 0.5 * (y - m) ** 2 / total)
        post_var = 1.0 / (1.0 / (s * s) + 1.0 / (se * se))
        means.append(post_var * (m / (s * s) + y / (se * se)))
        variances.append(post_var)
    top = max(logs)
    weights = [math.exp(v - top) for v in logs]
    norm = sum(weights)
    weights = [w / norm for w in weights]
    mean = sum(w * m for w, m in zip(weights, means, strict=True))
    second = sum(w * (v + m * m) for w, v, m in zip(weights, variances, means, strict=True))
    return mean, math.sqrt(max(second - mean * mean, 0.0))
