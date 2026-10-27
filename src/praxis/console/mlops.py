"""ML Operations view: champion and challengers, drift, calibration, training history, feature
freshness and model performance.

Read from the model registry tables (``model_versions``, ``model_stage_events``,
``model_validation_reports``, ``model_monitoring_reports``, ``retraining_requests``) and, for
the live serving signals (calibration, drift alarm, forecast age), from the fleet view. The
pre-registered monitoring thresholds come from ``configs/mlops`` so the charts can draw them;
the console never changes them. Every model here is trained on SYNTHETIC data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from praxis.console.context import ConsoleContext
from praxis.console.panels import SYNTHETIC_NOTE, Data, figure, panel, rows
from praxis.mlops.config import load_mlops_config

# Numeric monitoring signals worth a time series (searched anywhere in a report's signals).
SERIES_KEYS = ("vwape_ratio", "coverage_80", "prediction_psi", "worst_slice_ratio", "vwape")

VERSIONS_SQL = """
SELECT model_version, model_name, stage, data_version, feature_version, code_revision,
       artifact_checksum, metrics, params, trained_at, registered_at, stage_changed_at,
       is_synthetic, tracking_run_id
FROM model_versions ORDER BY model_name, registered_at DESC LIMIT 60
"""


def _find(signals: Any, key: str) -> float | None:
    """The first numeric value under ``key`` anywhere in a nested signals document."""
    if isinstance(signals, Mapping):
        value = signals.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        for nested in signals.values():
            found = _find(nested, key)
            if found is not None:
                return found
    return None


def _versions(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(conn.execute(text(VERSIONS_SQL)))
        validations = rows(
            conn.execute(
                text(
                    "SELECT DISTINCT ON (model_version) model_version, passed, failed_checks, "
                    "created_at "
                    "FROM model_validation_reports ORDER BY model_version, created_at DESC"
                )
            )
        )
    if not found:
        return None
    latest = {v["model_version"]: v for v in validations}
    for v in found:
        v["validation"] = latest.get(v["model_version"])
    champions = {v["model_name"]: v["model_version"] for v in found if v["stage"] == "champion"}
    return {"versions": found, "champions": champions}


def _events(ctx: ConsoleContext, limit: int = 40) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT decision_id, model_name, model_version, from_stage, to_stage, trigger, "
                    "actor, reason, occurred_at FROM model_stage_events "
                    "ORDER BY occurred_at DESC, event_id DESC LIMIT :n"
                ),
                {"n": limit},
            )
        )
    return {"events": found} if found else None


def _monitoring(ctx: ConsoleContext, per_kind: int = 60) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT report_id, model_name, model_version, kind, as_of, window_start, "
                    "window_end, fired, signals FROM ("
                    "  SELECT *, row_number() OVER (PARTITION BY model_name, kind "
                    "                               ORDER BY as_of DESC, created_at DESC) AS n "
                    "  FROM model_monitoring_reports) r WHERE n <= :n "
                    "ORDER BY model_name, kind, as_of"
                ),
                {"n": per_kind},
            )
        )
    if not found:
        return None
    for r in found:
        r["series"] = {k: _find(r["signals"], k) for k in SERIES_KEYS}
    mon = load_mlops_config().monitoring
    return {
        "reports": found,
        "thresholds": {
            "coverage_80_band": list(mon.coverage_80),
            "max_vwape_ratio_vs_reference": mon.max_vwape_ratio_vs_reference,
            "psi_alert": mon.psi_alert,
            "source": "configs/mlops (pre-registered, pinned by tests/mlops/test_config.py)",
        },
    }


def _retraining(ctx: ConsoleContext) -> Data | None:
    with ctx.db().connect() as conn:
        found = rows(
            conn.execute(
                text(
                    "SELECT request_id, model_name, status, triggers, as_of, champion_version, "
                    "candidate_version, failure_reason, created_at, updated_at "
                    "FROM retraining_requests ORDER BY created_at DESC LIMIT 20"
                )
            )
        )
    return {"requests": found} if found else None


def _serving(ctx: ConsoleContext) -> Data:
    """Live serving signals from the fleet view; a missing gauge says who should feed it."""
    system = ctx.reader.read(ctx.clock())
    view = system.view
    stale = sorted({p.component for p in system.processes if p.stale})
    tail = f" Stale processes: {', '.join(stale)}." if stale else ""
    feeds = {
        "model_calibration_80": "published by `python -m praxis.mlops run-day` / `operate`",
        "model_drift_alarm": "published by `python -m praxis.mlops run-day` / `operate`",
        "forecast_age_hours": "published by the API's forecast service",
        "data_source_age_s": "pulled from the warehouse by the ops collectors",
    }

    def gauge(metric: str, unit: str) -> Data:
        return figure(
            view.peak(metric),
            unit=unit,
            reason=f"No process has published {metric} ({feeds[metric]}).{tail}",
        )

    versions = sorted(
        {p.labels.get("model_version", "") for p in view.select("model_version_info")}
    )
    return {
        "calibration_80": gauge("model_calibration_80", "ratio"),
        "drift_alarm": gauge("model_drift_alarm", "flag"),
        "forecast_age_hours": gauge("forecast_age_hours", "hours"),
        "oldest_source_age_s": gauge("data_source_age_s", "seconds"),
        "serving_versions": [v for v in versions if v],
        "inputs": system.inputs(),
    }


def mlops_view(ctx: ConsoleContext) -> Data:
    none = "no model is registered (`python -m praxis.mlops register ...` or `make mlops-drill`)"
    return {
        "view": "mlops",
        "generated_at": ctx.clock().isoformat(),
        "data_origin": "synthetic",
        "note": f"Models are trained and monitored on SYNTHETIC data. {SYNTHETIC_NOTE}",
        "panels": {
            "versions": panel(
                "postgres: model_versions, model_validation_reports",
                lambda: _versions(ctx),
                empty=none,
            ),
            "events": panel("postgres: model_stage_events", lambda: _events(ctx), empty=none),
            "monitoring": panel(
                "postgres: model_monitoring_reports",
                lambda: _monitoring(ctx),
                empty="no drift, shadow or probation report has been written",
            ),
            "retraining": panel(
                "postgres: retraining_requests",
                lambda: _retraining(ctx),
                empty="no retraining has been requested",
            ),
            "serving": panel(
                "fleet: ops_metric_snapshots + collectors", lambda: _serving(ctx), empty=""
            ),
        },
    }
