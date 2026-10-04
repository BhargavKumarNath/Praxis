"""Forecasters: three baselines and LightGBM, all point + quantile, all on scaled targets.

Every model learns ``r = y / scale`` from a ``FeatureFrame`` and returns forecasts in units.
Baselines get quantiles from their own empirical residuals per horizon on the training
rows, so probabilistic metrics compare like with like (ADR 0010).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from praxis.forecasting.config import LightGBMParams
from praxis.forecasting.features import CATEGORICAL, FeatureFrame

F64 = NDArray[np.float64]


@dataclass(frozen=True)
class Prediction:
    point: F64  # (rows,) units, >= 0
    quantiles: F64  # (rows, Q) units, non-decreasing along Q
    levels: tuple[float, ...]
    raw_crossing_rows: int  # rows whose raw quantiles crossed before rearrangement

    def quantile(self, level: float) -> F64:
        col: F64 = self.quantiles[:, self.levels.index(level)]
        return col


class Forecaster(Protocol):
    name: str

    def fit(self, frame: FeatureFrame, target: F64, weight: F64) -> None: ...

    def predict(self, frame: FeatureFrame) -> Prediction: ...


def _to_units(frame: FeatureFrame, scaled: F64) -> F64:
    out: F64 = np.maximum(scaled * frame.scale, 0.0)
    return out


def _rearrange(raw_scaled: F64) -> tuple[F64, int]:
    """Sort each row's quantiles (monotone rearrangement); count rows that crossed."""
    crossed = int(np.any(np.diff(raw_scaled, axis=1) < 0, axis=1).sum())
    out: F64 = np.sort(raw_scaled, axis=1)
    return out, crossed


class ResidualQuantiles:
    """Empirical quantiles of scaled residuals, per horizon, from training rows."""

    def __init__(self, levels: Sequence[float]) -> None:
        self.levels = tuple(levels)
        self.table: dict[int, F64] = {}

    def fit(self, frame: FeatureFrame, target: F64, fitted: F64) -> None:
        resid = target - fitted
        self.table = {}
        for h in np.unique(frame.horizon).tolist():
            sel = frame.horizon == h
            self.table[int(h)] = np.quantile(resid[sel], self.levels)

    def apply(self, frame: FeatureFrame, scaled_point: F64) -> F64:
        offsets = np.vstack([self.table[int(h)] for h in frame.horizon.tolist()])
        out: F64 = np.maximum((scaled_point[:, None] + offsets) * frame.scale[:, None], 0.0)
        return out

    def to_json(self) -> dict[str, list[float]]:
        return {str(h): [float(v) for v in q] for h, q in sorted(self.table.items())}

    @classmethod
    def from_json(cls, levels: Sequence[float], raw: dict[str, list[float]]) -> ResidualQuantiles:
        rq = cls(levels)
        rq.table = {int(h): np.asarray(v, dtype=np.float64) for h, v in raw.items()}
        return rq


class _ColumnBaseline:
    """Point forecast = one (already scaled) feature column."""

    column: str
    name: str

    def __init__(self, levels: Sequence[float]) -> None:
        self.residuals = ResidualQuantiles(levels)

    def _scaled(self, frame: FeatureFrame) -> F64:
        return frame.column(self.column)

    def fit(self, frame: FeatureFrame, target: F64, weight: F64) -> None:
        del weight
        self.residuals.fit(frame, target, self._scaled(frame))

    def predict(self, frame: FeatureFrame) -> Prediction:
        scaled = self._scaled(frame)
        return Prediction(
            point=_to_units(frame, scaled),
            quantiles=self.residuals.apply(frame, scaled),
            levels=self.residuals.levels,
            raw_crossing_rows=0,
        )


class SeasonalNaive(_ColumnBaseline):
    """``y[D - 7]``: the documented naive baseline."""

    name = "seasonal_naive"
    column = "lag_dow_1"


class SeasonalMovingAverage(_ColumnBaseline):
    """Mean of the last four same-weekday values. Also the stale-feature fallback."""

    name = "seasonal_moving_average"
    column = "sma_dow_4"


