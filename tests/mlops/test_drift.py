"""Population stability index: bins, noise bound, validity floor (pure)."""

from __future__ import annotations

import numpy as np
import pytest

from praxis.mlops import drift


def test_identical_distributions_have_zero_psi() -> None:
    values = np.random.default_rng(0).normal(size=5000)
    ref = drift.profile({"x": values}, 10)["x"]
    assert len(ref["edges"]) == 9 and sum(ref["reference"]) == pytest.approx(1.0)
    assert drift.psi(ref["reference"], drift.proportions(values, ref["edges"])) == 0.0
    assert ref["n"] == 5000


def test_constant_and_empty_columns() -> None:
    assert drift.bin_edges(np.array([np.nan, np.inf]), 10) == []
    edges = drift.bin_edges(np.ones(100), 10)
    assert edges == [1.0]
    assert drift.proportions(np.ones(100), edges) == [0.0, 1.0]
    assert drift.proportions(np.array([np.nan]), edges) == [0.0, 0.0]
    with pytest.raises(ValueError, match="same bins"):
        drift.psi([0.5, 0.5], [1.0])


def test_noise_bound_shrinks_with_sample_size() -> None:
    small = drift.noise_bound(10, 50, 50, 0.001)
    large = drift.noise_bound(10, 5000, 5000, 0.001)
    assert small > 1.0 > 0.02 > large
    assert drift.noise_bound(1, 10, 10, 0.001) == float("inf")


def test_no_change_on_independent_samples_does_not_fire() -> None:
    rng = np.random.default_rng(1)
    ref = drift.profile({"x": rng.normal(size=2000)}, 10)
    fired = [
        drift.compare(ref, {"x": rng.normal(size=1000)}, threshold=0.25, alpha=0.001)["fired"]
        for _ in range(50)
    ]
    assert not any(fired)


def test_material_shift_fires_and_small_samples_are_insufficient() -> None:
    rng = np.random.default_rng(2)
    ref = drift.profile({"x": rng.normal(size=2000), "y": rng.normal(size=2000)}, 10)
    shifted = {"x": rng.normal(1.0, 1.0, size=1000), "y": rng.normal(size=20)}
    out = drift.compare(ref, shifted, threshold=0.25, alpha=0.001)
    assert out["fired"] == ["x"] and out["psi"]["x"] > 0.25 > out["noise_bound"]["x"]
    assert out["insufficient"] == ["y"] and "y" not in out["psi"]
    assert drift.compare(ref, {}, threshold=0.25, alpha=0.001)["psi"] == {}


def test_repeated_values_count_once() -> None:
    region_values = np.repeat(np.arange(30, dtype=np.float64), 12)  # 30 region-days x 12 series
    assert drift.effective_n(region_values) == 30
