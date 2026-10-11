"""Meta-analytic-predictive churn prior (ADR 0016 third amendment): archive, prior, posterior."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from praxis.elasticity.config import load_registry
from praxis.pricing.__main__ import main
from praxis.pricing.churn_prior import (
    ChurnPriorError,
    build_archive,
    load_archive,
    predictive_prior,
    robust_posterior,
    tau_posterior,
)
from praxis.pricing.config import load_policy
from praxis.pricing.evidence import (
    EvidenceError,
    PriorSettings,
    check_churn_document,
    load_evidence,
    prior_settings,
)
from tests.pricing.fakes import write_artifact, write_churn_response, write_report


def _doc(path: Path, gammas: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(write_churn_response(path, gammas=gammas).read_text())
    return out


def _archive(tmp_path: Path, n: int = 4, gamma: float = 1.5, se: float = 1.0) -> Path:
    products = [e.product for e in load_registry().experiments]
    tmp_path.mkdir(parents=True, exist_ok=True)
    docs = [
        (f"exp{k}", _doc(tmp_path / f"c{k}.json", dict.fromkeys(products, (gamma + 0.1 * k, se))))
        for k in range(n)
    ]
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(build_archive(docs, verify=check_churn_document)))
    return path


# --- predictive prior ----------------------------------------------------------------------


def _brute_force_predictive(y: list[float], s: list[float], scale: float) -> tuple[float, float]:
    """Independent check: integrate mu and tau on a plain 2-D grid (no conjugacy)."""
    mu = np.linspace(-10, 10, 2001)
    tau = np.linspace(1e-3, 15, 3000)
    m, t = np.meshgrid(mu, tau, indexing="ij")
    log_lik = sum(
        -0.5 * np.log(si**2 + t**2) - 0.5 * (yi - m) ** 2 / (si**2 + t**2)
        for yi, si in zip(y, s, strict=True)
    )
    log_post = log_lik + np.log(1.0 / (1.0 + (t / scale) ** 2))
    w = np.exp(log_post - log_post.max())
    w /= w.sum()
    mean = float((w * m).sum())
    var = float((w * (t**2 + (m - mean) ** 2)).sum())  # theta_new = mu + tau eps
    return mean, math.sqrt(var)


def test_predictive_prior_matches_brute_force_integration() -> None:
    y, s = [1.4, 2.6, 0.3, 1.9, 1.1], [0.9, 1.3, 1.0, 1.8, 0.8]
    mean, sd = predictive_prior(y, s, tau_scale=1.0)
    ref_mean, ref_sd = _brute_force_predictive(y, s, 1.0)
    assert mean == pytest.approx(ref_mean, abs=0.01)
    assert sd == pytest.approx(ref_sd, abs=0.01)


def test_predictive_prior_is_for_a_new_experiment_not_the_mean() -> None:
    # 25 agreeing experiments: the mean is pinned down, but a NEW experiment's true response
    # still carries the between-experiment SD, so the prior is wider than the SE of the mean
    y, s = [1.7] * 25, [0.9] * 25
    mean, sd = predictive_prior(y, s, tau_scale=1.0)
    assert mean == pytest.approx(1.7)
    assert sd > 0.9 / math.sqrt(25)
    # experiments that disagree beyond their noise widen it
    spread = [1.7 + (2.0 if k % 2 else -2.0) for k in range(25)]
    assert predictive_prior(spread, s, tau_scale=1.0)[1] > sd + 1.0


@pytest.mark.parametrize(
    ("y", "s", "scale"),
    [([1.0], [1.0], 1.0), ([1.0, float("nan")], [1.0, 1.0], 1.0), ([1.0, 2.0], [1.0, 0.0], 1.0)],
)
def test_predictive_prior_refuses_unusable_archives(
    y: list[float], s: list[float], scale: float
) -> None:
    with pytest.raises(ChurnPriorError):
        predictive_prior(y, s, tau_scale=scale)


def test_tau_posterior_refuses_bad_scale() -> None:
    with pytest.raises(ChurnPriorError, match="scale"):
        tau_posterior([1.0, 2.0], [1.0, 1.0], tau_scale=0.0)


# --- robust posterior ----------------------------------------------------------------------


def test_robust_posterior_without_vague_component_is_conjugate() -> None:
    mean, sd = robust_posterior(0.0, 1.0, 2.0, 0.5, vague_weight=0.0, vague_sd=4.0)
    prec = 1 / 0.5**2 + 1 / 1.0**2
    assert mean == pytest.approx((2.0 / 0.25 + 0.0) / prec)
    assert sd == pytest.approx(prec**-0.5)


def test_a_test_reading_no_effect_cannot_overrule_the_archive() -> None:
    # the seed-202 shape: the test reads ~0 +- 1.4, the archive ~1.65 +- 0.57
    mean, sd = robust_posterior(-0.68, 1.40, 1.65, 0.57, vague_weight=0.2, vague_sd=4.0)
    assert 0.8 < mean < 1.65  # far from the test's -0.68: one noisy test is not enough
    assert sd < 1.40


def test_prior_data_conflict_moves_toward_the_data_and_widens() -> None:
    agree = robust_posterior(1.5, 1.0, 1.65, 0.57, vague_weight=0.2, vague_sd=4.0)
    conflict = robust_posterior(9.0, 1.0, 1.65, 0.57, vague_weight=0.2, vague_sd=4.0)
    plain = robust_posterior(9.0, 1.0, 1.65, 0.57, vague_weight=0.0, vague_sd=4.0)
    assert conflict[0] > plain[0] + 3.0  # the vague component takes over
    assert conflict[1] > agree[1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"se": 0.0},
        {"prior_sd": 0.0},
        {"vague_sd": 0.0},
        {"vague_weight": 1.0},
        {"vague_weight": -0.1},
    ],
)
def test_robust_posterior_refuses_invalid_inputs(kwargs: dict[str, float]) -> None:
    args: dict[str, float] = {
        "y": 1.0,
        "se": 1.0,
        "prior_mean": 1.0,
        "prior_sd": 1.0,
        "vague_weight": 0.2,
        "vague_sd": 4.0,
    } | kwargs
    y, se, m, s = args.pop("y"), args.pop("se"), args.pop("prior_mean"), args.pop("prior_sd")
    with pytest.raises(ChurnPriorError):
        robust_posterior(y, se, m, s, **args)


# --- archive -------------------------------------------------------------------------------


def test_archive_round_trip_groups_estimates_by_product(tmp_path: Path) -> None:
    archive = load_archive(_archive(tmp_path, n=3))
    g, s = archive.estimates["api_requests"]
    assert g == pytest.approx([1.5, 1.6, 1.7]) and s == [1.0, 1.0, 1.0]
    assert len(archive.sources) == 3
    assert archive.registry_hash == load_registry().config_hash


def test_archive_refuses_duplicates_mixtures_tampering_and_nothing(tmp_path: Path) -> None:
    doc = _doc(tmp_path / "a.json")
    with pytest.raises(ChurnPriorError, match="duplicate"):
        build_archive([("a", doc), ("b", doc)], verify=check_churn_document)
    other = _doc(tmp_path / "b.json") | {"registry_hash": "0" * 64}
    with pytest.raises(ChurnPriorError, match="share"):
        build_archive([("a", doc), ("b", other)], verify=check_churn_document)
    with pytest.raises(ChurnPriorError, match="at least one"):
        build_archive([], verify=check_churn_document)
    bad = json.loads(json.dumps(doc))
    bad["products"]["api_requests"]["gamma"] = 9.0  # checksum no longer matches
    with pytest.raises(EvidenceError, match="checksum"):
        build_archive([("a", bad)], verify=check_churn_document)
    path = _archive(tmp_path)
    tampered = json.loads(path.read_text())
    tampered["experiments"][0]["products"]["api_requests"]["gamma"] = 9.0
    path.write_text(json.dumps(tampered))
    with pytest.raises(ChurnPriorError, match="checksum"):
        load_archive(path)
    with pytest.raises(ChurnPriorError, match="unusable"):
        load_archive(tmp_path / "missing.json")


# --- evidence ------------------------------------------------------------------------------


def _settings(path: Path) -> PriorSettings:
    return PriorSettings(load_archive(path), vague_weight=0.2, vague_sd=4.0)


def test_evidence_adds_the_meta_analytic_member(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    gammas = {e.product: (0.0, 1.4) for e in registry.experiments}  # a test reading ~0
    churn = write_churn_response(tmp_path / "churn.json", registry, gammas=gammas)
    prior = _settings(_archive(tmp_path / "arch", n=6))
    models = ("hierarchical", "complete_pooling", "no_pooling", "meta_analytic")
    ev = load_evidence(art, report, registry, churn, tau_scale=1.0, models=models, prior=prior)
    api = {m: (mu, sd) for m, mu, sd in ev.products["api_requests"].churn_models}
    assert api["no_pooling"] == (0.0, 1.4)
    assert api["meta_analytic"][0] > 0.8  # the archive (~1.75) is not overruled by one test
    assert api["meta_analytic"][1] < 1.4
    assert ev.churn_archive_version == "archive-" + prior.archive.checksum[:16]
    without = load_evidence(art, report, registry, churn, tau_scale=1.0)
    assert without.churn_archive_version == ""


def test_evidence_refuses_an_unusable_prior(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    churn = write_churn_response(tmp_path / "churn.json", registry)
    models = ("hierarchical", "meta_analytic")
    with pytest.raises(EvidenceError, match="needs a prior archive"):
        load_evidence(art, report, registry, churn, tau_scale=1.0, models=models)
    # the decided experiment inside its own archive would count its data twice
    doc = json.loads(churn.read_text())
    own = tmp_path / "own.json"
    own.write_text(json.dumps(build_archive([("self", doc)], verify=check_churn_document)))
    with pytest.raises(EvidenceError, match="own prior archive"):
        load_evidence(
            art, report, registry, churn, tau_scale=1.0, models=models, prior=_settings(own)
        )
    # products the archive never measured
    one, two = {"api_requests": (1.0, 1.0)}, {"api_requests": (2.0, 1.0)}
    few = tmp_path / "few.json"
    few.write_text(
        json.dumps(
            build_archive(
                [("x", _doc(tmp_path / "x.json", one)), ("y", _doc(tmp_path / "y.json", two))],
                verify=check_churn_document,
            )
        )
    )
    with pytest.raises(EvidenceError, match="lacks products"):
        load_evidence(
            art, report, registry, churn, tau_scale=1.0, models=models, prior=_settings(few)
        )
    # an archive of another registry
    path = _archive(tmp_path / "reg")
    foreign = _settings(path)
    foreign = PriorSettings(
        type(foreign.archive)(
            foreign.archive.checksum, "0" * 64, foreign.archive.sources, foreign.archive.estimates
        ),
        0.2,
        4.0,
    )
    with pytest.raises(EvidenceError, match="another experiment registry"):
        load_evidence(art, report, registry, churn, tau_scale=1.0, models=models, prior=foreign)
    # one archived experiment cannot define a predictive prior
    single = tmp_path / "single.json"
    single.write_text(
        json.dumps(
            build_archive(
                [
                    (
                        "x",
                        _doc(
                            tmp_path / "s.json",
                            {e.product: (1.0, 1.0) for e in registry.experiments},
                        ),
                    )
                ],
                verify=check_churn_document,
            )
        )
    )
    with pytest.raises(EvidenceError, match="churn prior"):
        load_evidence(
            art, report, registry, churn, tau_scale=1.0, models=models, prior=_settings(single)
        )


def test_prior_settings_follow_the_policy_and_verify_the_pinned_archive(tmp_path: Path) -> None:
    policy = load_policy()
    settings = prior_settings(policy.evidence)
    assert (
        settings is not None and settings.archive.checksum == policy.evidence.churn_archive_checksum
    )
    assert len(settings.archive.sources) == 25
    path = _archive(tmp_path)
    moved = policy.evidence.model_copy(update={"churn_archive": str(path)})
    with pytest.raises(EvidenceError, match="pins"):
        prior_settings(moved)
    # a development leave-one-out archive is taken on its own checksum, not the policy pin
    loo = prior_settings(policy.evidence, archive_override=path)
    assert loo is not None and loo.archive.checksum == load_archive(path).checksum
    broken = policy.evidence.model_copy(update={"churn_archive": str(tmp_path / "none.json")})
    with pytest.raises(EvidenceError, match="unusable"):
        prior_settings(broken)
    off = policy.evidence.model_copy(
        update={
            "churn_archive": None,
            "churn_archive_checksum": None,
            "churn_models": ("hierarchical",),
        }
    )
    assert prior_settings(off) is None


def test_policy_requires_an_archive_exactly_when_the_prior_is_used() -> None:
    raw = load_policy().model_dump()
    raw["evidence"]["churn_models"] = ["hierarchical"]
    with pytest.raises(ValidationError, match="meta_analytic"):
        type(load_policy()).model_validate(raw)
    raw = load_policy().model_dump()
    raw["evidence"]["churn_archive"] = None
    with pytest.raises(ValidationError, match="meta_analytic"):
        type(load_policy()).model_validate(raw)


# --- CLI -----------------------------------------------------------------------------------


def test_cli_builds_an_archive_and_refuses_a_corrupt_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = []
    for k in range(2):
        d = tmp_path / f"eval-seed{k}-c8000" / "analysis" / "churn_response.json"
        d.parent.mkdir(parents=True)
        write_churn_response(d, gammas={"api_requests": (1.0 + k, 1.0)})
        docs.append(str(d))
    out = tmp_path / "archive.json"
    assert main(["churn-archive", "--out", str(out), *docs]) == 0
    archive = json.loads(out.read_text())
    assert capsys.readouterr().out.strip() == archive["checksum"]
    assert [e["source"] for e in archive["experiments"]] == ["eval-seed0-c8000", "eval-seed1-c8000"]
    corrupt = json.loads(Path(docs[0]).read_text()) | {"checksum": "0" * 64}
    Path(docs[0]).write_text(json.dumps(corrupt))
    assert main(["churn-archive", "--out", str(out), *docs]) == 2
    assert "refused" in capsys.readouterr().err
