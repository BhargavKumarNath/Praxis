"""Log-log estimators: recovery, cluster-robust coverage, IV under contamination, pooling."""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from praxis.elasticity.estimators import (
    CellInput,
    demean,
    empirical_bayes,
    naive_pre_post,
    within_iv,
    within_slopes,
)

LOG_R = math.log(1.2)


def _clustered(
    rng: np.random.Generator,
    n_customers: int,
    beta: float,
    *,
    products: int = 3,
    rho: float = 0.6,
    per_customer: bool = False,
) -> tuple[NDArray[np.float64], ...]:
    """Each customer has ``products`` units with a shared shock (within-cluster correlation).

    ``per_customer``: one treatment draw per customer (all its units share it) instead of one
    per unit, which is when ignoring the clustering understates the variance.
    """
    cust = np.repeat(np.arange(n_customers), products)
    prod = np.tile(np.arange(products), n_customers)
    draw = rng.random(n_customers)[cust] if per_customer else rng.random(cust.size)
    treated = draw < 0.5
    x = np.where(treated, LOG_R, 0.0)
    shared = rng.normal(0, math.sqrt(rho), n_customers)[cust]
    y = 0.05 * prod + beta * x + shared + rng.normal(0, math.sqrt(1 - rho), cust.size)
    return y, x, prod.astype(float), cust.astype(float)


def test_demean_removes_group_means() -> None:
    v = np.array([1.0, 3.0, 10.0, 20.0, 30.0])
    g = np.array([0, 0, 1, 1, 1])
    assert demean(v, g) == pytest.approx([-1.0, 1.0, -10.0, 0.0, 10.0])


def test_single_group_slope_is_difference_in_means_over_log_ratio() -> None:
    rng = np.random.default_rng(1)
    x = np.where(rng.random(400) < 0.5, LOG_R, 0.0)
    y = -1.5 * x + rng.normal(0, 0.3, 400)
    est = within_slopes(y, x, [np.zeros(400)], np.full(400, "all"), np.arange(400), ci_level=0.95)
    expected = (y[x > 0].mean() - y[x == 0].mean()) / LOG_R
    assert est["all"].estimate == pytest.approx(expected)
    assert est["all"].ci_low < -1.5 < est["all"].ci_high


def test_segment_slopes_recover_their_own_truth() -> None:
    rng = np.random.default_rng(2)
    seg = np.repeat(np.array(["a", "b", "c"]), 3000)
    truth = {"a": -0.6, "b": -1.2, "c": -2.0}
    x = np.where(rng.random(seg.size) < 0.5, LOG_R, 0.0)
    y = np.array([truth[s] for s in seg]) * x + rng.normal(0, 0.2, seg.size)
    est = within_slopes(y, x, [np.zeros(seg.size)], seg, np.arange(seg.size), ci_level=0.95)
    for s, b in truth.items():
        assert est[s].estimate == pytest.approx(b, abs=4 * est[s].se)


def test_fixed_effects_absorb_group_level_trends() -> None:
    """Groups with different trends AND different treated shares: only FE is unbiased."""
    rng = np.random.default_rng(3)
    prod = np.repeat([0.0, 1.0], 2000)
    share = np.where(prod == 1.0, 0.8, 0.2)
    x = np.where(rng.random(4000) < share, LOG_R, 0.0)
    y = 0.5 * prod - 1.0 * x + rng.normal(0, 0.2, 4000)
    seg, cl = np.full(4000, "all"), np.arange(4000)
    fe = within_slopes(y, x, [prod], seg, cl, ci_level=0.95)["all"]
    no_fe = within_slopes(y, x, [np.zeros(4000)], seg, cl, ci_level=0.95)["all"]
    assert fe.estimate == pytest.approx(-1.0, abs=4 * fe.se)
    assert not no_fe.ci_low <= -1.0 <= no_fe.ci_high


