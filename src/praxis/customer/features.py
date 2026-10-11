"""``customer_features.v1``: churn-hazard features from a point-in-time ``Snapshot`` (pure).

Every input comes from the snapshot, which only holds data from before its date, so the
features cannot see the label window. Price-change history is deliberately NOT a feature:
list-price changes hit every customer at once, so in observational data they are collinear
with time; the causal price response comes from the randomised tests (ADR 0016).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from praxis.customer.warehouse import Snapshot

F64 = NDArray[np.float64]

FEATURE_VERSION = "customer_features.v1"
FEATURES = (
    "tier_growth",
    "tier_enterprise",
    "log_tenure_days",
    "log_daily_spend_gbp",
    "usage_trend",
    "log_failed_attempts",
    "log_region_error_rate",
    "log_region_latency_p95",
)
_MICROS_PER_GBP = 1e6
_MIN_ERROR_RATE = 1e-6


def feature_matrix(s: Snapshot, trend_half_days: int) -> F64:
    """(n, len(FEATURES)) raw (unstandardised) features. NaN only where service data is missing."""
    tier = np.array(s.tier)
    daily_spend = s.value_micros.sum(axis=1) / s.spend_window_days / _MICROS_PER_GBP
    half = float(trend_half_days)
    recent = s.recent_value_micros / half / _MICROS_PER_GBP
    prior = s.prior_value_micros / half / _MICROS_PER_GBP
    cols = [
        (tier == "growth").astype(np.float64),
        (tier == "enterprise").astype(np.float64),
        np.log1p(np.maximum(s.tenure_days, 0.0)),
        np.log1p(np.maximum(daily_spend, 0.0)),
        np.log((recent + 1.0) / (prior + 1.0)),
        np.log1p(s.failed_attempts),
        np.log(np.maximum(s.region_error_rate, _MIN_ERROR_RATE)),
        np.log(s.region_latency_p95_ms),
    ]
    out: F64 = np.column_stack(cols) if s.n else np.zeros((0, len(FEATURES)))
    return out
