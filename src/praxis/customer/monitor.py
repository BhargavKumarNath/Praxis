"""Segment monitoring: retention, churn, value and price exposure per segment at a date.

Everything is computed from data before ``as_of`` (the report is reproducible at any date):

* churn over the last label window: the cohort active at ``as_of - window`` and its voluntary /
  involuntary churn since, vs the model's (uncalibrated) expectation for the same person-time;
* retention: share of the cohort active ``retention_days`` ago still active now, per segment;
* value: calibrated CLV and margin per segment (``praxis.customer.value``), plus the CLV
  sensitivity to the discount rate and horizon (configs/customer/value.toml);
* price exposure: customers by the number of price-change dates seen in the look-back window.

Alerts (structured log ``customer.segment_alert`` + the report) fire when a segment with enough
customers churns above ``max_window_churn`` or its observed / expected ratio leaves
[1 / max_calibration_ratio, max_calibration_ratio]. Phase 11 wires them to alerting.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np

from praxis.customer.artifact import CustomerArtifact
from praxis.customer.clv import sensitivity
from praxis.customer.config import CustomerValueConfig
from praxis.customer.features import feature_matrix
from praxis.customer.value import CustomerValues, customer_values
from praxis.customer.warehouse import load_outcomes, load_snapshot

logger = logging.getLogger(__name__)


def _cohort(
    con: duckdb.DuckDBPyConnection,
    artifact: CustomerArtifact,
    as_of: date,
    days: int,
    cfg: CustomerValueConfig,
) -> dict[str, dict[str, float]]:
    """Per segment: customers at ``as_of - days``, churn since, model expectation, retention."""
    start = as_of - timedelta(days=days)
    snap = load_snapshot(con, start, cfg.features)
    out = load_outcomes(con, snap.ids, start, days)
    x = feature_matrix(snap, cfg.features.trend_half_days)
    mu = artifact.champion.predict(x, snap.tier) * out.exposure_days
    tiers = np.array(snap.tier)
    rows: dict[str, dict[str, float]] = {}
    for s in cfg.segments.names:
        m = tiers == s
        n = float(m.sum())
        churned = float(out.voluntary[m].sum() + out.involuntary[m].sum())
        rows[s] = {
            "customers": n,
            "voluntary": float(out.voluntary[m].sum()),
            "involuntary": float(out.involuntary[m].sum()),
            "expected_voluntary": float(mu[m].sum()),
            "retention": (n - churned) / n if n else 1.0,
        }
    return rows


def _alerts(segments: dict[str, dict[str, Any]], cfg: CustomerValueConfig) -> list[dict[str, Any]]:
    mon = cfg.monitoring
    alerts = []
    for name, seg in segments.items():
        w = seg["last_window"]
        if w["customers"] < mon.min_customers:
            continue
        rate = w["voluntary"] / w["customers"]
        if rate > mon.max_window_churn:
            alerts.append({"segment": name, "alert": "window_churn", "value": rate})
        ratio = seg["calibration_ratio"]
        if ratio is not None and not 1.0 / mon.max_calibration_ratio <= ratio <= (
            mon.max_calibration_ratio
        ):
            alerts.append({"segment": name, "alert": "calibration_ratio", "value": ratio})
    return alerts


def segment_report(
    con: duckdb.DuckDBPyConnection,
    artifact: CustomerArtifact,
    as_of: date,
    cfg: CustomerValueConfig,
    *,
    retention_days: int = 90,
    price_change_window_days: int = 90,
) -> dict[str, Any]:
    values = customer_values(con, artifact, as_of, cfg, price_history_days=retention_days)
    window = _cohort(con, artifact, as_of, cfg.label.window_days, cfg)
    retention = _cohort(con, artifact, as_of, retention_days, cfg)
    segments: dict[str, dict[str, Any]] = {}
    for name, value in values.segments(cfg.segments.names).items():
        w = window[name]
        ratio = (w["voluntary"] + 0.5) / (w["expected_voluntary"] + 0.5)
        segments[name] = {
            "value": value.record(),
            "last_window": w,
            "calibration_ratio": ratio if w["customers"] else None,
            f"retention_{retention_days}d": retention[name]["retention"],
            "price_change_exposure": _exposure(values, name, price_change_window_days),
            "clv_sensitivity": _sensitivity(values, name, cfg),
        }
    alerts = _alerts(segments, cfg)
    for a in alerts:
        logger.warning("customer.segment_alert", extra={**a, "as_of": as_of.isoformat()})
    return {
        "is_synthetic": True,
        "as_of": as_of.isoformat(),
        "model_version": artifact.model_version,
        "champion": values.champion,
        "calibration": None if values.calibration is None else values.calibration.record(),
        "customers": values.snapshot.n,
        "segments": segments,
        "alerts": alerts,
    }


def _exposure(values: CustomerValues, segment: str, days: int) -> dict[str, int]:
    tiers = np.array(values.snapshot.tier)
    counts = values.snapshot.price_changes_within(days)[tiers == segment]
    return {str(k): int((counts == k).sum()) for k in sorted(set(counts.tolist()))}


def _sensitivity(
    values: CustomerValues, segment: str, cfg: CustomerValueConfig
) -> list[dict[str, float]]:
    m = np.array(values.snapshot.tier) == segment
    points = sensitivity(
        values.margin_micros[m],
        values.voluntary_hazard[m] + values.involuntary_hazard[m],
        rates=cfg.clv.sensitivity_discount_rates,
        horizons=cfg.clv.sensitivity_horizons_days,
    )
    return [
        {"annual_rate": p.annual_rate, "horizon_days": p.horizon_days, "mean_clv": p.mean_micros}
        for p in points
    ]
