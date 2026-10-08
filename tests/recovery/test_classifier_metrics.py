"""Calibrated classifier, isotonic calibration, rate-table baseline and probability metrics."""

from __future__ import annotations

import math

import numpy as np
import pytest

from praxis.recovery import metrics
from praxis.recovery.classifier import (
    CalibratedClassifier,
    RateTable,
    TrainingError,
    first_retry_rows,
    isotonic_apply,
    isotonic_fit,
)
from praxis.recovery.config import load_model_config
from praxis.recovery.dataset import Episode
from tests.recovery.helpers import episodes, true_cdf


@pytest.fixture(scope="module")
def data() -> list[Episode]:
    return episodes(5000, seed=21)


@pytest.fixture(scope="module")
def clf(data: list[Episode]) -> CalibratedClassifier:
    return CalibratedClassifier(load_model_config().classifier).fit(data)


def test_isotonic_pools_violators_and_ties() -> None:
    knots, values = isotonic_fit(np.array([1.0, 2.0, 2.0, 3.0, 4.0]), np.array([0, 1, 0, 0, 1.0]))
    assert np.all(np.diff(values) >= 0)
    # (2, 2, 3) pooled: mean of labels 1, 0, 0
    assert isotonic_apply(knots, values, np.array([2.5]))[0] == pytest.approx(1 / 3)
    assert isotonic_apply(knots, values, np.array([-5.0, 9.0])).tolist() == [0.0, 1.0]


def test_classifier_is_monotone_in_elapsed_and_calibrated(
    data: list[Episode], clf: CalibratedClassifier
) -> None:
    rows = [e.features for e in data[:200]]
    curves = clf.prob_collectible(rows, np.arange(0, 11, dtype=np.float64))
    assert np.all(curves[:, 0] == 0.0)
    assert np.all(np.diff(curves, axis=1) >= -1e-12)
    test = episodes(3000, seed=22)
    kept, elapsed, label = first_retry_rows(test)
    p = clf.predict([e.features for e in kept], elapsed)
    assert metrics.ece(p, label) < 0.05
    truth = np.array([true_cdf(e.features.reason, t) for e, t in zip(kept, elapsed, strict=True)])
    # per-row error of a flexible model fitted on ~3,750 rows with six irrelevant features
    assert np.mean(np.abs(p - truth)) < 0.08


def test_classifier_state_round_trip(clf: CalibratedClassifier, data: list[Episode]) -> None:
    clone = CalibratedClassifier.from_state(clf.params, clf.state(), clf.model_string())
    rows = [e.features for e in data[:30]]
    t = np.full(30, 4.0)
    np.testing.assert_array_equal(clone.predict(rows, t), clf.predict(rows, t))
    with pytest.raises(ValueError, match="feature design"):
        CalibratedClassifier.from_state(clf.params, {**clf.state(), "feature_names": []}, "")
    assert clf.predict([], np.zeros(0)).shape == (0,)
    assert clf.prob_collectible([], np.array([1.0])).shape == (0, 1)


def test_untrained_and_too_small(data: list[Episode]) -> None:
    raw = CalibratedClassifier(load_model_config().classifier)
    for call in (lambda: raw.predict([data[0].features], np.ones(1)), raw.state, raw.model_string):
        with pytest.raises(TrainingError):
            call()
    with pytest.raises(TrainingError):
        raw.fit(data[:50])
    with pytest.raises(TrainingError):
        RateTable().fit([])


def test_rate_table_shrinks_to_the_reason_rate(data: list[Episode]) -> None:
    table = RateTable().fit(data)
    f = data[0].features
    (hits, n) = table.cells[(f.reason, 3)]
    expected = (hits + 5 * table.reason_rate[f.reason]) / (n + 5)
    assert table.predict([f], np.array([3.0]))[0] == pytest.approx(expected)
    assert table.predict([f], np.array([99.0]))[0] == pytest.approx(table.reason_rate[f.reason])


def test_probability_metrics_on_known_values() -> None:
    p, y = np.array([0.9, 0.1, 0.8, 0.3]), np.array([1.0, 0.0, 0.0, 1.0])
    assert metrics.brier(p, y) == pytest.approx((0.01 + 0.01 + 0.64 + 0.49) / 4)
    assert metrics.log_loss(np.array([0.5]), np.array([1.0])) == pytest.approx(math.log(2))
    # ranking 0.9 (pos), 0.8 (neg), 0.3 (pos), 0.1: AP = (1/1 + 2/3) / 2
    assert metrics.pr_auc(p, y) == pytest.approx((1 + 2 / 3) / 2)
    assert math.isnan(metrics.pr_auc(p, np.zeros(4)))
    perfect = np.array([0.0, 0.0, 1.0, 1.0])
    assert metrics.ece(perfect, perfect, bins=2) == 0.0
    rows = metrics.segment_calibration(
        np.full(100, 0.3),
        np.r_[np.ones(30), np.zeros(70)],
        ["a"] * 100,
        min_rows=50,
        tolerance=0.05,
        z=2.5,
    )
    assert rows[0]["passed"] and rows[0]["abs_error"] == pytest.approx(0.0)
    assert metrics.segment_calibration(p, y, ["a"] * 4, min_rows=5, tolerance=0.05, z=2) == []


def test_concordance_orders_intervals() -> None:
    lower = np.array([0.0, 3.0, 7.0])
    upper = np.array([2.0, 5.0, np.inf])
    assert metrics.interval_concordance(np.array([1.0, 2.0, 3.0]), lower, upper) == 1.0
    assert metrics.interval_concordance(np.array([3.0, 2.0, 1.0]), lower, upper) == 0.0
    assert math.isnan(metrics.interval_concordance(np.ones(1), np.zeros(1), np.full(1, np.inf)))


def test_assignment_tests() -> None:
    gaps = [1, 2, 3] * 100
    assert metrics.uniform_assignment_p(gaps, (1, 2, 3)) == pytest.approx(1.0)
    assert metrics.uniform_assignment_p([1] * 90 + [2] * 10, (1, 2, 3)) < 1e-6
    assert math.isnan(metrics.uniform_assignment_p([], (1, 2)))
    assert metrics.independence_p(["a", "b"] * 150, gaps) > 0.01
    assert metrics.independence_p(["a"] * 150 + ["b"] * 150, [1] * 150 + [2] * 150) < 1e-6
    assert math.isnan(metrics.independence_p(["a"] * 3, [1, 2, 3]))


def test_metric_edge_cases() -> None:
    assert len(metrics.reliability(np.array([0.2, 0.8]), np.array([0.0, 1.0]), bins=5)) == 2
    # i's interval ends after every other lower bound: no comparable pair, so no concordance
    lower, upper = np.array([0.0, 1.0]), np.array([9.0, np.inf])
    assert math.isnan(metrics.interval_concordance(np.array([1.0, 2.0]), lower, upper))
    cells = metrics.current_status_fit(
        np.full(60, 0.5),
        np.r_[np.ones(30), np.zeros(30)],
        ["card_declined"] * 60,
        np.full(60, 3.0),
        min_rows=40,
        tolerance=0.05,
        z=3.0,
    )
    assert cells == [
        {
            "n": 60,
            "mean_predicted": 0.5,
            "observed": 0.5,
            "abs_error": 0.0,
            "allowed": cells[0]["allowed"],
            "passed": True,
            "reason": "card_declined",
            "elapsed_days": 3,
        }
    ]
