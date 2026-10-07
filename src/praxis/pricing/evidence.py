"""Causal evidence the optimiser may act on, from the Phase 5 randomised price tests.

* Tier elasticities (posterior mean, SD) from the gated elasticity artifact (checksums
  verified on load).
* The churn response to each product's price, as a RELATIVE effect: the log ratio of the
  treatment and control churn rates of its randomised test (the analysis report's guardrails),
  per unit log price, with a delta-method standard error. Price multiplies the churn hazard, so
  the relative effect transports across periods while an absolute one does not: the base
  hazard moves with service quality (ADR 0013, second amendment: an absolute slope measured
  in February understated August's by 1.5-2.3x). The pricing inputs scale it by the churn rate
  observed just before each decision. One test per product is noisy, so product effects are
  partially pooled (empirical Bayes, Paule-Mandel between-product variance, Morris posterior
  SD); without pooling an optimiser picks the products whose noise looks most favourable.
* The test registry: which products were tested, when (evidence age, extrapolation anchor),
  and at what log ratio.

The report must belong to the artifact (same ``data_version`` and registry hash) and must
have passed every validity gate, otherwise no evidence is returned.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from praxis.elasticity.artifact import ArtifactError, load_artifact
from praxis.elasticity.config import ExperimentDesign, ExperimentRegistry
from praxis.pricing.problem import TierElasticity


class EvidenceError(RuntimeError):
    """The elasticity evidence is missing, corrupt, ungated or inconsistent."""


@dataclass(frozen=True)
class ProductEvidence:
    product: str
    experiment_id: str
    assignment_date: date
    window_days: int
    log_ratio: float
    churn_rel_slope: float  # d log(churn rate) / d log price, partially pooled
    churn_rel_se: float  # posterior SD of the pooled relative slope
    raw_churn_rel_slope: float  # this product's test alone
    raw_churn_rel_se: float
    control_churn_rate: float  # churn rate of the control arm over the window (audit only)

    def churn_rel_upper(self, z: float) -> float:
        return self.churn_rel_slope + z * self.churn_rel_se


@dataclass(frozen=True)
class PricingEvidence:
    model_version: str
    data_version: str
    tiers: dict[str, TierElasticity]
    products: dict[str, ProductEvidence]


def churn_rel_slope(guardrails: dict[str, Any], log_ratio: float) -> tuple[float, float, float]:
    """(slope, SE, control rate): log churn-rate ratio per unit log price, one randomised test.

    Rates use a +0.5 continuity correction, so an arm without churners stays finite. The SE is
    the delta-method SE of a log ratio of binomial proportions.
    """
    c, t = guardrails["control"], guardrails["treatment"]
    n_c, n_t = int(c["units"]), int(t["units"])
    p_c, p_t = float(c["churn_rate"]), float(t["churn_rate"])
    if n_c <= 0 or n_t <= 0 or not (math.isfinite(p_c) and math.isfinite(p_t)) or log_ratio == 0:
        raise EvidenceError("guardrail churn rates are missing or empty")
    q_c, q_t = (p_c * n_c + 0.5) / (n_c + 1), (p_t * n_t + 0.5) / (n_t + 1)
    se = math.sqrt((1 - q_t) / (n_t * q_t) + (1 - q_c) / (n_c * q_c))
    return math.log(q_t / q_c) / log_ratio, se / abs(log_ratio), p_c


def resolve_artifact_dir(path: Path, report_path: Path) -> Path:
    """``path`` is an artifact, or a root holding several: pick the report's own artifact."""
    if (path / "manifest.json").is_file():
        return path
    try:
        version = json.loads(report_path.read_text(encoding="utf-8")).get("data_version")
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"elasticity analysis report unusable: {exc}") from exc
    for manifest in sorted(path.glob("*/manifest.json")):
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("data_version") == version:
                return manifest.parent
        except json.JSONDecodeError:
            continue
    raise EvidenceError(f"no elasticity artifact under {path} for data version {version}")


