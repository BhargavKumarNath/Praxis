"""Evidence loading: gated artifact + its own report + registry, or a clear refusal."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from praxis.elasticity.config import load_registry
from praxis.pricing.evidence import (
    EvidenceError,
    hierarchical_pool,
    load_evidence,
    posteriors,
    resolve_artifact_dir,
)
from tests.pricing.fakes import write_artifact, write_churn_response, write_report


def test_load_evidence_joins_artifact_report_registry_and_churn_response(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    gammas = {e.product: (2.0, 1.0) for e in registry.experiments}
    gammas["api_requests"] = (4.0, 1.0)  # one product reads high
    churn = write_churn_response(tmp_path / "churn.json", registry, gammas=gammas)
    ev = load_evidence(art, report, registry, churn, tau_scale=1.0)
    assert ev.tiers["growth"].mean == -1.2 and ev.tiers["growth"].sd == 0.03
    assert set(ev.products) == {e.product for e in registry.experiments}
    api = ev.products["api_requests"]
    assert api.assignment_date.isoformat() == "2026-02-02" and api.window_days == 28
    assert api.raw_churn_response == 4.0 and api.raw_churn_response_sd == 1.0
    # products agree within noise: the high one is pulled toward the common bill response
    assert 2.0 < api.churn_response < 4.0
    assert api.churn_response_sd < api.raw_churn_response_sd
    assert ev.churn_response_version.startswith("churn-")
    assert api.bill_share == 0.2  # the experiment's mean share, carried to the decision
    # the artifact root also works: the report picks its own artifact
    root = load_evidence(tmp_path / "models", report, registry, churn, tau_scale=1.0)
    assert root.model_version == ev.model_version


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(method="other"), "unsupported"),
        (lambda d: d.update(report_data_version="units-other"), "does not belong"),
        (lambda d: d.update(registry_hash="0" * 64), "another experiment registry"),
        (lambda d: d["products"].pop("gpu_minutes"), "lacks products"),
        (lambda d: d["products"]["gpu_minutes"].update(gamma=99.0), "checksum"),
    ],
)
def test_churn_response_must_belong_to_the_report_and_be_intact(
    tmp_path: Path, change: object, message: str
) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    path = write_churn_response(tmp_path / "churn.json", registry)
    doc = json.loads(path.read_text())
    change(doc)  # type: ignore[operator]
    if message != "checksum":
        from praxis.elasticity.churn import checksum

        doc["checksum"] = checksum(doc)  # internally consistent, but wrong
    path.write_text(json.dumps(doc))
    with pytest.raises(EvidenceError, match=message):
        load_evidence(art, report, registry, path, tau_scale=1.0)
    with pytest.raises(EvidenceError, match="unusable"):
        load_evidence(art, report, registry, tmp_path / "missing.json", tau_scale=1.0)


def test_root_resolution_needs_a_matching_artifact(tmp_path: Path) -> None:
    write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json", data_version="units-other")
    with pytest.raises(EvidenceError, match="no elasticity artifact"):
        resolve_artifact_dir(tmp_path / "models", report)
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(EvidenceError, match="report unusable"):
        resolve_artifact_dir(tmp_path / "models", tmp_path / "bad.json")


@pytest.mark.parametrize(
    ("report_kwargs", "message"),
    [
        ({"data_version": "units-elsewhere"}, "does not belong"),
        ({"passed": False}, "validity gate"),
    ],
)
def test_inconsistent_or_ungated_evidence_is_refused(
    tmp_path: Path, report_kwargs: dict[str, object], message: str
) -> None:
    art = write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json", **report_kwargs)  # type: ignore[arg-type]
    churn = write_churn_response(tmp_path / "churn.json")
    with pytest.raises(EvidenceError, match=message):
        load_evidence(art, report, load_registry(), churn, tau_scale=1.0)


def test_registry_mismatch_is_refused(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    churn = write_churn_response(tmp_path / "churn.json", registry)
    other = registry.model_copy(update={"experiments": registry.experiments[:2]})
    with pytest.raises(EvidenceError, match="registry"):
        load_evidence(art, report, other, churn, tau_scale=1.0)


def test_corrupt_artifacts_and_reports_are_refused(tmp_path: Path) -> None:
    art = write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json")
    churn = write_churn_response(tmp_path / "churn.json")
    (art / "estimates.json").write_text("{}")
    with pytest.raises(EvidenceError, match="artifact unusable"):
        load_evidence(art, report, load_registry(), churn, tau_scale=1.0)
    good = write_artifact(tmp_path / "models2")
    with pytest.raises(EvidenceError, match="report unusable"):
        load_evidence(good, tmp_path / "missing.json", load_registry(), churn, tau_scale=1.0)


def test_hierarchical_pooling_keeps_agreement_and_honest_disagreement() -> None:
    # agreeing estimates: shrunk together, SD close to the common mean's
    agree = hierarchical_pool([2.0, 2.0, 2.0], [1.0, 1.0, 1.0], tau_scale=1.0)
    assert agree.means == pytest.approx([2.0] * 3)
    assert all(1 / 3**0.5 < sd < 1.0 for sd in agree.sds)
    # seed-45 shape: one product reads high, four low. Empirical Bayes (tau-hat = 0) put every
    # product at 1.23 +- 0.94; the full posterior keeps cpu higher and far less certain
    post = hierarchical_pool(
        [0.74, 5.22, 0.81, 5.59, 0.77], [1.46, 3.11, 1.93, 8.75, 1.88], tau_scale=1.0
    )
    cpu, api = post.means[1], post.means[0]
    assert cpu > api and post.sds[1] > 1.3
    assert all(sd > 0.94 for sd in post.sds)  # no falsely small SD
    assert 0 < post.tau_mean < post.tau_p90
    # genuinely heterogeneous precise estimates are left almost alone
    far = hierarchical_pool([0.0, 5.0, 10.0], [0.1, 0.1, 0.1], tau_scale=1.0)
    assert far.means == pytest.approx([0.0, 5.0, 10.0], abs=0.01)
    # a wider tau prior shrinks less
    wide = hierarchical_pool([0.74, 5.22, 0.81], [1.46, 3.11, 1.93], tau_scale=4.0)
    tight = hierarchical_pool([0.74, 5.22, 0.81], [1.46, 3.11, 1.93], tau_scale=0.25)
    assert wide.means[1] > tight.means[1]
    single = hierarchical_pool([0.3], [0.1], tau_scale=1.0)
    assert (single.means, single.sds) == ([0.3], [0.1])


def test_closed_form_matches_direct_numerical_integration() -> None:
    """Independent check: integrate mu numerically on a grid (the code does it analytically)."""
    y, s = np.array([0.5, 3.0, 1.0]), np.array([1.0, 2.0, 1.5])
    post = hierarchical_pool(list(y), list(s), tau_scale=1.0)
    tau = np.linspace(1e-4, 40, 8000)[:, None]
    mu = np.linspace(-20, 25, 801)[None, :]
    log_p = np.log(1 / (1 + tau**2))  # half-Cauchy(1), flat mu
    for j in range(3):
        log_p = (
            log_p - 0.5 * np.log(s[j] ** 2 + tau**2) - 0.5 * (y[j] - mu) ** 2 / (s[j] ** 2 + tau**2)
        )
    w = np.exp(log_p - log_p.max())
    w /= w.sum()
    prec = 1 / s[1] ** 2 + 1 / tau**2
    cond_mean = (y[1] / s[1] ** 2 + mu / tau**2) / prec
    mean = float((w * cond_mean).sum())
    sd = float(np.sqrt((w * (1 / prec + cond_mean**2)).sum() - mean**2))
    assert post.means[1] == pytest.approx(mean, abs=0.01)
    assert post.sds[1] == pytest.approx(sd, abs=0.01)


@pytest.mark.parametrize(
    ("estimates", "ses", "scale"),
    [([], [], 1.0), ([1.0, 2.0], [0.0, 1.0], 1.0), ([1.0, np.nan], [1.0, 1.0], 1.0),
     ([1.0, 2.0], [1.0, 1.0], 0.0)],
)  # fmt: skip
def test_hierarchical_pooling_refuses_unusable_inputs(
    estimates: list[float], ses: list[float], scale: float
) -> None:
    with pytest.raises(EvidenceError):
        hierarchical_pool(estimates, ses, tau_scale=scale)


def test_the_credible_class_spans_complete_to_no_pooling() -> None:
    est, ses = [0.74, 5.22, 0.81], [1.46, 3.11, 1.93]
    post = posteriors(est, ses, tau_scale=1.0)
    w = [1 / s**2 for s in ses]
    common = sum(wi * y for wi, y in zip(w, est, strict=True)) / sum(w)
    assert post["complete_pooling"][0] == pytest.approx([common] * 3)
    assert post["complete_pooling"][1] == pytest.approx([(1 / sum(w)) ** 0.5] * 3)
    assert post["no_pooling"] == (est, ses)
    h = post["hierarchical"][0]
    assert common < h[1] < est[1]  # between the two extremes


def test_evidence_carries_every_model_of_the_class(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    churn = write_churn_response(tmp_path / "churn.json", registry)
    models = ("hierarchical", "complete_pooling", "no_pooling")
    ev = load_evidence(art, report, registry, churn, tau_scale=1.0, models=models)
    api = ev.products["api_requests"]
    assert [m for m, _, _ in api.churn_models] == list(models)
    assert (api.churn_response, api.churn_response_sd) == api.churn_models[0][1:]
    with pytest.raises(EvidenceError, match="unknown churn models"):
        load_evidence(art, report, registry, churn, tau_scale=1.0, models=("other",))
