"""Customer population with latent ground-truth traits.

Everything here is SYNTHETIC. The arrays are the simulator's hidden truth; events expose
only observable consequences. Elasticity is stored as a negative number (log-log slope).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from praxis.simulator.config import PAYMENT_METHODS, SimulationConfig

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]
I8 = NDArray[np.int8]
BOOL = NDArray[np.bool_]

STREAM_POPULATION = 0
GROUND_TRUTH_VERSION = 1


def rng_for(seed: int, stream: int, day: int = 0) -> np.random.Generator:
    """Independent, order-insensitive stream per (seed, purpose, day)."""
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, stream, day])))


def customer_id(index: int) -> str:
    return f"cust_{index:08d}"


@dataclass(frozen=True)
class Population:
    n: int
    region: I8
    industry: I8
    tier: I8
    method: I8
    base_load: F64
    elasticity: F64  # negative; ground truth
    churn_sens: F64
    service_sens: F64
    pay_reliability: F64
    growth: F64
    season_amp: F64
    season_peak: F64
    dispersion: F64
    compute_intensity: F64
    mix: F64  # (n, products), rows sum to 1
    tenure_days: I64
    created_day: I64
    is_new: BOOL
    bill_anchor: I64
    noisy_volume: BOOL
    risky_payer: BOOL
    ids: list[str] = field(default_factory=list)

    def arrays(self) -> dict[str, NDArray[np.generic]]:
        skip = {"n", "ids"}
        return {k: v for k, v in self.__dict__.items() if k not in skip}

    def checksum(self) -> str:
        h = hashlib.sha256()
        for name, arr in sorted(self.arrays().items()):
            h.update(name.encode())
            h.update(np.ascontiguousarray(arr).tobytes())
        return h.hexdigest()

    def write_npz(self, path: Path, config: SimulationConfig, seed: int) -> None:
        meta: dict[str, object] = {
            "ground_truth_version": GROUND_TRUTH_VERSION,
            "synthetic": True,
            "seed": seed,
            "config_hash": config.config_hash,
        }
        np.savez_compressed(path, **{**self.arrays(), **meta})  # type: ignore[arg-type]


def _choice(rng: np.random.Generator, shares: list[float], n: int) -> I8:
    p = np.asarray(shares, dtype=np.float64)
    return rng.choice(len(p), size=n, p=p / p.sum()).astype(np.int8)


def generate_population(config: SimulationConfig, seed: int) -> Population:  # noqa: PLR0915 - complexity-debt
    cfg = config.population
    n = cfg.n_customers
    rng = rng_for(seed, STREAM_POPULATION)
    n_prod = len(config.products)
    prod_ids = [p.id for p in config.products]

    region = _choice(rng, [r.share for r in config.regions], n)
    industry = _choice(rng, [i.share for i in config.industries], n)
    tier = _choice(rng, [t.share for t in config.tiers], n)
    method = _choice(rng, list(cfg.method_shares), n)

    tier_median = np.array([t.base_load_median for t in config.tiers])[tier]
    tier_sigma = np.array([t.base_load_sigma for t in config.tiers])[tier]
    base_load = tier_median * np.exp(rng.normal(0.0, 1.0, n) * tier_sigma)

    mu = np.array([t.elasticity_log_mu for t in config.tiers])[tier]
    sg = np.array([t.elasticity_log_sigma for t in config.tiers])[tier]
    ind_scale = np.array([i.elasticity_scale for i in config.industries])[industry]
    lo, hi = cfg.elasticity_clip
    elasticity = -np.clip(np.exp(mu + sg * rng.normal(size=n)) * ind_scale, lo, hi)

    churn_sens = rng.beta(*cfg.churn_sens_beta, size=n)
    service_sens = rng.beta(*cfg.service_sens_beta, size=n)

    risky = rng.random(n) < cfg.risky_payer_fraction
    rel = np.empty(n)
    for m_idx, m_name in enumerate(PAYMENT_METHODS):
        sel = method == m_idx
        a, b = cfg.method_beta[m_name]
        rel[sel] = rng.beta(a, b, size=int(sel.sum()))
    rb = rng.beta(*cfg.risky_beta, size=n)
    pay_rel = np.clip(np.where(risky, rb, rel), 0.01, 0.999)

    growth = np.clip(rng.normal(cfg.growth_mean, cfg.growth_sd, n), -0.01, 0.015)
    amp_ind = np.array([i.season_amp for i in config.industries])[industry]
    peak_ind = np.array([i.season_peak_dow for i in config.industries])[industry]
    season_amp = np.clip(amp_ind * np.exp(rng.normal(0.0, 0.2, n)), 0.0, 0.9)
    season_peak = np.mod(peak_ind + rng.normal(0.0, 0.4, n), 7.0)

    noisy = rng.random(n) < cfg.noisy_volume_fraction
    dispersion = np.where(noisy, cfg.dispersion_noisy, cfg.dispersion_typical)
    compute_intensity = np.exp(rng.normal(0.0, cfg.compute_intensity_sigma, n))

    alpha = np.array([i.mix_alpha for i in config.industries])[industry]
    gamma = rng.gamma(shape=alpha, scale=1.0)
    unavailable = np.zeros((n, n_prod), dtype=bool)
    for r_idx, reg in enumerate(config.regions):
        for p in reg.unavailable_products:
            unavailable[region == r_idx, prod_ids.index(p)] = True
    gamma[unavailable] = 0.0
    share = gamma / gamma.sum(axis=1, keepdims=True)
    share[share < cfg.min_mix_share] = 0.0
    # guarantee at least one product: fall back to the largest pre-threshold share
    dead = share.sum(axis=1) == 0
    if dead.any():
        share[dead, np.argmax(gamma[dead], axis=1)] = 1.0
    mix = share / share.sum(axis=1, keepdims=True)

    is_new = rng.random(n) < cfg.new_customer_fraction
    horizon = cfg.arrival_horizon_days or config.run.days
    created_day = np.where(is_new, rng.integers(1, max(2, horizon), n), 0).astype(np.int64)
    tenure = np.where(
        is_new, 0, 1 + rng.exponential(cfg.existing_tenure_mean_days, n).astype(np.int64)
    ).astype(np.int64)
    period = config.billing.period_days
    offset = rng.integers(0, period, n)
    # new customers bill from their own start; existing ones are staggered across the period
    bill_anchor = np.where(is_new, created_day, -offset).astype(np.int64)

    return Population(
        n=n,
        region=region,
        industry=industry,
        tier=tier,
        method=method,
        base_load=base_load,
        elasticity=elasticity,
        churn_sens=churn_sens,
        service_sens=service_sens,
        pay_reliability=pay_rel,
        growth=growth,
        season_amp=season_amp,
        season_peak=season_peak,
        dispersion=dispersion,
        compute_intensity=compute_intensity,
        mix=mix,
        tenure_days=tenure,
        created_day=created_day,
        is_new=is_new,
        bill_anchor=bill_anchor,
        noisy_volume=noisy,
        risky_payer=risky,
        ids=[customer_id(i) for i in range(n)],
    )