class RidgeBaseline:
    """Weighted ridge regression on standardised numeric features + one-hot categoricals."""

    name = "ridge"

    def __init__(self, levels: Sequence[float], alpha: float) -> None:
        self.alpha = alpha
        self.residuals = ResidualQuantiles(levels)
        self._cats: dict[str, F64] = {}
        self._mu: F64 = np.zeros(0)
        self._sd: F64 = np.ones(0)
        self._beta: F64 = np.zeros(0)

    def _design(self, frame: FeatureFrame, *, fitting: bool) -> F64:
        num_idx = [i for i, n in enumerate(frame.names) if n not in CATEGORICAL]
        num = frame.X[:, num_idx]
        if fitting:
            finite = np.isfinite(num)
            count = np.maximum(finite.sum(axis=0), 1)
            vals = np.where(finite, num, 0.0)
            self._mu = vals.sum(axis=0) / count  # all-missing column -> 0
            var = (np.where(finite, num - self._mu, 0.0) ** 2).sum(axis=0) / count
            self._sd = np.where(var > 1e-24, np.sqrt(var), 1.0)
            self._cats = {c: np.unique(frame.column(c)) for c in CATEGORICAL}
        z = (num - self._mu) / self._sd
        z = np.where(np.isfinite(z), z, 0.0)  # mean imputation
        onehots = [
            (frame.column(c)[:, None] == levels[None, 1:]).astype(np.float64)
            for c, levels in self._cats.items()
        ]
        out: F64 = np.hstack([np.ones((len(frame), 1)), z, *onehots])
        return out

    def fit(self, frame: FeatureFrame, target: F64, weight: F64) -> None:
        a = self._design(frame, fitting=True)
        w = weight / weight.mean()
        penalty = self.alpha * np.eye(a.shape[1])
        penalty[0, 0] = 0.0  # intercept is not shrunk
        gram = a.T @ (a * w[:, None]) + penalty
        self._beta = np.linalg.solve(gram, a.T @ (w * target))
        self.residuals.fit(frame, target, a @ self._beta)

    def predict(self, frame: FeatureFrame) -> Prediction:
        scaled: F64 = self._design(frame, fitting=False) @ self._beta
        return Prediction(
            point=_to_units(frame, scaled),
            quantiles=self.residuals.apply(frame, scaled),
            levels=self.residuals.levels,
            raw_crossing_rows=0,
        )

    def state(self) -> dict[str, Any]:
        """Everything needed to predict again (persisted in the artifact as JSON)."""
        if not self._beta.size:
            raise RuntimeError("model is not trained")
        return {
            "alpha": self.alpha,
            "mu": self._mu.tolist(),
            "sd": self._sd.tolist(),
            "beta": self._beta.tolist(),
            "categories": {c: v.tolist() for c, v in self._cats.items()},
            "residual_quantiles": self.residuals.to_json(),
        }

    @classmethod
    def from_state(cls, levels: Sequence[float], state: dict[str, Any]) -> RidgeBaseline:
        model = cls(levels, float(state["alpha"]))
        model._mu = np.asarray(state["mu"], dtype=np.float64)
        model._sd = np.asarray(state["sd"], dtype=np.float64)
        model._beta = np.asarray(state["beta"], dtype=np.float64)
        model._cats = {c: np.asarray(v, dtype=np.float64) for c, v in state["categories"].items()}
        model.residuals = ResidualQuantiles.from_json(levels, state["residual_quantiles"])
        return model


