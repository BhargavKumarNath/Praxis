"""MLflow mirror on a real local SQLite tracking store (no network, no server)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.tracking import MlflowTracker, flatten, numeric_metrics
from tests.mlops.test_registry import DAY, A, B, passed, rec, shadow_report

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def tracker(tmp_path_factory: pytest.TempPathFactory) -> MlflowTracker:
    root = tmp_path_factory.mktemp("mlflow")
    return MlflowTracker(f"sqlite:///{root / 'mlflow.db'}", root / "artifacts", "praxis-test")


def test_flatten_and_numeric_metrics() -> None:
    nested = {"a": {"b": 1, "c": {"d": 2.5}}, "flag": True, "name": "x"}
    assert flatten("", nested, {}) == {"a.b": 1, "a.c.d": 2.5, "flag": True, "name": "x"}
    assert numeric_metrics(nested) == {"a.b": 1.0, "a.c.d": 2.5}


def test_registry_lineage_and_stages_are_mirrored(
    make_registry: Callable[..., ModelRegistry], tracker: MlflowTracker, tmp_path: Path
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    registry = make_registry(tracker=tracker)
    registry.register(rec(A), files={"artifact": manifest})
    registry.record_validation(A, passed(A))
    registry.promote(A, approver="owner", reason="bootstrap", as_of=DAY)
    row = registry.store.require(A)
    run = tracker.client.get_run(str(row.tracking_run_id))
    tags = run.data.tags
    assert tags["praxis.data_version"] == row.record.data_version
    assert tags["praxis.feature_version"] == row.record.feature_version
    assert tags["praxis.code_revision"] == row.record.code_revision
    assert tags["praxis.artifact_checksum"] == row.record.artifact_checksum
    assert tags["praxis.validation_passed"] == "true"
    assert run.data.params["ridge.alpha"] == "1.0"
    assert run.data.metrics["backtest.hybrid.vwape"] == pytest.approx(0.12)
    assert run.data.metrics["validation.x.passed"] == 1.0
    assert tracker.alias("demand_forecaster", "champion") == A

    registry.register(rec(B))
    registry.record_validation(B, passed(B))
    registry.start_shadow(B, as_of=DAY)
    assert tracker.alias("demand_forecaster", "shadow") == B
    registry.complete_shadow(B, shadow_report(A, B, ok=True))
    registry.promote(B, approver="owner", reason="ok", as_of=DAY)
    assert tracker.alias("demand_forecaster", "champion") == B
    assert tracker.alias("demand_forecaster", "shadow") is None
    registry.rollback(actor="oncall", reason="drill", as_of=DAY)
    assert tracker.alias("demand_forecaster", "champion") == A
    assert registry.store.require(B).stage == ModelStage.ROLLED_BACK