def pool_slopes(slopes: list[float], ses: list[float]) -> tuple[list[float], list[float], float]:
    """Random-effects partial pooling toward the common mean: (posterior means, SDs, tau).

    tau^2 by Paule-Mandel (generalised Q = k - 1); posterior SD by Morris (1983), which adds the
    uncertainty of the estimated common mean. One estimate returns itself.
    """
    b = [float(x) for x in slopes]
    v = [float(se) ** 2 for se in ses]
    if any(x <= 0 for x in v):
        raise EvidenceError("churn slope standard errors must be positive")
    k = len(b)
    if k == 1:
        return b, [math.sqrt(v[0])], 0.0

    def mean_and_q(tau2: float) -> tuple[float, float, float]:
        w = [1.0 / (vi + tau2) for vi in v]
        mu = sum(wi * bi for wi, bi in zip(w, b, strict=True)) / sum(w)
        q = sum(wi * (bi - mu) ** 2 for wi, bi in zip(w, b, strict=True))
        return mu, q, 1.0 / sum(w)

    tau2 = 0.0
    if mean_and_q(0.0)[1] > k - 1:
        lo, hi = 0.0, max(v) * 100.0 + max(abs(x) for x in b) ** 2
        for _ in range(200):  # Q(tau^2) is decreasing in tau^2
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if mean_and_q(mid)[1] > k - 1 else (lo, mid)
        tau2 = 0.5 * (lo + hi)
    mu, _, var_mu = mean_and_q(tau2)
    means, sds = [], []
    for bi, vi in zip(b, v, strict=True):
        shrink = vi / (vi + tau2)  # weight on the common mean
        means.append(mu + (1.0 - shrink) * (bi - mu))
        sds.append(math.sqrt((1.0 - shrink) * vi + shrink**2 * var_mu))
    return means, sds, math.sqrt(tau2)


def load_evidence(
    artifact_dir: Path, report_path: Path, registry: ExperimentRegistry
) -> PricingEvidence:
    """``artifact_dir`` may be the artifact itself or the artifact root (see above)."""
    artifact_dir = resolve_artifact_dir(artifact_dir, report_path)
    try:
        art = load_artifact(artifact_dir)
    except ArtifactError as exc:
        raise EvidenceError(f"elasticity artifact unusable: {exc}") from exc
    try:
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"elasticity analysis report unusable: {exc}") from exc
    manifest = art.manifest
    if report.get("data_version") != manifest["data_version"]:
        raise EvidenceError("analysis report does not belong to the elasticity artifact")
    if not (report.get("registry_hash") == manifest["registry_hash"] == registry.config_hash):
        raise EvidenceError("experiment registry differs from the one the evidence used")
    if not report.get("validity", {}).get("passed", False):
        raise EvidenceError("analysis report failed a validity gate")
    tiers = {
        tier: TierElasticity(float(v["mean"]), float(v["sd"]))
        for tier, v in art.estimates["tier"].items()
    }
    by_id = {e["id"]: e for e in report.get("experiments", [])}
    latest: dict[str, tuple[ExperimentDesign, float, float, float]] = {}
    for design in registry.experiments:
        exp = by_id.get(design.id)
        if exp is None:
            raise EvidenceError(f"report has no results for experiment {design.id}")
        slope, se, base = churn_rel_slope(exp["guardrails"], design.log_ratio)
        current = latest.get(design.product)
        if current is None or design.assignment_date > current[0].assignment_date:
            latest[design.product] = (design, slope, se, base)
    names = sorted(latest)
    pooled, pooled_sd, _tau = pool_slopes(
        [latest[n][1] for n in names], [latest[n][2] for n in names]
    )
    products = {
        n: ProductEvidence(
            n,
            latest[n][0].id,
            latest[n][0].assignment_date,
            latest[n][0].window_days,
            latest[n][0].log_ratio,
            pooled[i],
            pooled_sd[i],
            latest[n][1],
            latest[n][2],
            latest[n][3],
        )
        for i, n in enumerate(names)
    }
    return PricingEvidence(art.model_version, str(manifest["data_version"]), tiers, products)
