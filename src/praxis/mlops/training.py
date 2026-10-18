"""Continuous training for the demand forecaster: candidate artifacts with full lineage.

A retraining run fits the Phase 4 champion recipe (hybrid) on every target observed up to
the request's data date, runs the pre-registered rolling-origin backtest on that same panel,
and saves the artifact as a CANDIDATE even when the backtest fails: a failing candidate must
be registered and rejected by the validation gate (and audited), not silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from praxis.forecasting.artifact import ForecastArtifact, load_artifact, save_artifact
from praxis.forecasting.artifact import train_artifact as fit_artifact
from praxis.forecasting.backtest import run_backtest
from praxis.forecasting.config import ForecastConfig
from praxis.mlops import drift
from praxis.mlops.config import Monitoring
from praxis.mlops.scoring import WarehouseData, observations, predict, training_end
from praxis.mlops.store import VersionRecord

PREDICTION_COLUMN = "prediction.scaled_point"


@dataclass(frozen=True)
class Candidate:
    path: Path
    artifact: ForecastArtifact
    backtest: dict[str, Any]
    record: VersionRecord


def reference_profile(
    artifact: ForecastArtifact, data: WarehouseData, monitoring: Monitoring
) -> dict[str, Any]:
    """Feature and prediction bins over the last ``reference_days`` origins of training."""
    end = data.panel.day_of(training_end(artifact))
    origins = list(range(max(end - monitoring.reference_days, 0), end))
    obs = observations(artifact, data, origins)
    if obs is None:
        raise ValueError("training window too short for a monitoring reference")
    one_step = obs.frame.horizon == 1
    pred = predict(artifact, obs)
    columns = {f"feature.{n}": obs.frame.column(n)[one_step] for n in monitoring.features}
    columns[PREDICTION_COLUMN] = (pred.point / obs.frame.scale)[one_step]
    return {
        "bins": drift.profile(columns, monitoring.psi_bins),
        "origins": [
            data.panel.date_of(origins[0]).isoformat(),
            data.panel.date_of(origins[-1]).isoformat(),
        ],
        "rows": int(one_step.sum()),
    }


def version_record(
    artifact: ForecastArtifact,
    path: Path,
    backtest: dict[str, Any] | None,
    reference: dict[str, Any] | None,
) -> VersionRecord:
    m = artifact.manifest
    candidate = m["champion"]["kind"]
    metrics: dict[str, Any] = {}
    if backtest is not None:
        metrics = {
            "backtest": {name: res["overall"] for name, res in backtest["models"].items()},
            "backtest_acceptance_passed": bool(backtest.get("acceptance", {}).get("passed")),
            "backtest_origins": len(backtest["origins"]),
        }
    ref = dict(reference or {})
    if backtest is not None and candidate in backtest["models"]:
        ref["metrics"] = {
            "overall": backtest["models"][candidate]["overall"],
            "slices": {
                g: backtest["models"][candidate]["slices"][g]
                for g in ("region", "product", "segment")
            },
        }
    return VersionRecord(
        model_name=str(m["model_name"]),
        model_version=artifact.model_version,
        artifact_uri=path.resolve().as_uri(),
        artifact_checksum=str(m["artifact_checksum"]),
        data_version=str(m["data_version"]),
        feature_version=str(m["feature_version"]),
        code_revision=str(m["code_revision"]),
        params={"forecast_config": m["config"], "champion": m["champion"]},
        metrics=metrics,
        lineage={
            "training_start": m["training"]["panel_start"],
            "training_end": m["training"]["last_target_date"],
            "panel_end": m["training"]["panel_end"],
            "rows": m["training"]["rows"],
            "config_hash": m["config_hash"],
            "price_plan_version": m["price_plan_version"],
            "series": len(m["series"]),
            "artifact_format": m["artifact_format"],
            "backtest_data_version": None if backtest is None else backtest["data_version"],
        },
        trained_at=datetime.fromisoformat(str(m["created_at"])),
        is_synthetic=bool(m.get("is_synthetic", True)),
        reference=ref or None,
    )


def train_candidate(
    data: WarehouseData,
    cfg: ForecastConfig,
    monitoring: Monitoring,
    out_root: Path,
    *,
    code_revision: str,
    now: datetime | None = None,
) -> Candidate:
    """Backtest + fit on ``data`` (every observed target), save, profile, describe."""
    report = run_backtest(data.panel, data.plan, cfg)
    manifest, files = fit_artifact(
        data.panel, data.plan, cfg, code_revision=code_revision, backtest=report, now=now
    )
    path = save_artifact(manifest, files, out_root)
    artifact = load_artifact(path)
    reference = reference_profile(artifact, data, monitoring)
    return Candidate(path, artifact, report, version_record(artifact, path, report, reference))


def utc_midnight(d: date) -> datetime:
    """Logical creation time for an artifact trained at the end of data day ``d``."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
