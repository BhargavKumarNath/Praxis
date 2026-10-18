"""Experiment tracking: MLflow mirror of the registry (ADR 0017).

Postgres decides which version is champion; MLflow mirrors it so experiments, runs,
parameters, metrics, artifacts and aliases can be browsed with standard tooling. A tracker
failure never blocks or reverses a registry decision: it is logged, counted and repaired by
``sync`` (which re-derives every alias from the registry).

MLflow is imported lazily: importing ``praxis.mlops`` never loads it, and a process without
a tracking URI uses ``NullTracker``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.store import VersionRow

logger = logging.getLogger(__name__)

# Stages mirrored as MLflow aliases (an alias names exactly one version).
ALIASED_STAGES = (ModelStage.CHAMPION, ModelStage.SHADOW, ModelStage.CANARY)


class TrackingError(RuntimeError):
    """The tracking backend rejected or failed an operation."""


class ExperimentTracker(Protocol):
    def log_version(self, row: VersionRow, files: Mapping[str, Path]) -> str | None:
        """Record one run for a registered version; returns the run id (None = not tracked)."""
        ...

    def log_validation(self, run_id: str, report: Mapping[str, Any]) -> None:
        """Attach the offline validation report (metrics + JSON) to the version's run."""
        ...

    def set_aliases(self, model_name: str, aliases: Mapping[str, str | None]) -> None:
        """Point each alias (stage name) at a run's model version, or remove it (None)."""
        ...


class NullTracker:
    """No tracking backend configured: the registry alone is the record."""

    def log_version(self, row: VersionRow, files: Mapping[str, Path]) -> str | None:
        return None

    def log_validation(self, run_id: str, report: Mapping[str, Any]) -> None:
        return None

    def set_aliases(self, model_name: str, aliases: Mapping[str, str | None]) -> None:
        return None


def flatten(prefix: str, value: Any, out: dict[str, Any]) -> dict[str, Any]:
    """Nested mapping -> dotted keys (MLflow params / metrics are flat)."""
    if isinstance(value, Mapping):
        for key in sorted(value):
            flatten(f"{prefix}.{key}" if prefix else str(key), value[key], out)
    else:
        out[prefix] = value
    return out


def numeric_metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
    flat = flatten("", metrics, {})
    return {
        k: float(v)
        for k, v in flat.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }


class MlflowTracker:
    """MLflow tracking + model registry (local SQLite or a server URI; never a paid service)."""

    def __init__(self, tracking_uri: str, artifact_root: Path, experiment: str) -> None:
        from mlflow import MlflowClient  # lazy: heavy import, optional backend

        self.client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
        self.artifact_root = artifact_root
        self.experiment = experiment

    def _experiment_id(self) -> str:
        found = self.client.get_experiment_by_name(self.experiment)
        if found is not None:
            return str(found.experiment_id)
        location = (self.artifact_root / self.experiment).resolve().as_uri()
        return str(self.client.create_experiment(self.experiment, artifact_location=location))

    def _registered_model(self, name: str) -> None:
        from mlflow.exceptions import MlflowException

        try:
            self.client.get_registered_model(name)
        except MlflowException:
            self.client.create_registered_model(name, description="Praxis (SYNTHETIC data)")

    def log_version(self, row: VersionRow, files: Mapping[str, Path]) -> str | None:
        rec = row.record
        run = self.client.create_run(
            self._experiment_id(),
            run_name=rec.model_version,
            tags={
                "praxis.model_name": rec.model_name,
                "praxis.model_version": rec.model_version,
                "praxis.data_version": rec.data_version,
                "praxis.feature_version": rec.feature_version,
                "praxis.code_revision": rec.code_revision,
                "praxis.artifact_checksum": rec.artifact_checksum,
                "praxis.artifact_uri": rec.artifact_uri,
                "praxis.trained_at": rec.trained_at.isoformat(),
                "praxis.is_synthetic": str(rec.is_synthetic).lower(),
                "praxis.lineage": json.dumps(rec.lineage, sort_keys=True, default=str)[:4900],
            },
        )
        run_id = str(run.info.run_id)
        for key, value in sorted(flatten("", rec.params, {}).items()):
            self.client.log_param(run_id, key[:250], str(value)[:6000])
        for key, value in sorted(numeric_metrics(rec.metrics).items()):
            self.client.log_metric(run_id, key[:250], value)
        for name, path in sorted(files.items()):
            self.client.log_artifact(run_id, str(path), artifact_path=name)
        self.client.set_terminated(run_id)
        self._registered_model(rec.model_name)
        self.client.create_model_version(
            rec.model_name,
            source=rec.artifact_uri,
            run_id=run_id,
            tags={"praxis.model_version": rec.model_version, "praxis.stage": row.stage.value},
        )
        return run_id

    def log_validation(self, run_id: str, report: Mapping[str, Any]) -> None:
        checks = {c["check"]: c for c in report.get("checks", [])}
        for name, check in sorted(checks.items()):
            self.client.log_metric(
                run_id, f"validation.{name}.passed"[:250], float(check["passed"])
            )
            value = check.get("value")
            if isinstance(value, int | float) and not isinstance(value, bool):
                self.client.log_metric(run_id, f"validation.{name}.value"[:250], float(value))
        self.client.set_tag(run_id, "praxis.validation_passed", str(report["passed"]).lower())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "validation.json"
            path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
            self.client.log_artifact(run_id, str(path))

    def _mlflow_version(self, model_name: str, run_id: str) -> str | None:
        found = self.client.search_model_versions(f"name='{model_name}' and run_id='{run_id}'")
        return str(found[0].version) if found else None

    def set_aliases(self, model_name: str, aliases: Mapping[str, str | None]) -> None:
        from mlflow.exceptions import MlflowException

        self._registered_model(model_name)
        for alias, run_id in sorted(aliases.items()):
            version = self._mlflow_version(model_name, run_id) if run_id else None
            if version is None:
                with contextlib.suppress(MlflowException):  # alias not set: nothing to remove
                    self.client.delete_registered_model_alias(model_name, alias)
                continue
            self.client.set_registered_model_alias(model_name, alias, version)

    def alias(self, model_name: str, alias: str) -> str | None:
        """The Praxis model version an alias points at (None if unset)."""
        from mlflow.exceptions import MlflowException

        try:
            mv = self.client.get_model_version_by_alias(model_name, alias)
        except MlflowException:
            return None
        return str(mv.tags.get("praxis.model_version"))
