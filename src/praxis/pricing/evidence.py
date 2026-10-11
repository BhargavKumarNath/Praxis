"""Causal evidence the optimiser may act on, from the Phase 5 randomised price tests.

* Tier elasticities (posterior mean, SD) from the gated elasticity artifact (checksums
  verified on load).
* The causal voluntary-churn response to each product's price, measured per unit log BILL
  change (``praxis.elasticity.churn``, ADR 0016): gamma_p from all concurrent randomised tests
  jointly, partially pooled across products here by a FULL-BAYES normal hierarchical model
  (between-product SD tau integrated over a half-Cauchy prior; ADR 0016 second amendment).
  Without pooling an optimiser picks the products whose noise looks most favourable; with a
  plug-in tau (empirical Bayes, tau-hat = 0 for five noisy tests) it pools away real product
  differences behind a falsely small SD (the seed-45 failure). Price multiplies the hazard,
  so the response stays RELATIVE: the pricing inputs apply it to each customer's own
  calibrated hazard and bill share at decision time (ADR 0013 second amendment, kept).
* The test registry: which products were tested, when (evidence age, extrapolation anchor),
  and at what log ratio.

The report must belong to the artifact (same ``data_version`` and registry hash) and must
have passed every validity gate; the churn-response document must belong to the report and
carry a valid checksum. Otherwise no evidence is returned.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from praxis.elasticity.artifact import ArtifactError, load_artifact
from praxis.elasticity.churn import METHOD as CHURN_METHOD
from praxis.elasticity.churn import RESPONSE_FORMAT as CHURN_FORMAT
from praxis.elasticity.churn import checksum as churn_checksum
from praxis.elasticity.config import ExperimentDesign, ExperimentRegistry
from praxis.pricing.churn_prior import (
    ChurnArchive,
    ChurnPriorError,
    load_archive,
    predictive_prior,
    robust_posterior,
    tau_posterior,
)
from praxis.pricing.config import REPO_ROOT, Evidence
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
    churn_response: float  # d log(voluntary hazard) / d log(bill), partially pooled
    churn_response_sd: float  # posterior SD of the pooled response
    raw_churn_response: float  # this product's test (joint fit) alone
    raw_churn_response_sd: float
    bill_share: float = 1.0  # mean share of the product in the experiment customers' bills
    # (model, posterior mean, posterior SD) for every member of the policy's credible class;
    # the first is the primary (= churn_response, churn_response_sd)
    churn_models: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True)
class PricingEvidence:
    model_version: str
    data_version: str
    tiers: dict[str, TierElasticity]
    products: dict[str, ProductEvidence]
    churn_response_version: str = ""  # checksum of the churn-response document
    churn_archive_version: str = ""  # checksum of the meta-analytic archive ("" if unused)
    # customer-level Var(elasticity) per (signup) tier, randomisation-based (ADR 0016)
    heterogeneity: dict[str, float] = field(default_factory=dict)


def check_churn_document(doc: dict[str, Any]) -> None:
    """Method, format and content checksum of a churn-response document."""
    if doc.get("method") != CHURN_METHOD or doc.get("response_format") != CHURN_FORMAT:
        raise EvidenceError(f"unsupported churn response method {doc.get('method')}")
    if churn_checksum(doc) != doc.get("checksum"):
        raise EvidenceError("churn response checksum mismatch")


def load_churn_response(path: Path, report: dict[str, Any], registry_hash: str) -> dict[str, Any]:
    """The churn-response document, verified against the analysis report and its checksum."""
    try:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"churn response unusable: {exc}") from exc
    check_churn_document(doc)
    if doc.get("report_data_version") != report.get("data_version"):
        raise EvidenceError("churn response does not belong to the analysis report")
    if doc.get("registry_hash") != registry_hash:
        raise EvidenceError("churn response used another experiment registry")
    return doc


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


@dataclass(frozen=True)
class PriorSettings:
    """The robust meta-analytic-predictive prior (``praxis.pricing.churn_prior``)."""

    archive: ChurnArchive
    vague_weight: float
    vague_sd: float


def posteriors(
    estimates: list[float],
    ses: list[float],
    *,
    tau_scale: float,
    priors: list[tuple[float, float]] | None = None,
    prior: PriorSettings | None = None,
) -> dict[str, tuple[list[float], list[float]]]:
    """Every member of the credible class: (means, SDs) per product.

    * ``hierarchical``: full Bayes, tau ~ half-Cauchy(tau_scale) integrated out;
    * ``complete_pooling``: one common response (tau = 0): the precision-weighted mean and its SE;
    * ``no_pooling``: each product's own estimate (tau = infinity);
    * ``meta_analytic`` (with ``priors``, one predictive (mean, SD) per product, and ``prior``):
      each product's own estimate under its robust meta-analytic-predictive prior.
    """
    post = hierarchical_pool(estimates, ses, tau_scale=tau_scale)
    w = [1.0 / s**2 for s in ses]
    common = sum(wi * y for wi, y in zip(w, estimates, strict=True)) / sum(w)
    common_se = math.sqrt(1.0 / sum(w))
    k = len(estimates)
    out = {
        "hierarchical": (post.means, post.sds),
        "complete_pooling": ([common] * k, [common_se] * k),
        "no_pooling": (list(estimates), list(ses)),
    }
    if priors is not None and prior is not None:
        robust = [
            robust_posterior(y, s, m, sd, vague_weight=prior.vague_weight, vague_sd=prior.vague_sd)
            for y, s, (m, sd) in zip(estimates, ses, priors, strict=True)
        ]
        out["meta_analytic"] = ([m for m, _ in robust], [s for _, s in robust])
    return out


def prior_settings(
    cfg: Evidence, root: Path = REPO_ROOT, *, archive_override: Path | None = None
) -> PriorSettings | None:
    """The policy's robust MAP prior, its archive verified against the pinned checksum.

    ``archive_override``: a DEVELOPMENT leave-one-out archive (the nightly seed-1 world is in
    the pinned archive, so deciding it needs the archive without it); verified by its own
    checksum only and recorded in the lineage. Never for a held-out evaluation.
    """
    if cfg.churn_archive is None:
        return None
    path = archive_override or Path(cfg.churn_archive)
    try:
        archive = load_archive(path if path.is_absolute() else root / path)
    except ChurnPriorError as exc:
        raise EvidenceError(str(exc)) from exc
    if archive_override is None and archive.checksum != cfg.churn_archive_checksum:
        raise EvidenceError("churn archive differs from the one the policy pins")
    return PriorSettings(archive, cfg.churn_prior_vague_weight, cfg.churn_prior_vague_sd)


def _predictive_priors(
    names: list[str], churn: dict[str, Any], prior: PriorSettings, tau_scale: float
) -> list[tuple[float, float]]:
    """One predictive prior per product from the archive; the decided experiment must not be
    in it (its data would count twice)."""
    archive = prior.archive
    if archive.registry_hash != churn.get("registry_hash"):
        raise EvidenceError("churn archive used another experiment registry")
    if churn.get("checksum") in archive.sources:
        raise EvidenceError("the decided experiment is in its own prior archive")
    missing = [n for n in names if n not in archive.estimates]
    if missing:
        raise EvidenceError(f"churn archive lacks products {missing}")
    try:
        return [predictive_prior(*archive.estimates[n], tau_scale=tau_scale) for n in names]
    except ChurnPriorError as exc:
        raise EvidenceError(f"churn prior: {exc}") from exc


@dataclass(frozen=True)
class HierarchicalPosterior:
    """Full-Bayes normal hierarchical posterior, tau integrated out (one entry per estimate)."""

    means: list[float]
    sds: list[float]
    tau_mean: float
    tau_p90: float


def hierarchical_pool(
    estimates: list[float], ses: list[float], *, tau_scale: float, grid: int = 4000
) -> HierarchicalPosterior:
    """theta_j ~ N(mu, tau^2), y_j ~ N(theta_j, s_j^2), flat mu, tau ~ half-Cauchy(tau_scale).

    tau is integrated over a deterministic grid (BDA3 s5.4): given tau, mu and theta_j are
    normal; the posterior of theta_j is the tau-mixture of those normals, summarised by its
    mean and SD. Unlike a plug-in tau-hat (Paule-Mandel gives 0 whenever Q < k - 1, routine
    with five noisy tests), this keeps the uncertainty about how different the products are:
    a product whose own estimate disagrees with the others is not pooled away with a falsely
    small SD (Gelman 2006; Polson and Scott 2012; few-group random-effects meta-analysis).
    """
    try:
        post = tau_posterior(estimates, ses, tau_scale=tau_scale, grid=grid)
    except ChurnPriorError as exc:
        raise EvidenceError(f"hierarchical pooling: {exc}") from exc
    y = np.asarray(estimates, dtype=np.float64)
    v = np.asarray(ses, dtype=np.float64) ** 2
    if len(y) == 1:
        return HierarchicalPosterior([float(y[0])], [float(math.sqrt(v[0]))], 0.0, 0.0)
    tau, weight, mu, v_mu = post.tau, post.weight, post.mu, post.v_mu
    prec_data = 1.0 / v[None, :]
    prec_prior = 1.0 / tau[:, None] ** 2
    shrink = prec_prior / (prec_data + prec_prior)  # weight on mu
    cond_mean = (1.0 - shrink) * y[None, :] + shrink * mu[:, None]
    cond_var = 1.0 / (prec_data + prec_prior) + shrink**2 * v_mu[:, None]
    mean = (weight[:, None] * cond_mean).sum(axis=0)
    second = (weight[:, None] * (cond_var + cond_mean**2)).sum(axis=0)
    cdf = np.cumsum(weight)
    return HierarchicalPosterior(
        [float(m) for m in mean],
        [float(math.sqrt(max(s - m * m, 0.0))) for s, m in zip(second, mean, strict=True)],
        float((weight * tau).sum()),
        float(tau[int(np.searchsorted(cdf, 0.9))]),
    )


def _churn_class(
    names: list[str],
    churn: dict[str, Any],
    models: tuple[str, ...],
    tau_scale: float,
    prior: PriorSettings | None,
) -> dict[str, tuple[list[float], list[float]]]:
    """Every requested member of the credible class, from the decided experiment's estimates."""
    raw = [float(churn["products"][n]["gamma"]) for n in names]
    raw_sd = [float(churn["products"][n]["se"]) for n in names]
    if "meta_analytic" in models and prior is None:
        raise EvidenceError("the meta_analytic churn model needs a prior archive")
    priors = None if prior is None else _predictive_priors(names, churn, prior, tau_scale)
    by_model = posteriors(raw, raw_sd, tau_scale=tau_scale, priors=priors, prior=prior)
    if not models or any(m not in by_model for m in models):
        raise EvidenceError(f"unknown churn models {models}")
    return by_model


