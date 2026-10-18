"""Artifact lineage, reproducibility and integrity checks."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from praxis.forecasting.artifact import (
    ArtifactError,
    ForecastArtifact,
    load_artifact,
    save_artifact,
    train_artifact,
    train_artifact_with_model,
)
from praxis.forecasting.features import CATEGORICAL, FEATURE_VERSION, build_features, build_frame
from praxis.forecasting.models import RidgeBaseline
from tests.forecasting.conftest import N_DAYS
from tests.forecasting.helpers import make_panel, make_plan, small_config

REQUIRED_LINEAGE = (
    "model_version",
    "data_version",
    "feature_version",
    "code_revision",
    "config",
    "artifact_checksum",
    "created_at",
    "files",
)


def test_manifest_records_full_lineage(artifact: ForecastArtifact) -> None:
    m = artifact.manifest
    for key in REQUIRED_LINEAGE:
        assert m[key], key
    assert m["is_synthetic"] is True
    assert m["feature_version"] == FEATURE_VERSION
    assert m["data_version"] == make_panel(n_days=N_DAYS).data_version()
    assert m["code_revision"] == "test-rev"
    assert m["model_version"].startswith("demand-hybrid-")
    assert m["champion"] == {
        "kind": "hybrid",
        "point": "ridge",
        "quantiles": "lightgbm_calibrated",
        "adr": "0011",
    }
    assert "ridge.json" in m["files"] and "point.txt" not in m["files"]
    assert m["config"]["lightgbm"]["seed"] == 42  # parameters travel with the model
    assert len(m["calibration_offsets"]) == len(m["quantiles"])
    assert m["backtest"] is None
    assert all(len(v) == 64 for v in m["files"].values())


def test_same_inputs_give_the_same_model_version_regardless_of_time() -> None:
    args = (make_panel(n_days=N_DAYS), make_plan(), small_config())
    a, fa = train_artifact(*args, code_revision="r", now=datetime(2026, 1, 1, tzinfo=UTC))
    b, fb = train_artifact(*args, code_revision="r", now=datetime(2027, 1, 1, tzinfo=UTC))
    assert fa == fb
    assert a["model_version"] == b["model_version"] and a["created_at"] != b["created_at"]
    c, _ = train_artifact(*args, code_revision="other")
    assert c["model_version"] != a["model_version"]


def test_saved_model_predicts_exactly_like_the_trained_one(artifact_dir: Path) -> None:
    panel = make_panel(n_days=N_DAYS)
    manifest, files = train_artifact(panel, make_plan(), small_config(), code_revision="test-rev")
    loaded = load_artifact(artifact_dir)
    assert loaded.model_version == manifest["model_version"]
    assert loaded.model.quantiles.model_strings() == {
        k.removesuffix(".txt"): v for k, v in files.items() if k.endswith(".txt")
    }
    assert json.loads(files["ridge.json"]) == loaded.model.ridge.state()
    frame = build_features(panel, N_DAYS - 1, (1, 7), make_plan(), loaded.catalogue)
    assert np.isfinite(loaded.model.predict(frame).quantiles).all()
    assert loaded.fallback.predict(frame).point.shape == (len(frame),)


def test_loaded_artifact_predicts_like_the_in_memory_model(artifact: ForecastArtifact) -> None:
    """Regression (2026-10-11): the ridge state is stored with sorted keys; loading rebuilt
    the categorical one-hot blocks in JSON order, misaligning their coefficients. State
    equality (dict ==, order-blind) did not catch it; predictions do."""
    panel = make_panel(n_days=N_DAYS)
    *_, model = train_artifact_with_model(
        panel, make_plan(), small_config(), code_revision="test-rev"
    )
    frame = build_frame(
        panel, range(N_DAYS - 15, N_DAYS - 1), (1, 4, 7), make_plan(), artifact.catalogue
    )
    trained, served = model.predict(frame), artifact.model.predict(frame)
    assert np.array_equal(served.point, trained.point)
    assert np.array_equal(served.quantiles, trained.quantiles)
    assert list(artifact.model.ridge._cats) == list(CATEGORICAL)


def test_ridge_state_with_unknown_categories_is_refused(artifact: ForecastArtifact) -> None:
    state = artifact.model.ridge.state()
    state["categories"]["colour"] = [0.0]
    with pytest.raises(ValueError, match="categories"):
        RidgeBaseline.from_state(artifact.model.ridge.residuals.levels, state)


def _files(artifact: ForecastArtifact) -> dict[str, str]:
    files = {f"{n}.txt": t for n, t in artifact.model.quantiles.model_strings().items()}
    files["ridge.json"] = json.dumps(
        artifact.model.ridge.state(), sort_keys=True, separators=(",", ":")
    )
    return files


def test_saving_twice_is_a_no_op(artifact_dir: Path, artifact: ForecastArtifact) -> None:
    manifest, files = dict(artifact.manifest), _files(artifact)
    assert save_artifact(manifest, files, artifact_dir.parent) == artifact_dir


@pytest.fixture
def copy(tmp_path: Path, artifact_dir: Path) -> Path:
    dst = tmp_path / "model"
    shutil.copytree(artifact_dir, dst)
    return dst


def _edit_manifest(path: Path, **changes: object) -> None:
    m = json.loads((path / "manifest.json").read_text())
    m.update(changes)
    (path / "manifest.json").write_text(json.dumps(m))


@pytest.mark.parametrize("name", ["q0.5.txt", "ridge.json"])
def test_tampered_model_file_is_rejected(copy: Path, name: str) -> None:
    with (copy / name).open("a") as fh:
        fh.write("\n")
    with pytest.raises(ArtifactError, match=f"checksum mismatch for {name}"):
        load_artifact(copy)


def test_manifest_without_ridge_component_is_rejected(copy: Path) -> None:
    m = json.loads((copy / "manifest.json").read_text())
    del m["files"]["ridge.json"]
    from praxis.forecasting.artifact import _content_checksum

    m["artifact_checksum"] = _content_checksum(m)  # a consistently re-signed manifest
    (copy / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ArtifactError, match=r"ridge\.json"):
        load_artifact(copy)


def test_missing_model_file_is_rejected(copy: Path) -> None:
    (copy / "q0.5.txt").unlink()
    with pytest.raises(ArtifactError, match="missing model file"):
        load_artifact(copy)


def test_tampered_manifest_is_rejected(copy: Path) -> None:
    _edit_manifest(copy, data_version="panel-forged")
    with pytest.raises(ArtifactError, match="manifest checksum"):
        load_artifact(copy)


def test_incompatible_feature_version_is_rejected(copy: Path) -> None:
    _edit_manifest(copy, feature_version="demand_features.v0")
    with pytest.raises(ArtifactError, match="feature version"):
        load_artifact(copy)


def test_unsupported_format_and_broken_json_are_rejected(copy: Path, tmp_path: Path) -> None:
    _edit_manifest(copy, artifact_format=99)
    with pytest.raises(ArtifactError, match="format"):
        load_artifact(copy)
    (copy / "manifest.json").write_text("{not json")
    with pytest.raises(ArtifactError, match="JSON"):
        load_artifact(copy)
    with pytest.raises(ArtifactError, match="no manifest"):
        load_artifact(tmp_path / "empty")


def test_corrupt_existing_copy_is_not_silently_reused(
    copy: Path, artifact: ForecastArtifact
) -> None:
    (copy / "q0.5.txt").write_text("garbage")
    manifest = dict(artifact.manifest)
    target = copy.parent / manifest["model_version"]
    copy.rename(target)
    files = _files(artifact)
    with pytest.raises(ArtifactError):
        save_artifact(manifest, files, target.parent)
