"""Analysis report contract and the gated, checksummed elasticity artifact."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from praxis.elasticity.analysis import Analysis
from praxis.elasticity.artifact import (
    ESTIMATES,
    MANIFEST,
    ArtifactError,
    build_artifact,
    load_artifact,
    save_artifact,
)
from praxis.elasticity.config import ElasticityConfig
from tests.elasticity.helpers import registry, true_elasticity


def test_report_contract(analysis: Analysis) -> None:
    r = analysis.report
    assert r["notice"].startswith("SYNTHETIC")
    assert r["data_version"].startswith("units-")
    assert {e["id"] for e in r["experiments"]} == {"px-p_up", "px-p_down"}
    assert set(r["estimates"]) == {
        "pooled",
        "pooled_iv",
        "tier",
        "industry",
        "cell",
        "dose",
        "per_test",
    }
    assert set(r["estimates"]["dose"]) == {"raise", "cut"}
    assert r["validity"]["passed"] and analysis.passed
    assert r["units"]["in_model"] == int(analysis.in_model.sum()) <= r["units"]["outcome_observed"]


def test_pooled_log_log_estimate_recovers_the_weighted_truth(analysis: Analysis) -> None:
    u = analysis.units
    obs = ~u.missing_outcome
    truth = np.array([true_elasticity(t, i) for t, i in zip(u.tier, u.industry, strict=True)])
    w = u.design_weight[obs]
    expected = float(truth[obs] @ w / w.sum())
    pooled = analysis.report["estimates"]["pooled"]
    assert pooled["ci_low"] < expected < pooled["ci_high"]


def test_naive_pre_post_is_reported_beside_the_randomised_estimate(analysis: Analysis) -> None:
    for test in analysis.report["estimates"]["per_test"].values():
        assert test["itt"] and test["iv"] and test["naive_pre_post"]
        assert test["iv"]["first_stage"] == pytest.approx(1.0, abs=1e-3)  # micro rounding


def test_artifact_round_trip_and_lineage(
    analysis: Analysis, cfg: ElasticityConfig, tmp_path: Path
) -> None:
    manifest, text = build_artifact(analysis, cfg, registry(), code_revision="test-rev")
    for key in (
        "model_version",
        "data_version",
        "feature_version",
        "code_revision",
        "parameters",
        "metrics",
        "artifact_checksum",
        "created_at",
        "config_hash",
        "registry_hash",
    ):
        assert manifest[key], key
    assert manifest["is_synthetic"] is True
    path = save_artifact(manifest, text, tmp_path)
    loaded = load_artifact(path)
    assert loaded.model_version == manifest["model_version"]
    assert set(loaded.estimates["tier"]) == {"enterprise", "growth", "starter"}
    assert save_artifact(manifest, text, tmp_path) == path  # idempotent


def test_model_version_does_not_depend_on_the_clock(
    analysis: Analysis, cfg: ElasticityConfig
) -> None:
    a, _ = build_artifact(
        analysis, cfg, registry(), code_revision="r", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    b, _ = build_artifact(
        analysis, cfg, registry(), code_revision="r", now=datetime(2027, 1, 1, tzinfo=UTC)
    )
    assert a["model_version"] == b["model_version"] and a["created_at"] != b["created_at"]


def test_failed_analysis_cannot_become_an_artifact(
    contaminated_analysis: Analysis, cfg: ElasticityConfig
) -> None:
    assert not contaminated_analysis.passed
    with pytest.raises(ArtifactError, match="gate"):
        build_artifact(contaminated_analysis, cfg, registry(), code_revision="r")


def test_tampering_is_detected(analysis: Analysis, cfg: ElasticityConfig, tmp_path: Path) -> None:
    manifest, text = build_artifact(analysis, cfg, registry(), code_revision="r")
    path = save_artifact(manifest, text, tmp_path)
    (path / ESTIMATES).write_text(text.replace("starter", "sterter"))
    with pytest.raises(ArtifactError, match="checksum"):
        load_artifact(path)
    m = json.loads((path / MANIFEST).read_text())
    m["data_version"] = "units-forged"
    (path / MANIFEST).write_text(json.dumps(m))
    with pytest.raises(ArtifactError, match="manifest checksum"):
        load_artifact(path)
    (path / MANIFEST).write_text("{not json")
    with pytest.raises(ArtifactError, match="JSON"):
        load_artifact(path)
    with pytest.raises(ArtifactError, match="no manifest"):
        load_artifact(tmp_path / "missing")


def test_unsupported_format_and_missing_estimates(
    analysis: Analysis, cfg: ElasticityConfig, tmp_path: Path
) -> None:
    manifest, text = build_artifact(analysis, cfg, registry(), code_revision="r")
    path = save_artifact(manifest, text, tmp_path)
    (path / ESTIMATES).unlink()
    with pytest.raises(ArtifactError, match="missing"):
        load_artifact(path)
    m = dict(manifest, artifact_format=99)
    (path / MANIFEST).write_text(json.dumps(m))
    with pytest.raises(ArtifactError, match="format"):
        load_artifact(path)


def test_cli_refuses_to_publish_a_failed_analysis(
    contaminated_analysis: Analysis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit 1, report written, no artifact. Warehouse I/O is stubbed at the boundary only."""
    from praxis.elasticity import __main__ as cli

    class _Con:
        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "connect", lambda _path: _Con())
    monkeypatch.setattr(cli, "load_extract", lambda *_a: None)
    monkeypatch.setattr(cli, "analyse", lambda *_a: contaminated_analysis)
    out, models = tmp_path / "out", tmp_path / "models"
    code = cli.main(["--db", "x.duckdb", "analyze", "--out", str(out), "--models", str(models)])
    assert code == 1
    assert json.loads((out / "report.json").read_text())["validity"]["passed"] is False
    assert len(json.loads((out / "units.json").read_text())) == contaminated_analysis.units.n
    assert not models.exists()
