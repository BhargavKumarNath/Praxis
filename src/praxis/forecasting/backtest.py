"""Expanding-window rolling-origin backtest and the pre-registered acceptance checks.

For each weekly origin T, every model is trained on rows whose *target* day is <= T and
evaluated on the forecasts made at T for T+1..T+7. There is no random split anywhere; the
temporal boundary is asserted for every fold.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.features import (
    FEATURE_VERSION,
    WINDOW,
    Catalogue,
    FeatureFrame,
    actual,
    build_frame,
    scaled_target,
)
from praxis.forecasting.metrics import bootstrap_vwape_difference, sliced, summarize
from praxis.forecasting.models import (
    Forecaster,
    HybridForecaster,
    LightGBMForecaster,
    Prediction,
    RidgeBaseline,
    SeasonalMovingAverage,
    SeasonalNaive,
)
from praxis.forecasting.panel import DemandPanel, PricePlan

F64 = NDArray[np.float64]
NAIVE = SeasonalNaive.name
SpikeDays = Collection[tuple[str, str, date]]  # (region, product, date) inside a spike


class TemporalLeakError(AssertionError):
    """A training row's target is not strictly before every evaluated target."""


@dataclass(frozen=True)
class Dataset:
    """Every feature row the panel supports, with targets and weights."""

    frame: FeatureFrame
    target: F64  # scaled; NaN where the target day is beyond the panel
    y: F64  # units
    value_weight: F64  # GBP per unit of the row's product

    @property
    def train_weight(self) -> F64:
        out: F64 = self.value_weight * self.frame.scale
        return out


def make_dataset(panel: DemandPanel, plan: PricePlan, cfg: ForecastConfig) -> Dataset:
    catalogue = Catalogue.from_series(panel.series)
    origins = range(WINDOW - 1, panel.n_days - 1)
    frame = build_frame(
        panel, origins, cfg.target.horizons, plan, catalogue, external=cfg.features.external
    )
    weights = cfg.evaluation.value_weights
    missing = {s.product for s in panel.series} - set(weights)
    if missing:
        raise ValueError(f"no value weight for products {sorted(missing)}")
    per_series = np.array([weights[s.product] for s in panel.series])
    return Dataset(
        frame=frame,
        target=scaled_target(panel, frame),
        y=actual(panel, frame),
        value_weight=per_series[frame.series_idx],
    )


def make_models(cfg: ForecastConfig) -> list[Forecaster]:
    levels = cfg.target.quantiles
    return [
        SeasonalNaive(levels),
        SeasonalMovingAverage(levels),
        RidgeBaseline(levels, cfg.ridge.alpha),
        LightGBMForecaster(cfg.lightgbm, levels),
    ]


def rolling_origins(n_days: int, cfg: ForecastConfig) -> list[int]:
    first = cfg.backtest.initial_train_days - 1
    last = n_days - 1 - max(cfg.target.horizons)
    return list(range(first, last + 1, cfg.backtest.step_days))


def split(ds: Dataset, origin: int) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """Training rows: target observed by ``origin``. Test rows: forecasts made at ``origin``."""
    known = ~np.isnan(ds.target)
    train = known & (ds.frame.target_day <= origin)
    test = known & (ds.frame.origin == origin)
    if not train.any() or not test.any():
        raise ValueError(f"origin {origin}: empty train or test set")
    if ds.frame.target_day[train].max() >= ds.frame.target_day[test].min():
        raise TemporalLeakError(f"origin {origin}: training target overlaps evaluation")
    return train, test


def _concat(preds: Sequence[Prediction]) -> Prediction:
    return Prediction(
        point=np.concatenate([p.point for p in preds]),
        quantiles=np.vstack([p.quantiles for p in preds]),
        levels=preds[0].levels,
        raw_crossing_rows=sum(p.raw_crossing_rows for p in preds),
    )


