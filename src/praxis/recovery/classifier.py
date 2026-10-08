"""Calibrated binary recovery model and the empirical-rate baseline.

Target: does an episode's FIRST retry succeed, given the features and the elapsed time e of
that retry? Training uses one row per episode (its first retry). Under absorbing
collectibility P(first retry at e succeeds | x) = P(C <= e | x), so the same function serves
the planner. Later retries are conditional on earlier failures and are deliberately left out
of this model (the survival model uses them).

* ``CalibratedClassifier``: LightGBM, monotone non-decreasing in e, then isotonic calibration
  fitted on the LATEST ``calibration_fraction`` of training rows (time-ordered, never random).
  Isotonic is monotone in the raw score, so the calibrated output stays monotone in e.
* ``RateTable``: empirical success rate per (reason, whole days of e), shrunk towards the
  reason's rate. The simple baseline every model must beat.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from praxis.recovery.config import ClassifierConfig
from praxis.recovery.dataset import Episode
from praxis.recovery.features import RecoveryFeatures, column_names, encode

F64 = NDArray[np.float64]
NAME = "classifier_lgbm_isotonic"
TABLE_NAME = "rate_table"
ELAPSED = "elapsed_days"


class TrainingError(RuntimeError):
    """Not enough labelled rows to train or calibrate."""


def first_retry_rows(episodes: Sequence[Episode]) -> tuple[list[Episode], F64, F64]:
    """Episodes with an observed first retry, its elapsed time and outcome (time-ordered)."""
    kept = [e for e in episodes if e.first_retry is not None]
    elapsed = np.array([e.retries[0].elapsed_days for e in kept], dtype=np.float64)
    label = np.array([float(e.retries[0].succeeded) for e in kept], dtype=np.float64)
    return kept, elapsed, label


# --------------------------------------------------------------------- isotonic (PAV)
def isotonic_fit(score: F64, label: F64) -> tuple[F64, F64]:
    """Pool-adjacent-violators: non-decreasing step function (knots x, values y)."""
    # Tied scores (common for tree ensembles) are one point with the mean label.
    xs, inverse, counts = np.unique(score, return_inverse=True, return_counts=True)
    ys = np.bincount(inverse, weights=label) / counts
    values: list[float] = []
    weights: list[float] = []
    lefts: list[float] = []
    for x, y, c in zip(xs.tolist(), ys.tolist(), counts.tolist(), strict=True):
        values.append(y)
        weights.append(float(c))
        lefts.append(x)
        while len(values) > 1 and values[-2] > values[-1]:
            w = weights[-2] + weights[-1]
            v = (values[-2] * weights[-2] + values[-1] * weights[-1]) / w
            values[-2:] = [v]
            weights[-2:] = [w]
            lefts.pop()
    return np.asarray(lefts, dtype=np.float64), np.asarray(values, dtype=np.float64)


def isotonic_apply(knots: F64, values: F64, score: F64) -> F64:
    idx = np.searchsorted(knots, score, side="right") - 1
    out: F64 = values[np.clip(idx, 0, len(values) - 1)]
    return out


# ------------------------------------------------------------------- classifier
@dataclass
class CalibratedClassifier:
    params: ClassifierConfig
    booster: lgb.Booster | None = None
    knots: F64 = field(default_factory=lambda: np.zeros(0))
    values: F64 = field(default_factory=lambda: np.zeros(0))

    name = NAME

    @staticmethod
    def feature_names() -> list[str]:
        return [*(n.replace("=", "_") for n in column_names()), ELAPSED]

    def _lgb_params(self) -> dict[str, Any]:
        p = self.params
        monotone = [0] * (len(self.feature_names()) - 1) + [1]
        return {
            "objective": "binary",
            "learning_rate": p.learning_rate,
            "num_leaves": p.num_leaves,
            "min_data_in_leaf": p.min_data_in_leaf,
            "lambda_l2": p.lambda_l2,
            "monotone_constraints": monotone,
            "monotone_constraints_method": "advanced",
            "seed": p.seed,
            "num_threads": 1,
            "deterministic": True,
            "force_row_wise": True,
            "verbose": -1,
        }

    @staticmethod
    def _matrix(rows: Sequence[RecoveryFeatures], elapsed: F64) -> F64:
        return np.hstack([encode(rows), np.asarray(elapsed, dtype=np.float64)[:, None]])

    def fit(self, episodes: Sequence[Episode]) -> CalibratedClassifier:
        kept, elapsed, label = first_retry_rows(episodes)
        n_cal = math.ceil(len(kept) * self.params.calibration_fraction)
        n_fit = len(kept) - n_cal
        if n_fit < 100 or n_cal < 50:
            raise TrainingError(f"too few first-retry rows ({len(kept)})")
        x = self._matrix([e.features for e in kept], elapsed)
        data = lgb.Dataset(
            x[:n_fit],
            label=label[:n_fit],
            feature_name=self.feature_names(),
            free_raw_data=False,
            params={"verbose": -1, "seed": self.params.seed},
        )
        self.booster = lgb.train(
            self._lgb_params(), data, num_boost_round=self.params.num_boost_round
        )
        raw = self.booster.predict(x[n_fit:], raw_score=True)
        self.knots, self.values = isotonic_fit(np.asarray(raw, dtype=np.float64), label[n_fit:])
        return self

    def predict(self, rows: Sequence[RecoveryFeatures], elapsed: F64) -> F64:
        if not rows:
            return np.zeros(0)
        if self.booster is None:
            raise TrainingError("classifier is not trained")
        raw = np.asarray(
            self.booster.predict(self._matrix(rows, elapsed), raw_score=True), dtype=np.float64
        )
        return isotonic_apply(self.knots, self.values, raw)

    def prob_collectible(self, rows: Sequence[RecoveryFeatures], t: F64) -> F64:
        """(n, m): calibrated P(first retry at t succeeds) = P(C <= t) under absorbing cures."""
        grid = np.asarray(t, dtype=np.float64)
        n, m = len(rows), len(grid)
        if n == 0:
            return np.zeros((0, m))
        repeated = [r for r in rows for _ in range(m)]
        out: F64 = self.predict(repeated, np.tile(grid, n)).reshape(n, m)
        return np.where(grid[None, :] > 0, out, 0.0)

    def state(self) -> dict[str, Any]:
        if self.booster is None:
            raise TrainingError("classifier is not trained")
        return {
            "name": NAME,
            "feature_names": self.feature_names(),
            "knots": self.knots.tolist(),
            "values": self.values.tolist(),
        }

    def model_string(self) -> str:
        if self.booster is None:
            raise TrainingError("classifier is not trained")
        return str(self.booster.model_to_string())

    @classmethod
    def from_state(
        cls, params: ClassifierConfig, state: dict[str, Any], model_str: str
    ) -> CalibratedClassifier:
        if state.get("feature_names") != cls.feature_names():
            raise ValueError("classifier state does not match this feature design")
        return cls(
            params,
            lgb.Booster(model_str=model_str),
            np.asarray(state["knots"], dtype=np.float64),
            np.asarray(state["values"], dtype=np.float64),
        )


# --------------------------------------------------------------------- baseline
@dataclass
class RateTable:
    """Empirical first-retry success rate by (reason, whole days of e), shrunk to the reason."""

    prior_weight: float = 5.0
    cells: dict[tuple[str, int], tuple[float, float]] = field(default_factory=dict)
    reason_rate: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0

    name = TABLE_NAME

    def fit(self, episodes: Sequence[Episode]) -> RateTable:
        kept, elapsed, label = first_retry_rows(episodes)
        if not kept:
            raise TrainingError("no first-retry rows")
        self.overall = float(label.mean())
        sums: dict[str, list[float]] = {}
        for e, y in zip(kept, label.tolist(), strict=True):
            sums.setdefault(e.features.reason, []).append(y)
        self.reason_rate = {r: float(np.mean(v)) for r, v in sums.items()}
        cells: dict[tuple[str, int], list[float]] = {}
        for e, t, y in zip(kept, elapsed.tolist(), label.tolist(), strict=True):
            cells.setdefault((e.features.reason, round(t)), []).append(y)
        self.cells = {k: (float(sum(v)), float(len(v))) for k, v in cells.items()}
        return self

    def predict(self, rows: Sequence[RecoveryFeatures], elapsed: F64) -> F64:
        out = np.empty(len(rows), dtype=np.float64)
        for k, (f, t) in enumerate(zip(rows, np.asarray(elapsed).tolist(), strict=True)):
            prior = self.reason_rate.get(f.reason, self.overall)
            hits, n = self.cells.get((f.reason, round(t)), (0.0, 0.0))
            out[k] = (hits + self.prior_weight * prior) / (n + self.prior_weight)
        return out
