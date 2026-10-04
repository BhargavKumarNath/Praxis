"""Leakage-safe features for a forecast made at the end of day ``origin`` for day ``origin + h``.

The builder first cuts the panel down to ``panel.history(origin)``; every outcome-derived
feature is computed from that slice, so it cannot read a value dated after the origin. The
only inputs about the target day are its calendar and the planned list price (ADR 0010).
``tests/forecasting/test_leakage.py`` proves this by rewriting the future and comparing.

All level features are divided by the series' own 28-day mean (``scale``) so one pooled
model serves series whose volumes differ by orders of magnitude.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from praxis.forecasting.panel import (
    EXTERNAL_CONTEXT,
    SERVICE_CONTEXT,
    DemandPanel,
    PricePlan,
    SeriesKey,
)

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]

FEATURE_VERSION = "demand_features.v1"
WINDOW = 28  # days of history every feature row needs
SCALE_FLOOR = 1.0  # units; keeps near-empty series from exploding the scaled target
EWM_HALFLIFE = 7.0
SAME_DOW_LAGS = (1, 2, 3, 4)  # weeks back: y[D - 7k]

CATEGORICAL = ("region", "product", "segment", "dow")
_LEVEL = (
    *(f"lag_dow_{k}" for k in SAME_DOW_LAGS),
    "sma_dow_4",
    "last_1",
    "mean_7",
    "ewm_7",
    "std_28",
    "log_scale",
    "served_ratio_7",
    "active_ratio",
)
_PRICE = ("log_price_change_to_target", "log_price_change_last_7", "log_price_vs_first")
_SERVICE = tuple(f"ctx_{c}" for c in SERVICE_CONTEXT)
_EXTERNAL = tuple(f"ext_{c}" for c in EXTERNAL_CONTEXT)


def feature_names(external: bool) -> tuple[str, ...]:
    return (*CATEGORICAL, "horizon", *_LEVEL, *_PRICE, *_SERVICE, *(_EXTERNAL if external else ()))


@dataclass(frozen=True)
class Catalogue:
    """Stable integer codes for categorical features (persisted with the model)."""

    regions: tuple[str, ...]
    products: tuple[str, ...]
    segments: tuple[str, ...]

    @classmethod
    def from_series(cls, series: Iterable[SeriesKey]) -> Catalogue:
        keys = list(series)
        return cls(
            regions=tuple(sorted({s.region_id for s in keys})),
            products=tuple(sorted({s.product for s in keys})),
            segments=tuple(sorted({s.segment for s in keys})),
        )

    def codes(self, s: SeriesKey) -> tuple[int, int, int]:
        try:
            return (
                self.regions.index(s.region_id),
                self.products.index(s.product),
                self.segments.index(s.segment),
            )
        except ValueError as exc:
            raise KeyError(f"series {s.label} is not in the model catalogue") from exc


@dataclass(frozen=True)
class FeatureFrame:
    """One row per (series, origin, horizon)."""

    X: F64
    names: tuple[str, ...]
    scale: F64
    series_idx: I64
    origin: I64
    horizon: I64

    @property
    def target_day(self) -> I64:
        out: I64 = self.origin + self.horizon
        return out

    def __len__(self) -> int:
        return int(self.X.shape[0])

    def take(self, mask: NDArray[np.bool_] | I64) -> FeatureFrame:
        return FeatureFrame(
            self.X[mask],
            self.names,
            self.scale[mask],
            self.series_idx[mask],
            self.origin[mask],
            self.horizon[mask],
        )

    def column(self, name: str) -> F64:
        col: F64 = self.X[:, self.names.index(name)]
        return col

    @staticmethod
    def concat(frames: Sequence[FeatureFrame]) -> FeatureFrame:
        if not frames:
            raise ValueError("nothing to concatenate")
        names = frames[0].names
        if any(f.names != names for f in frames):
            raise ValueError("feature names differ")
        return FeatureFrame(
            np.vstack([f.X for f in frames]),
            names,
            np.concatenate([f.scale for f in frames]),
            np.concatenate([f.series_idx for f in frames]),
            np.concatenate([f.origin for f in frames]),
            np.concatenate([f.horizon for f in frames]),
        )


def _safe_log_ratio(a: F64, b: F64) -> F64:
    out: F64 = np.where((a > 0) & (b > 0), np.log(np.maximum(a, 1e-12) / np.maximum(b, 1e-12)), 0.0)
    return out


def _plan_prices(plan: PricePlan, panel: DemandPanel, products: Sequence[str], day: int) -> F64:
    d = panel.date_of(day)
    return np.array([float(plan.price_on(p, d) or np.nan) for p in products], dtype=np.float64)


def build_features(
    panel: DemandPanel,
    origin: int,
    horizons: Sequence[int],
    plan: PricePlan,
    catalogue: Catalogue,
    *,
    external: bool = False,
) -> FeatureFrame:
    """Features for every series at ``origin`` and each horizon. Uses outcomes <= origin only."""
    if origin < WINDOW - 1:
        raise ValueError(f"origin {origin} has fewer than {WINDOW} days of history")
    if not horizons or min(horizons) < 1 or max(horizons) > 7:
        raise ValueError("horizons must lie in 1..7")
    hist = panel.history(origin)  # the leakage boundary: nothing after `origin` beyond here
    t = origin
    y, served, active = hist.demand, hist.served, hist.active
    n_s = len(hist.series)
    win = y[:, t - WINDOW + 1 : t + 1]
    scale = np.maximum(win.mean(axis=1), SCALE_FLOOR)
    lags = np.arange(WINDOW)[::-1]  # age in days of each window column
    w = 0.5 ** (lags / EWM_HALFLIFE)
    ewm = (win * w).sum(axis=1) / w.sum()
    dem_7 = y[:, t - 6 : t + 1].sum(axis=1)
    served_7 = served[:, t - 6 : t + 1].sum(axis=1)
    served_ratio = np.where(dem_7 > 0, served_7 / np.maximum(dem_7, 1e-12), 1.0)
    act_mean = active[:, t - WINDOW + 1 : t + 1].mean(axis=1)
    active_ratio = np.where(act_mean > 0, active[:, t] / np.maximum(act_mean, 1e-12), 0.0)

    ridx = hist.series_region_index()
    ctx_cols = [hist.context.get(c) for c in SERVICE_CONTEXT]
    ext_cols = [hist.context.get(c) for c in EXTERNAL_CONTEXT] if external else []
    nan = np.full(n_s, np.nan)
    ctx = [nan if c is None else c[ridx, t] for c in [*ctx_cols, *ext_cols]]

    codes = np.array([catalogue.codes(s) for s in hist.series], dtype=np.float64).reshape(n_s, 3)
    products = [s.product for s in hist.series]
    price_t = _plan_prices(plan, hist, products, t)
    price_t7 = _plan_prices(plan, hist, products, t - 7)
    first = np.array(
        [float(plan.changes[p][0][1]) if plan.changes.get(p) else np.nan for p in products]
    )
    level_common = [
        y[:, t] / scale,
        y[:, t - 6 : t + 1].mean(axis=1) / scale,
        ewm / scale,
        win.std(axis=1) / scale,
        np.log1p(scale),
        served_ratio,
        active_ratio,
    ]

    rows: list[F64] = []
    horizon_col: list[I64] = []
    for h in horizons:
        target = t + h
        lag_vals = [y[:, target - 7 * k] / scale for k in SAME_DOW_LAGS]  # target-7k <= t
        price_d = _plan_prices(plan, hist, products, target)
        dow = np.full(n_s, float(hist.date_of(target).weekday()))
        block = np.column_stack(
            [
                codes,
                dow,
                np.full(n_s, float(h)),
                *lag_vals,
                np.mean(lag_vals, axis=0),
                *level_common,
                _safe_log_ratio(price_d, price_t),
                _safe_log_ratio(price_t, price_t7),
                _safe_log_ratio(price_d, first),
                *ctx,
            ]
        )
        rows.append(block)
        horizon_col.append(np.full(n_s, h, dtype=np.int64))

    n_h = len(horizons)
    return FeatureFrame(
        X=np.vstack(rows),
        names=feature_names(external),
        scale=np.tile(scale, n_h),
        series_idx=np.tile(np.arange(n_s, dtype=np.int64), n_h),
        origin=np.full(n_s * n_h, t, dtype=np.int64),
        horizon=np.concatenate(horizon_col),
    )


def build_frame(
    panel: DemandPanel,
    origins: Iterable[int],
    horizons: Sequence[int],
    plan: PricePlan,
    catalogue: Catalogue,
    *,
    external: bool = False,
) -> FeatureFrame:
    return FeatureFrame.concat(
        [build_features(panel, o, horizons, plan, catalogue, external=external) for o in origins]
    )


def scaled_target(panel: DemandPanel, frame: FeatureFrame) -> F64:
    """``y[target_day] / scale``; NaN where the target day is beyond the panel."""
    out = np.full(len(frame), np.nan)
    ok = frame.target_day < panel.n_days
    out[ok] = panel.demand[frame.series_idx[ok], frame.target_day[ok]] / frame.scale[ok]
    return out


def actual(panel: DemandPanel, frame: FeatureFrame) -> F64:
    out = np.full(len(frame), np.nan)
    ok = frame.target_day < panel.n_days
    out[ok] = panel.demand[frame.series_idx[ok], frame.target_day[ok]]
    return out