def run_backtest(
    panel: DemandPanel,
    plan: PricePlan,
    cfg: ForecastConfig,
    *,
    spike_days: SpikeDays = (),
    model_factory: Callable[[ForecastConfig], list[Forecaster]] = make_models,
) -> dict[str, Any]:
    started = time.perf_counter()
    ds = make_dataset(panel, plan, cfg)
    origins = rolling_origins(panel.n_days, cfg)
    if not origins:
        raise ValueError("panel too short for one backtest origin")
    preds: dict[str, list[Prediction]] = {}
    test_idx: list[NDArray[np.int64]] = []
    fit_s: dict[str, float] = {}
    for origin in origins:
        train, test = split(ds, origin)
        test_idx.append(np.flatnonzero(test))
        for model in model_factory(cfg):
            t0 = time.perf_counter()
            model.fit(ds.frame.take(train), ds.target[train], ds.train_weight[train])
            preds.setdefault(model.name, []).append(model.predict(ds.frame.take(test)))
            fit_s[model.name] = fit_s.get(model.name, 0.0) + time.perf_counter() - t0

    rows = np.concatenate(test_idx)
    frame = ds.frame.take(rows)
    y, w = ds.y[rows], ds.value_weight[rows]
    by_model = {name: _concat(p) for name, p in preds.items()}
    if RidgeBaseline.name in by_model and LightGBMForecaster.name in by_model:
        # The hybrid's components are fitted on exactly the same rows and are
        # deterministic, so composing their fold predictions equals fitting the hybrid
        # (tests/forecasting/test_models.py checks this). It saves a third LightGBM fit.
        by_model[HybridForecaster.name] = HybridForecaster.combine(
            by_model[RidgeBaseline.name], by_model[LightGBMForecaster.name]
        )
        fit_s[HybridForecaster.name] = fit_s[RidgeBaseline.name] + fit_s[LightGBMForecaster.name]
    candidate = cfg.acceptance.candidate
    series = panel.series
    spikes = set(spike_days)
    groups: dict[str, NDArray[Any]] = {
        "region": np.array([series[i].region_id for i in frame.series_idx.tolist()]),
        "product": np.array([series[i].product for i in frame.series_idx.tolist()]),
        "segment": np.array([series[i].segment for i in frame.series_idx.tolist()]),
        "horizon": frame.horizon.copy(),
        "period": np.array(
            [
                "spike"
                if (series[i].region_id, series[i].product, panel.date_of(int(d))) in spikes
                else "normal"
                for i, d in zip(frame.series_idx.tolist(), frame.target_day.tolist(), strict=True)
            ]
        ),
    }
    models: dict[str, Any] = {}
    for name, pred in by_model.items():
        models[name] = {
            "overall": summarize(y, pred, w),
            "slices": sliced(y, pred, w, groups),
            "raw_quantile_crossing_rows": pred.raw_crossing_rows,
            "raw_quantile_crossing_rate": pred.raw_crossing_rows / max(len(y), 1),
            "fit_predict_seconds": round(fit_s[name], 3),
        }
    comparisons = {}
    if candidate in by_model:
        for name, pred in by_model.items():
            if name != candidate:
                comparisons[f"{candidate}_minus_{name}"] = bootstrap_vwape_difference(
                    y,
                    by_model[candidate].point,
                    pred.point,
                    w,
                    frame.origin,
                    samples=cfg.backtest.bootstrap_samples,
                    seed=cfg.backtest.bootstrap_seed,
                )
    report: dict[str, Any] = {
        "is_synthetic": True,
        "candidate": candidate,
        "feature_version": FEATURE_VERSION,
        "config_hash": cfg.config_hash,
        "external_features": cfg.features.external,
        "data_version": panel.data_version(),
        "price_plan_version": plan.version(),
        "panel": {
            "start_date": panel.start_date.isoformat(),
            "end_date": panel.end_date.isoformat(),
            "days": panel.n_days,
            "series": len(series),
        },
        "origins": [panel.date_of(o).isoformat() for o in origins],
        "evaluated_rows": len(y),
        "spike_rows": int((groups["period"] == "spike").sum()),
        "models": models,
        "comparisons": comparisons,
        "elapsed_s": round(time.perf_counter() - started, 2),
    }
    if candidate in models and NAIVE in models:
        report["acceptance"] = evaluate_acceptance(report, cfg)
    return report


def _check(name: str, value: float, threshold: str, passed: bool) -> dict[str, Any]:
    return {"check": name, "value": round(value, 6), "threshold": threshold, "passed": passed}


def evaluate_acceptance(report: dict[str, Any], cfg: ForecastConfig) -> dict[str, Any]:
    """Apply the pre-registered ``[acceptance]`` thresholds to a backtest report."""
    acc = cfg.acceptance
    m = report["models"]
    name_c = acc.candidate
    cand, naive = m[name_c]["overall"], m[NAIVE]["overall"]
    checks = []
    ratio = cand["vwape"] / naive["vwape"]
    checks.append(
        _check(
            "vwape_ratio_vs_seasonal_naive",
            ratio,
            f"<= {acc.max_vwape_ratio_vs_seasonal_naive}",
            ratio <= acc.max_vwape_ratio_vs_seasonal_naive,
        )
    )
    for name in acc.point_must_beat:
        other = m[name]["overall"]["vwape"]
        checks.append(
            _check(f"vwape_below_{name}", cand["vwape"] - other, "< 0", cand["vwape"] < other)
        )
    if acc.require_bootstrap_ci_below_zero:
        ci = report["comparisons"][f"{name_c}_minus_{NAIVE}"]["ci95_high"]
        checks.append(_check("bootstrap_ci95_high_vs_naive", ci, "< 0", ci < 0))
    pin = cand["pinball"] / naive["pinball"]
    checks.append(
        _check(
            "pinball_ratio_vs_seasonal_naive",
            pin,
            f"< {acc.max_pinball_ratio_vs_seasonal_naive}",
            pin < acc.max_pinball_ratio_vs_seasonal_naive,
        )
    )
    for name in acc.pinball_must_beat:
        other = m[name]["overall"]["pinball"]
        checks.append(
            _check(f"pinball_below_{name}", cand["pinball"] - other, "< 0", cand["pinball"] < other)
        )
    for key, (lo, hi) in (("coverage_80", acc.coverage_80), ("coverage_50", acc.coverage_50)):
        v = cand[key]
        checks.append(_check(key, v, f"in [{lo}, {hi}]", lo <= v <= hi))
    worst_ratio, worst_slice = 0.0, ""
    for group in ("region", "product", "segment"):
        for label, res in m[name_c]["slices"][group].items():
            r = res["vwape"] / m[NAIVE]["slices"][group][label]["vwape"]
            if r > worst_ratio:
                worst_ratio, worst_slice = r, f"{group}={label}"
    checks.append(
        _check(
            f"worst_slice_vwape_ratio ({worst_slice})",
            worst_ratio,
            f"<= {acc.max_slice_vwape_ratio_vs_seasonal_naive}",
            worst_ratio <= acc.max_slice_vwape_ratio_vs_seasonal_naive,
        )
    )
    return {"checks": checks, "passed": all(c["passed"] for c in checks)}