def load_evidence(
    artifact_dir: Path,
    report_path: Path,
    registry: ExperimentRegistry,
    churn_path: Path,
    *,
    tau_scale: float,
    models: tuple[str, ...] = ("hierarchical",),
    prior: PriorSettings | None = None,
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
    churn = load_churn_response(churn_path, report, registry.config_hash)
    tiers = {
        tier: TierElasticity(float(v["mean"]), float(v["sd"]))
        for tier, v in art.estimates["tier"].items()
    }
    latest: dict[str, ExperimentDesign] = {}
    for design in registry.experiments:
        current = latest.get(design.product)
        if current is None or design.assignment_date > current.assignment_date:
            latest[design.product] = design
    names = sorted(latest)
    measured = churn.get("products", {})
    if set(names) - set(measured):
        raise EvidenceError(f"churn response lacks products {sorted(set(names) - set(measured))}")
    raw = [float(measured[n]["gamma"]) for n in names]
    raw_sd = [float(measured[n]["se"]) for n in names]
    by_model = _churn_class(names, churn, models, tau_scale, prior)
    pooled, pooled_sd = by_model[models[0]]
    products = {
        n: ProductEvidence(
            n,
            latest[n].id,
            latest[n].assignment_date,
            latest[n].window_days,
            latest[n].log_ratio,
            pooled[i],
            pooled_sd[i],
            raw[i],
            raw_sd[i],
            float(measured[n]["mean_share"]),
            tuple((m, by_model[m][0][i], by_model[m][1][i]) for m in models),
        )
        for i, n in enumerate(names)
    }
    return PricingEvidence(
        art.model_version,
        str(manifest["data_version"]),
        tiers,
        products,
        churn_response_version="churn-" + str(churn["checksum"])[:16],
        churn_archive_version="" if prior is None else "archive-" + prior.archive.checksum[:16],
        heterogeneity={
            str(t): float(v["variance"])
            for t, v in churn.get("elasticity_heterogeneity", {}).items()
        },
    )
