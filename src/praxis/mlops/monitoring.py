"""Champion monitoring: drift signals, retraining triggers and post-promotion probation.

Drift (``[monitoring]``, evaluated as of a data date D, actuals known through D-1):

* residual drift: pooled vWAPE of the champion's out-of-sample forecasts whose target falls
  in the trailing window, divided by the champion's own backtest vWAPE; and the worst
  region / product / segment slice ratio against the backtest slice (segment degradation);
* calibration drift: empirical 80% interval coverage outside the band;
* feature / prediction drift: PSI of monitored features (one-step rows at origins in the
  window) and of the scaled point forecast, on bins fixed at registration.

A decision signal over its threshold on one day is a BREACH (reported). It becomes an ALARM
only when the same signal breached on ``persistence_days`` consecutive evaluated days of the
same champion (ADR 0017, amendment 2026-10-18): consecutive 14-day windows share 13 days,
so a single extreme day can lift one window; a real change keeps lifting them.

Drift is EVIDENCE: an alarm (or an investigation signal) opens (or joins) a retraining
request. It never promotes anything.
Retraining is also requested on schedule (data age) or when enough new labels exist.

Probation: for ``[probation].days`` after a promotion the replaced champion is scored on the
same matured observations; a new champion clearly worse than it is rolled back.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.features import build_frame
from praxis.mlops import drift
from praxis.mlops.config import MlopsConfig
from praxis.mlops.scoring import (
    IncompatibleVersions,
    WarehouseData,
    check_compatible,
    matured_origins,
    metrics,
    observations,
    predict,
    training_end,
    worst_slice_ratio,
)
from praxis.mlops.training import PREDICTION_COLUMN

# The champion is measurably worse on matured outcomes (vs investigation-only PSI signals).
DECISION_SIGNALS = frozenset({"residual_drift", "segment_drift", "calibration_drift"})
DRIFT_KINDS = (
    "residual_drift",
    "segment_drift",
    "calibration_drift",
    "feature_drift",
    "prediction_drift",
)


def _residual(
    art: ForecastArtifact,
    reference: dict[str, Any],
    data: WarehouseData,
    as_of: date,
    cfg: MlopsConfig,
) -> dict[str, Any]:
    mon = cfg.monitoring
    panel = data.panel
    oos = training_end(art) + timedelta(days=1)
    lo = panel.day_of(max(as_of - timedelta(days=mon.window_days), oos))
    hi = panel.day_of(as_of - timedelta(days=1))
    first_origin = max(lo - max(art.config.target.horizons), panel.day_of(training_end(art)))
    obs = observations(art, data, range(first_origin, hi), target_window=(lo, hi))
    rows = 0 if obs is None else len(obs)
    out: dict[str, Any] = {
        "rows": rows,
        "window": [panel.date_of(lo).isoformat(), panel.date_of(hi).isoformat()],
    }
    if obs is None or rows < mon.min_rows:
        out["status"] = "insufficient_data"
        return out
    m = metrics(obs, predict(art, obs))
    ref = reference["metrics"]
    ratio = m["overall"]["vwape"] / ref["overall"]["vwape"]
    worst, label = worst_slice_ratio(m["slices"], ref["slices"], mon.min_slice_rows)
    cov = m["overall"]["coverage_80"]
    lo_c, hi_c = mon.coverage_80
    out.update(
        status="evaluated",
        vwape=round(m["overall"]["vwape"], 6),
        reference_vwape=round(ref["overall"]["vwape"], 6),
        vwape_ratio=round(ratio, 6),
        worst_slice=label,
        worst_slice_ratio=round(worst, 6),
        coverage_80=round(cov, 6),
        fired=sorted(
            k
            for k, hit in (
                ("residual_drift", ratio > mon.max_vwape_ratio_vs_reference),
                ("segment_drift", worst > mon.max_slice_vwape_ratio_vs_reference),
                ("calibration_drift", not lo_c <= cov <= hi_c),
            )
            if hit
        ),
    )
    return out


def _distribution(
    art: ForecastArtifact,
    reference: dict[str, Any],
    data: WarehouseData,
    as_of: date,
    cfg: MlopsConfig,
) -> dict[str, Any]:
    mon = cfg.monitoring
    panel = data.panel
    lo = panel.day_of(max(as_of - timedelta(days=mon.window_days), training_end(art)))
    hi = min(panel.day_of(as_of - timedelta(days=1)), panel.n_days - 1)
    if hi < lo:
        return {"status": "insufficient_data", "fired": []}
    frame = build_frame(
        panel,
        range(lo, hi + 1),
        (1,),
        data.plan,
        art.catalogue,
        external=art.config.features.external,
    )
    pred = art.model.predict(frame)
    columns = {f"feature.{n}": frame.column(n) for n in mon.features}
    columns[PREDICTION_COLUMN] = pred.point / frame.scale
    bins = reference["bins"]
    features = drift.compare(
        {k: v for k, v in bins.items() if k != PREDICTION_COLUMN},
        columns,
        threshold=mon.psi_alert,
        alpha=mon.psi_alpha,
    )
    prediction = drift.compare(
        {PREDICTION_COLUMN: bins[PREDICTION_COLUMN]},
        columns,
        threshold=mon.psi_alert,
        alpha=mon.psi_alpha,
    )
    fired = (["feature_drift"] if features["fired"] else []) + (
        ["prediction_drift"] if prediction["fired"] else []
    )
    return {
        "status": "evaluated",
        "rows": len(frame),
        "feature_psi": features,
        "prediction_psi": prediction["psi"].get(PREDICTION_COLUMN),
        "fired": fired,
    }


def persistent(today: Iterable[str], history: Sequence[Iterable[str]], days: int) -> list[str]:
    """Decision signals breached today AND on each of the previous ``days - 1`` days.

    ``history``: the breaches of the immediately preceding evaluated days, most recent first
    (a gap or a day without an evaluation ends the run)."""
    if len(history) < days - 1:
        return []
    alarms = set(today) & DECISION_SIGNALS
    for previous in history[: days - 1]:
        alarms &= set(previous)
    return sorted(alarms)


def drift_check(
    art: ForecastArtifact,
    reference: dict[str, Any],
    data: WarehouseData,
    as_of: date,
    cfg: MlopsConfig,
    history: Sequence[Iterable[str]] = (),
) -> dict[str, Any]:
    """Signals for the champion as of ``as_of`` plus the retraining triggers that apply.

    ``history``: decision breaches of the previous consecutive days (see ``persistent``)."""
    residual = _residual(art, reference, data, as_of, cfg)
    dist = _distribution(art, reference, data, as_of, cfg)
    fired = sorted(set(residual.get("fired", [])) | set(dist["fired"]))
    alarms = persistent(fired, history, cfg.monitoring.persistence_days)
    age = (as_of - timedelta(days=1) - training_end(art)).days
    labels = int(max(age, 0) * len(art.series))
    rt = cfg.retraining
    routine = [
        k
        for k, hit in (
            ("scheduled", age >= rt.scheduled_every_days),
            ("new_labels", labels >= rt.min_new_labels),
        )
        if hit
    ]
    # persisted decision alarms + investigation signals (PSI) + schedule / labels
    triggers = alarms + sorted(set(dist["fired"])) + routine
    return {
        "model_version": art.model_version,
        "as_of": as_of.isoformat(),
        "residual": residual,
        "distribution": dist,
        "data_age_days": age,
        "new_labels": labels,
        "drift_fired": fired,
        "breaches": sorted(set(fired) & DECISION_SIGNALS),
        "alarms": alarms,
        "persistence_days": cfg.monitoring.persistence_days,
        "retraining_triggers": triggers,
        "policy_version": cfg.policy_version,
    }


def probation_check(
    champion: ForecastArtifact,
    previous: ForecastArtifact,
    data: WarehouseData,
    promoted_as_of: date,
    as_of: date,
    cfg: MlopsConfig,
) -> dict[str, Any]:
    """New champion vs the champion it replaced, on the same matured observations."""
    prob = cfg.probation
    out: dict[str, Any] = {
        "champion": champion.model_version,
        "previous": previous.model_version,
        "promoted_as_of": promoted_as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "active": as_of <= promoted_as_of + timedelta(days=prob.days),
        "fired": False,
    }
    try:
        check_compatible([previous, champion])
    except IncompatibleVersions as exc:
        out["status"] = f"incomparable: {exc}"
        return out
    first = max(promoted_as_of - timedelta(days=1), training_end(champion), training_end(previous))
    origins = matured_origins(
        data.panel,
        first=first,
        as_of=as_of,
        max_horizon=max(champion.config.target.horizons),
        full=True,
    )
    obs = observations(champion, data, origins) if origins else None
    out["origins"] = 0 if obs is None else len(obs.origins)
    out["rows"] = 0 if obs is None else len(obs)
    if obs is None or len(obs.origins) < prob.min_origins:
        out["status"] = "insufficient_data"
        return out
    new = metrics(obs, predict(champion, obs))["overall"]
    old = metrics(obs, predict(previous, obs))["overall"]
    ratio = new["vwape"] / old["vwape"]
    out.update(
        status="evaluated",
        vwape=round(new["vwape"], 6),
        previous_vwape=round(old["vwape"], 6),
        vwape_ratio=round(ratio, 6),
        threshold=prob.max_vwape_ratio_vs_previous,
        fired=bool(out["active"] and ratio > prob.max_vwape_ratio_vs_previous),
    )
    return out


def finite_reference(reference: dict[str, Any] | None) -> bool:
    """A monitoring reference has bins and backtest metrics with a positive vWAPE."""
    if not reference or "bins" not in reference or "metrics" not in reference:
        return False
    v = reference["metrics"]["overall"].get("vwape")
    return isinstance(v, int | float) and bool(np.isfinite(v)) and v > 0