class LightGBMForecaster:
    """L2 model for the mean plus one quantile model per level; deterministic training.

    Quantile models fitted in-sample are too narrow out of sample. With
    ``calibration_days > 0`` each level gets an additive offset (scaled units) measured
    out of sample: fit on rows whose target is older than the last ``calibration_days``,
    predict forecasts made after that cutoff, and set ``offset_a = Q_a(r - q_a)`` so the
    level is hit empirically. The final models are then refit on all rows.
    """

    name = "lightgbm"
    MIN_CALIBRATION_ROWS = 200

    def __init__(
        self, params: LightGBMParams, levels: Sequence[float], *, with_point: bool = True
    ) -> None:
        self.params = params
        self.levels = tuple(levels)
        self.with_point = with_point
        self.boosters: dict[str, lgb.Booster] = {}
        self.offsets: F64 = np.zeros(len(self.levels))

    def _base_params(self) -> dict[str, Any]:
        p = self.params
        return {
            "learning_rate": p.learning_rate,
            "num_leaves": p.num_leaves,
            "min_data_in_leaf": p.min_data_in_leaf,
            "feature_fraction": p.feature_fraction,
            "lambda_l2": p.lambda_l2,
            "seed": p.seed,
            "num_threads": p.num_threads,
            "deterministic": True,
            "force_row_wise": True,
            "verbose": -1,
        }

    @staticmethod
    def output_names(levels: Sequence[float], *, with_point: bool = True) -> list[str]:
        return [*(["point"] if with_point else []), *(f"q{level:g}" for level in levels)]

    @staticmethod
    def quantile_names(levels: Sequence[float]) -> list[str]:
        return [f"q{level:g}" for level in levels]

    def _train(
        self, frame: FeatureFrame, target: F64, weight: F64, *, quantiles_only: bool = False
    ) -> dict[str, lgb.Booster]:
        data = lgb.Dataset(
            frame.X,
            label=target,
            weight=weight / weight.mean(),
            feature_name=list(frame.names),
            categorical_feature=[n for n in frame.names if n in CATEGORICAL],
            free_raw_data=False,
            params={"verbose": -1, "seed": self.params.seed},
        )
        objectives: dict[str, dict[str, Any]] = (
            {} if quantiles_only else {"point": {"objective": "regression"}}
        )
        for name, q in zip(self.quantile_names(self.levels), self.levels, strict=True):
            objectives[name] = {"objective": "quantile", "alpha": q}
        return {
            name: lgb.train(
                {**self._base_params(), **obj}, data, num_boost_round=self.params.num_boost_round
            )
            for name, obj in objectives.items()
        }

    def _raw_quantiles(self, boosters: dict[str, lgb.Booster], frame: FeatureFrame) -> F64:
        return np.column_stack(
            [
                np.asarray(boosters[n].predict(frame.X), dtype=np.float64)
                for n in self.quantile_names(self.levels)
            ]
        )

    def _calibrate(self, frame: FeatureFrame, target: F64, weight: F64) -> F64:
        days = self.params.calibration_days
        cutoff = int(frame.target_day.max()) - days
        proper = frame.target_day <= cutoff
        held_out = frame.origin > cutoff  # every target here is after every proper target
        if days == 0 or min(proper.sum(), held_out.sum()) < self.MIN_CALIBRATION_ROWS:
            return np.zeros(len(self.levels))
        boosters = self._train(
            frame.take(proper), target[proper], weight[proper], quantiles_only=True
        )
        q_hat = self._raw_quantiles(boosters, frame.take(held_out))
        resid = target[held_out][:, None] - q_hat
        return np.array([np.quantile(resid[:, j], a) for j, a in enumerate(self.levels)])

    def fit(self, frame: FeatureFrame, target: F64, weight: F64) -> None:
        self.offsets = self._calibrate(frame, target, weight)
        self.boosters = self._train(frame, target, weight, quantiles_only=not self.with_point)

    def predict_quantiles(self, frame: FeatureFrame) -> tuple[F64, int]:
        """Calibrated, rearranged quantiles in units, and the raw crossing-row count."""
        if not self.boosters:
            raise RuntimeError("model is not trained")
        raw = self._raw_quantiles(self.boosters, frame) + self.offsets[None, :]
        ordered, crossed = _rearrange(raw)
        return np.maximum(ordered * frame.scale[:, None], 0.0), crossed

    def predict(self, frame: FeatureFrame) -> Prediction:
        quantiles, crossed = self.predict_quantiles(frame)
        if "point" not in self.boosters:
            raise RuntimeError("quantile-only model has no point forecast")
        point = np.asarray(self.boosters["point"].predict(frame.X), dtype=np.float64)
        return Prediction(
            point=_to_units(frame, point),
            quantiles=quantiles,
            levels=self.levels,
            raw_crossing_rows=crossed,
        )

    def model_strings(self) -> dict[str, str]:
        return {name: b.model_to_string() for name, b in self.boosters.items()}

    @classmethod
    def from_model_strings(
        cls,
        params: LightGBMParams,
        levels: Sequence[float],
        models: dict[str, str],
        offsets: Sequence[float],
        *,
        with_point: bool = True,
    ) -> LightGBMForecaster:
        model = cls(params, levels, with_point=with_point)
        if len(offsets) != len(model.levels):
            raise ValueError("one calibration offset per quantile level is required")
        model.offsets = np.asarray(offsets, dtype=np.float64)
        expected = cls.output_names(levels, with_point=with_point)
        if sorted(models) != sorted(expected):
            raise ValueError(f"model files {sorted(models)} do not match outputs {expected}")
        model.boosters = {n: lgb.Booster(model_str=models[n]) for n in expected}
        return model


class HybridForecaster:
    """Champion (ADR 0011): ridge expected demand + calibrated LightGBM quantiles.

    On the development world ridge was the best mean forecast and calibrated LightGBM the
    best probabilistic one; each component serves the output it was measured best at.
    """

    name = "hybrid"

    def __init__(self, ridge: RidgeBaseline, quantiles: LightGBMForecaster) -> None:
        if quantiles.with_point:
            raise ValueError("the hybrid's LightGBM component must be quantile-only")
        self.ridge = ridge
        self.quantiles = quantiles

    @classmethod
    def build(
        cls, params: LightGBMParams, levels: Sequence[float], alpha: float
    ) -> HybridForecaster:
        return cls(
            RidgeBaseline(levels, alpha), LightGBMForecaster(params, levels, with_point=False)
        )

    @staticmethod
    def combine(point_from: Prediction, quantiles_from: Prediction) -> Prediction:
        return Prediction(
            point=point_from.point,
            quantiles=quantiles_from.quantiles,
            levels=quantiles_from.levels,
            raw_crossing_rows=quantiles_from.raw_crossing_rows,
        )

    def fit(self, frame: FeatureFrame, target: F64, weight: F64) -> None:
        self.ridge.fit(frame, target, weight)
        self.quantiles.fit(frame, target, weight)

    def predict(self, frame: FeatureFrame) -> Prediction:
        quantiles, crossed = self.quantiles.predict_quantiles(frame)
        return Prediction(
            point=self.ridge.predict(frame).point,
            quantiles=quantiles,
            levels=self.quantiles.levels,
            raw_crossing_rows=crossed,
        )
