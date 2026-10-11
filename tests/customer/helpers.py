"""Hand-built customer snapshots and values with exactly known numbers (SYNTHETIC)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from praxis.customer.value import CustomerValues, RecentCalibration
from praxis.customer.warehouse import Snapshot

AS_OF = date(2026, 7, 20)
PRODUCTS = ("api_requests", "gpu_minutes")


def snapshot(**overrides: Any) -> Snapshot:
    """Four customers: two growth, one enterprise, one starter that never used api_requests."""
    fields: dict[str, Any] = {
        "as_of": AS_OF,
        "products": PRODUCTS,
        "ids": ("c1", "c2", "c3", "c4"),
        "region": ("eu_west",) * 4,
        "industry": ("saas",) * 4,
        "tier": ("growth", "growth", "enterprise", "starter"),
        "fee_minor": np.array([9900.0, 9900.0, 99900.0, 900.0]),
        "tenure_days": np.array([100.0, 400.0, 900.0, 10.0]),
        # 28-day usage value per product (micros): shares 0.75 / 0.5 / 1.0 / 0.0 of api
        "value_micros": np.array(
            [[30e6, 10e6], [20e6, 20e6], [280e6, 0.0], [0.0, 28e6]], dtype=np.float64
        ),
        "cost_micros": np.array(
            [[9e6, 6e6], [6e6, 12e6], [84e6, 0.0], [0.0, 14e6]], dtype=np.float64
        ),
        "recent_value_micros": np.array([20e6, 20e6, 140e6, 14e6]),
        "prior_value_micros": np.array([20e6, 20e6, 140e6, 14e6]),
        "failed_attempts": np.array([0.0, 2.0, 0.0, 0.0]),
        "region_error_rate": np.full(4, 0.001),
        "region_latency_p95_ms": np.full(4, 60.0),
        "payment_loss_rate": 0.0,
        "spend_window_days": 28,
        "price_change_dates": {
            "c1": (AS_OF - timedelta(days=80), AS_OF - timedelta(days=40)),
            "c2": (AS_OF - timedelta(days=200),),
            "c3": (
                AS_OF - timedelta(days=60),
                AS_OF - timedelta(days=30),
                AS_OF - timedelta(days=10),
            ),
        },
    }
    fields.update(overrides)
    return Snapshot(**fields)


def values(
    snap: Snapshot | None = None,
    *,
    hazard: tuple[float, ...] = (0.002, 0.002, 0.0005, 0.003),
    clv: tuple[float, ...] = (5e9, 7e9, 40e9, 1e9),
    calibration: RecentCalibration | None = None,
) -> CustomerValues:
    snap = snap or snapshot()
    return CustomerValues(
        snapshot=snap,
        model_version="churn-table-test",
        champion="tier_rate_table",
        window_days=28,
        voluntary_hazard=np.array(hazard),
        involuntary_hazard=np.full(snap.n, 0.0002),
        margin_micros=snap.daily_margin_micros(),
        clv_micros=np.array(clv),
        calibration=calibration,
        horizon_days=365,
        annual_rate=0.1,
    )