@pytest.mark.parametrize("per_customer", [False, True])
def test_cluster_robust_intervals_have_nominal_coverage(per_customer: bool) -> None:
    """Monte Carlo (400 worlds): 95% CR1 intervals cover at the nominal rate.

    With one draw per unit (the Praxis design: independent salts per test) the shared customer
    shock creates no design effect, so unit-level SEs would also be fine. With one draw per
    customer they under-cover; the cluster-robust interval stays nominal in both designs.
    """
    rng = np.random.default_rng(4)
    reps, hit_cluster, hit_naive = 400, 0, 0
    for _ in range(reps):
        y, x, prod, cust = _clustered(rng, 300, -1.3, rho=0.6, per_customer=per_customer)
        seg = np.full(y.size, "all")
        robust = within_slopes(y, x, [prod], seg, cust, ci_level=0.95)["all"]
        naive = within_slopes(y, x, [prod], seg, np.arange(y.size), ci_level=0.95)["all"]
        hit_cluster += robust.ci_low <= -1.3 <= robust.ci_high
        hit_naive += naive.ci_low <= -1.3 <= naive.ci_high
    assert 0.92 <= hit_cluster / reps <= 0.98
    if per_customer:
        assert hit_naive / reps < 0.90


def test_segments_without_variation_or_units_are_skipped() -> None:
    y = np.arange(10, dtype=float)
    seg = np.array(["a"] * 5 + ["b"] * 5)
    x = np.array([0.0] * 5 + [0.0, LOG_R, 0.0, LOG_R, 0.0])
    est = within_slopes(y, x, [np.zeros(10)], seg, np.arange(10), ci_level=0.95)
    assert set(est) == {"b"}
    assert within_slopes(y, x, [np.zeros(10)], seg, np.arange(10), ci_level=0.95, min_units=6) == {}
    one_cluster = within_slopes(y, x, [np.zeros(10)], seg, np.zeros(10), ci_level=0.95)
    assert one_cluster == {}


def test_iv_recovers_elasticity_under_contamination_and_itt_is_diluted() -> None:
    rng = np.random.default_rng(5)
    n = 20_000
    z = np.where(rng.random(n) < 0.5, LOG_R, 0.0)
    contaminated = (z == 0.0) & (rng.random(n) < 0.25)
    d = np.where(contaminated, LOG_R, z)
    y = -1.4 * d + rng.normal(0, 0.3, n)
    fe = [np.zeros(n)]
    iv = within_iv(y, d, z, fe, np.arange(n), ci_level=0.95)
    assert iv is not None
    assert iv.estimate == pytest.approx(-1.4, abs=4 * iv.se)
    assert iv.first_stage == pytest.approx(0.75, abs=0.02)
    assert iv.first_stage_f > 100
    itt = within_slopes(y, z, fe, np.full(n, "all"), np.arange(n), ci_level=0.95)["all"]
    assert itt.estimate == pytest.approx(-1.4 * 0.75, abs=4 * itt.se)
    assert abs(itt.estimate) < abs(iv.estimate)
    assert within_iv(y, d, np.zeros(n), fe, np.arange(n), ci_level=0.95) is None


def test_naive_pre_post_absorbs_the_common_trend() -> None:
    rng = np.random.default_rng(6)
    trend, beta = 0.06, -1.2
    treated_delta = trend + beta * LOG_R + rng.normal(0, 0.2, 5000)
    naive = naive_pre_post(treated_delta, LOG_R, ci_level=0.95)
    assert naive.estimate == pytest.approx(beta + trend / LOG_R, abs=0.05)
    assert not naive.ci_low <= beta <= naive.ci_high
    with pytest.raises(ValueError, match="two"):
        naive_pre_post(np.array([0.1]), LOG_R, ci_level=0.95)


def _cells(estimates: list[float], se: float) -> list[CellInput]:
    tiers, inds = ("t1", "t2", "t3"), ("i1", "i2")
    labels = [(t, i) for t in tiers for i in inds]
    return [CellInput(f"{t}|{i}", t, i, b, se) for (t, i), b in zip(labels, estimates, strict=True)]


def test_empirical_bayes_pools_completely_when_cells_are_additive() -> None:
    additive = [-1.0 + t + i for t in (0.0, -0.5, -1.0) for i in (0.0, 0.2)]
    pooled, tau = empirical_bayes(_cells(additive, 0.1))
    assert tau == 0.0
    assert all(p.shrinkage == pytest.approx(1.0) for p in pooled)
    assert [p.estimate for p in pooled] == pytest.approx(additive)


def test_empirical_bayes_keeps_precise_heterogeneous_cells() -> None:
    noisy = [-1.0, -0.2, -1.9, -0.4, -3.0, -1.1]
    pooled, tau = empirical_bayes(_cells(noisy, 0.01))
    assert tau > 0.1
    assert all(p.shrinkage < 0.05 for p in pooled)
    assert [p.estimate for p in pooled] == pytest.approx(noisy, abs=0.01)
    assert all(p.sd > 0 for p in pooled)
