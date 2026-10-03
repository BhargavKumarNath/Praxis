"""Population distribution tests (required_test.md section 7).

Tolerances, fixed BEFORE running (N = 20,000 customers, seed 42):
* categorical shares (region, industry, tier, payment method): |observed - configured|
  <= 4 * sqrt(p (1 - p) / N)  (4 sigma of a binomial proportion)
* mean log|elasticity| per tier: within 0.04 of configured mu + E[log industry scale]
  (standard error is about 0.007 at this N)
* |elasticity| tier ordering: starter > growth > enterprise (strict)
* within-tier log-sd of base load within 0.04 of configured sigma
* mean payment reliability within 0.01 of the analytic mixture mean
* risky-payer / noisy-volume / new-customer fractions: 4 sigma binomial
* difficult-cohort existence: >= 3% of customers have |elasticity| < 0.5; risky payers have
  mean reliability < 0.7 while others > 0.85
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from praxis.simulator.config import PAYMENT_METHODS, TIERS, SimulationConfig, load_config
from praxis.simulator.population import Population, generate_population

N = 20_000
SEED = 42


@pytest.fixture(scope="module")
def cfg() -> SimulationConfig:
    return load_config().with_overrides(n_customers=N)


@pytest.fixture(scope="module")
def pop(cfg: SimulationConfig) -> Population:
    return generate_population(cfg, SEED)


def within_binomial(observed: float, p: float, n: int, sigmas: float = 4.0) -> bool:
    return abs(observed - p) <= sigmas * math.sqrt(p * (1 - p) / n)


def test_region_mix(cfg: SimulationConfig, pop: Population) -> None:
    for idx, r in enumerate(cfg.regions):
        assert within_binomial(float(np.mean(pop.region == idx)), r.share, N), r.id


def test_industry_mix(cfg: SimulationConfig, pop: Population) -> None:
    for idx, i in enumerate(cfg.industries):
        assert within_binomial(float(np.mean(pop.industry == idx)), i.share, N), i.id


def test_tier_mix(cfg: SimulationConfig, pop: Population) -> None:
    for idx, t in enumerate(cfg.tiers):
        assert within_binomial(float(np.mean(pop.tier == idx)), t.share, N), t.id


def test_payment_method_mix(cfg: SimulationConfig, pop: Population) -> None:
    for idx, share in enumerate(cfg.population.method_shares):
        assert within_binomial(float(np.mean(pop.method == idx)), share, N), PAYMENT_METHODS[idx]


def test_elasticity_is_negative_and_bounded(cfg: SimulationConfig, pop: Population) -> None:
    lo, hi = cfg.population.elasticity_clip
    assert np.all(pop.elasticity < 0)
    assert np.all(np.abs(pop.elasticity) >= lo - 1e-12)
    assert np.all(np.abs(pop.elasticity) <= hi + 1e-12)


def test_elasticity_distribution_by_tier(cfg: SimulationConfig, pop: Population) -> None:
    ind_scale = np.array([i.elasticity_scale for i in cfg.industries])
    ind_share = np.array([i.share for i in cfg.industries])
    expected_log_scale = float(np.sum(ind_share * np.log(ind_scale)) / ind_share.sum())
    means = []
    for idx, t in enumerate(cfg.tiers):
        sel = pop.tier == idx
        observed = float(np.mean(np.log(np.abs(pop.elasticity[sel]))))
        assert abs(observed - (t.elasticity_log_mu + expected_log_scale)) <= 0.04, t.id
        means.append(float(np.mean(np.abs(pop.elasticity[sel]))))
    assert means[0] > means[1] > means[2], "starter must be more price sensitive than enterprise"


def test_industry_elasticity_ordering(cfg: SimulationConfig, pop: Population) -> None:
    by_ind = {
        i.id: float(np.mean(np.abs(pop.elasticity[pop.industry == idx])))
        for idx, i in enumerate(cfg.industries)
    }
    assert by_ind["gaming"] > by_ind["fintech"] > by_ind["health"]


def test_demand_heterogeneity(cfg: SimulationConfig, pop: Population) -> None:
    for idx, t in enumerate(cfg.tiers):
        sel = pop.tier == idx
        assert abs(float(np.std(np.log(pop.base_load[sel]))) - t.base_load_sigma) <= 0.04, t.id
    cv = float(np.std(pop.base_load) / np.mean(pop.base_load))
    p90, p10 = np.percentile(pop.base_load, [90, 10])
    assert cv > 1.5
    assert p90 / p10 > 20


def test_payment_reliability_distribution(cfg: SimulationConfig, pop: Population) -> None:
    p = cfg.population
    method_mean = [a / (a + b) for a, b in (p.method_beta[m] for m in PAYMENT_METHODS)]
    mean_normal = sum(s * m for s, m in zip(p.method_shares, method_mean, strict=True))
    risky_mean = p.risky_beta[0] / sum(p.risky_beta)
    expected = (1 - p.risky_payer_fraction) * mean_normal + p.risky_payer_fraction * risky_mean
    assert abs(float(pop.pay_reliability.mean()) - expected) <= 0.01
    assert np.all((pop.pay_reliability > 0) & (pop.pay_reliability < 1))
    assert within_binomial(float(pop.risky_payer.mean()), p.risky_payer_fraction, N)


def test_difficult_cohorts_exist(cfg: SimulationConfig, pop: Population) -> None:
    assert float(np.mean(np.abs(pop.elasticity) < 0.5)) >= 0.03
    assert pop.pay_reliability[pop.risky_payer].mean() < 0.7
    assert pop.pay_reliability[~pop.risky_payer].mean() > 0.85
    assert within_binomial(float(pop.noisy_volume.mean()), cfg.population.noisy_volume_fraction, N)
    assert set(np.unique(pop.dispersion)) == {
        cfg.population.dispersion_typical,
        cfg.population.dispersion_noisy,
    }


def test_customer_flow_and_tenure(cfg: SimulationConfig, pop: Population) -> None:
    assert within_binomial(float(pop.is_new.mean()), cfg.population.new_customer_fraction, N)
    assert np.all(pop.created_day[~pop.is_new] == 0)
    assert np.all(pop.created_day[pop.is_new] >= 1)
    assert np.all(pop.created_day < cfg.run.days)
    assert np.all(pop.tenure_days[pop.is_new] == 0)
    assert np.all(pop.tenure_days[~pop.is_new] >= 1)


def test_product_mix_valid_and_respects_availability(
    cfg: SimulationConfig, pop: Population
) -> None:
    assert np.allclose(pop.mix.sum(axis=1), 1.0)
    assert np.all(pop.mix >= 0)
    assert np.all((pop.mix > 0).sum(axis=1) >= 1)
    nonzero = pop.mix[pop.mix > 0]
    assert nonzero.min() >= cfg.population.min_mix_share / 1.0 - 1e-9
    prod_ids = [p.id for p in cfg.products]
    for r_idx, reg in enumerate(cfg.regions):
        for prod in reg.unavailable_products:
            assert np.all(pop.mix[pop.region == r_idx, prod_ids.index(prod)] == 0)


def test_industry_mix_shapes_product_mix(cfg: SimulationConfig, pop: Population) -> None:
    prod_ids = [p.id for p in cfg.products]
    ind_ids = [i.id for i in cfg.industries]
    media = pop.mix[pop.industry == ind_ids.index("media")].mean(axis=0)
    ecommerce = pop.mix[pop.industry == ind_ids.index("ecommerce")].mean(axis=0)
    assert media[prod_ids.index("data_transfer_gb")] > ecommerce[prod_ids.index("data_transfer_gb")]


def test_tiers_are_the_contracted_ones(cfg: SimulationConfig) -> None:
    assert tuple(t.id for t in cfg.tiers) == TIERS


def test_no_ground_truth_is_nan(pop: Population) -> None:
    for name, arr in pop.arrays().items():
        if arr.dtype.kind == "f":
            assert np.all(np.isfinite(arr)), name
