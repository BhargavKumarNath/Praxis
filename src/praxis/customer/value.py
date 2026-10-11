"""Customer values at a decision date: calibrated churn hazards, CLV, segment value.

``customer_values`` applies a trained artifact to the snapshot at ``as_of``:

1. the champion's voluntary daily hazard per customer;
2. recent calibration (ADR 0016): the same model applied to past snapshots, compared with the
   voluntary churn observed since (all of it before ``as_of``). "level": the last label window,
   factor = (O + 0.5) / (E + 0.5), SD(log factor) = 1 / sqrt(O + 0.5). "trend" (second
   amendment): weekly windows, observed ~ Poisson(expected x exp(a + b t)), forecast to the
   middle of the decision week. Price multiplies the hazard, so a pricing decision needs the
   level that will HOLD while it is scored (the seed-42 Phase 6 lesson, and the lag found on
   development worlds);
3. involuntary hazard per segment (rate table);
4. CLV per customer (``praxis.customer.clv``) with margin = daily contribution x (1 - payment
   loss), hazard = calibrated voluntary + involuntary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
from numpy.typing import NDArray

from praxis import glm
from praxis.customer.artifact import CustomerArtifact
from praxis.customer.clv import expected_days, lifetime_value, window_probability
from praxis.customer.config import CustomerValueConfig
from praxis.customer.features import feature_matrix
from praxis.customer.hazard import HazardModel
from praxis.customer.warehouse import Snapshot, load_outcomes, load_snapshot

F64 = NDArray[np.float64]


@dataclass(frozen=True)
class RecentCalibration:
    window_start: date
    observed: float  # voluntary churners in [window_start, as_of)
    expected: float  # the model's expectation for the same customers and person-time
    factor: float
    sd_log: float
    trend_per_week: float = 0.0  # log factor change per week ("trend" method)
    method: str = "level"

    def record(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "observed": self.observed,
            "expected": self.expected,
            "factor": self.factor,
            "sd_log": self.sd_log,
            "trend_per_week": self.trend_per_week,
            "method": self.method,
        }


NO_CALIBRATION_SD = 0.0


def recent_calibration(
    con: duckdb.DuckDBPyConnection,
    model: HazardModel,
    as_of: date,
    cfg: CustomerValueConfig,
) -> RecentCalibration:
    level = _level_calibration(con, model, as_of, cfg)
    if cfg.calibration.method == "level":
        return level
    weeks = [
        _window(con, model, as_of - timedelta(days=7 * k), 7, cfg)
        for k in range(1, cfg.calibration.weeks + 1)
    ]
    observed = np.array([o for o, _ in weeks])
    expected = np.array([e for _, e in weeks])
    if observed.sum() < cfg.calibration.min_events or np.any(expected <= 0):
        return level
    # week midpoints relative to as_of, in weeks (negative = past)
    t = np.array([-(k - 0.5) for k in range(1, cfg.calibration.weeks + 1)])
    try:
        fit = glm.fit_poisson(
            np.column_stack([np.ones(len(t)), t]),
            observed,
            expected,
            penalty=np.array([0.0, 1.0 / cfg.calibration.slope_prior_sd**2]),
        )
    except glm.FitError:
        return level
    at = np.array([1.0, cfg.calibration.horizon_days / 7.0])
    log_factor = float(at @ fit.coef)
    sd_log = float(math.sqrt(max(at @ fit.cov @ at, 0.0)))
    return RecentCalibration(
        as_of - timedelta(days=7 * cfg.calibration.weeks),
        float(observed.sum()),
        float(expected.sum()),
        math.exp(log_factor),
        sd_log,
        trend_per_week=float(fit.coef[1]),
        method="trend",
    )


def _window(
    con: duckdb.DuckDBPyConnection,
    model: HazardModel,
    start: date,
    days: int,
    cfg: CustomerValueConfig,
) -> tuple[float, float]:
    """(observed voluntary churners, model expectation) for the customers active at ``start``."""
    snap = load_snapshot(con, start, cfg.features)
    out = load_outcomes(con, snap.ids, start, days)  # churn in [start, start + days)
    x = feature_matrix(snap, cfg.features.trend_half_days)
    return float(out.voluntary.sum()), float(
        (model.predict(x, snap.tier) * out.exposure_days).sum()
    )


def _level_calibration(
    con: duckdb.DuckDBPyConnection,
    model: HazardModel,
    as_of: date,
    cfg: CustomerValueConfig,
) -> RecentCalibration:
    w = cfg.label.window_days
    start = as_of - timedelta(days=w)
    observed, expected = _window(con, model, start, w, cfg)  # all before as_of
    return RecentCalibration(
        start,
        observed,
        expected,
        (observed + 0.5) / (expected + 0.5),
        1.0 / math.sqrt(observed + 0.5),
    )


@dataclass(frozen=True)
class SegmentValue:
    segment: str
    customers: int
    mean_clv_micros: float
    total_clv_micros: float
    mean_daily_margin_micros: float
    mean_window_churn: float  # voluntary, calibrated, over the label window
    involuntary_hazard: float  # daily
    mean_expected_days: float  # expected active days within the CLV horizon

    def record(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "customers": self.customers,
            "mean_clv_micros": self.mean_clv_micros,
            "total_clv_micros": self.total_clv_micros,
            "mean_daily_margin_micros": self.mean_daily_margin_micros,
            "mean_window_churn": self.mean_window_churn,
            "involuntary_hazard": self.involuntary_hazard,
            "mean_expected_days": self.mean_expected_days,
        }


@dataclass(frozen=True)
class CustomerValues:
    snapshot: Snapshot
    model_version: str
    champion: str
    window_days: int
    voluntary_hazard: F64  # daily, calibrated
    involuntary_hazard: F64  # daily
    margin_micros: F64  # daily contribution net of payment loss
    clv_micros: F64
    calibration: RecentCalibration | None
    horizon_days: int
    annual_rate: float

    def window_churn(self) -> F64:
        """P(voluntary churn within the label window) per customer (calibrated)."""
        return window_probability(self.voluntary_hazard, self.window_days)

    def segments(self, names: tuple[str, ...]) -> dict[str, SegmentValue]:
        tiers = np.array(self.snapshot.tier)
        days = expected_days(self.voluntary_hazard + self.involuntary_hazard, self.horizon_days)
        q = self.window_churn()
        out: dict[str, SegmentValue] = {}
        for s in names:
            m = tiers == s
            n = int(m.sum())
            out[s] = SegmentValue(
                s,
                n,
                float(self.clv_micros[m].mean()) if n else 0.0,
                float(self.clv_micros[m].sum()),
                float(self.margin_micros[m].mean()) if n else 0.0,
                float(q[m].mean()) if n else 0.0,
                float(self.involuntary_hazard[m].mean()) if n else 0.0,
                float(days[m].mean()) if n else 0.0,
            )
        return out


def customer_values(
    con: duckdb.DuckDBPyConnection,
    artifact: CustomerArtifact,
    as_of: date,
    cfg: CustomerValueConfig,
    *,
    price_history_days: int = 365,
) -> CustomerValues:
    snap = load_snapshot(con, as_of, cfg.features, price_history_days=price_history_days)
    model = artifact.champion
    x = feature_matrix(snap, cfg.features.trend_half_days)
    hazard = model.predict(x, snap.tier)
    cal = recent_calibration(con, model, as_of, cfg) if cfg.calibration.enabled else None
    if cal is not None:
        hazard = hazard * cal.factor
    involuntary = artifact.involuntary.predict(x, snap.tier)
    margin = snap.daily_margin_micros() * (1.0 - snap.payment_loss_rate)
    clv = lifetime_value(
        margin,
        hazard + involuntary,
        horizon_days=cfg.clv.horizon_days,
        annual_rate=cfg.clv.annual_discount_rate,
    )
    return CustomerValues(
        snapshot=snap,
        model_version=artifact.model_version,
        champion=model.name,
        window_days=cfg.label.window_days,
        voluntary_hazard=hazard,
        involuntary_hazard=involuntary,
        margin_micros=margin,
        clv_micros=clv,
        calibration=cal,
        horizon_days=cfg.clv.horizon_days,
        annual_rate=cfg.clv.annual_discount_rate,
    )
