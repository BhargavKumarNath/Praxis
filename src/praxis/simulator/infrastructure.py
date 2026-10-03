"""Infrastructure model: regional capacity, load-dependent latency / errors, availability.

Capacity is provisioned once from the population's expected load and each region's
``target_utilization``. Each day, realised load plus scheduled shocks, spikes and outages
determine hourly utilisation, which drives latency, error rate, throttling and marginal
cost. Customers react to service quality with a one-day lag (no circularity).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from praxis.simulator.config import SimulationConfig
from praxis.simulator.population import Population

F64 = NDArray[np.float64]
BOOL = NDArray[np.bool_]
HOURS = 24


@dataclass(frozen=True)
class RegionDay:
    """Service state for every region on one day (R regions, P products, 24 hours)."""

    capacity: F64  # (R,) load units
    util_h: F64  # (R, 24)
    p50_h: F64
    p95_h: F64
    err_h: F64
    cost_h: NDArray[np.int64]  # (R, 24, P) micro-GBP per unit
    served_ratio: F64  # (R,)
    mean_p50: F64  # (R,)
    mean_p95: F64
    mean_err: F64
    degradation: F64  # (R,) >= 0, relative latency degradation
    avail: BOOL  # (R, P)


class Infrastructure:
    def __init__(self, config: SimulationConfig, population: Population) -> None:
        self._cfg = config
        infra = config.infrastructure
        self.region_ids = [r.id for r in config.regions]
        self.product_ids = [p.id for p in config.products]
        n_r, n_p = len(self.region_ids), len(self.product_ids)
        self.weights = np.array([p.load_weight for p in config.products])
        self._base_lat = np.array([r.base_latency_ms for r in config.regions])
        self._base_err = np.array([r.base_error_rate for r in config.regions])
        self._cost_mult = np.array([r.cost_multiplier for r in config.regions])
        self._base_cost = np.array([p.base_cost_micros for p in config.products], dtype=float)

        expected = np.bincount(population.region, weights=population.base_load, minlength=n_r)
        target = np.array([r.target_utilization for r in config.regions])
        self.base_capacity = np.maximum(expected / target, 1.0)

        peak_utc = np.mod(
            infra.diurnal_peak_local_hour - np.array([r.utc_offset_hours for r in config.regions]),
            24.0,
        )
        hours = np.arange(HOURS)[None, :]
        self.diurnal = 1.0 + infra.diurnal_amplitude * np.cos(
            2 * np.pi * (hours - peak_utc[:, None]) / 24.0
        )  # (R, 24), mean exactly 1 per region

        self._structural_unavail = np.zeros((n_r, n_p), dtype=bool)
        for r_idx, reg in enumerate(config.regions):
            for p in reg.unavailable_products:
                self._structural_unavail[r_idx, self.product_ids.index(p)] = True
        self._ref_p50 = 1.5 * self._base_lat  # "normal" latency for degradation scoring

    def _window(self, day: int, start: int, end: int) -> bool:
        return start <= day < end

    def capacity_on(self, day: int) -> F64:
        cap: F64 = self.base_capacity.copy()
        for s in self._cfg.infrastructure.capacity_shocks:
            if self._window(day, s.start_day, s.end_day):
                cap[self.region_ids.index(s.region)] *= s.capacity_multiplier
        return cap

    def spike_on(self, day: int) -> F64:
        m = np.ones((len(self.region_ids), len(self.product_ids)))
        for sp in self._cfg.infrastructure.demand_spikes:
            if self._window(day, sp.start_day, sp.end_day):
                r = self.region_ids.index(sp.region)
                if sp.product is None:
                    m[r, :] *= sp.multiplier
                else:
                    m[r, self.product_ids.index(sp.product)] *= sp.multiplier
        return m

    def availability_on(self, day: int) -> BOOL:
        avail = ~self._structural_unavail
        for o in self._cfg.infrastructure.product_outages:
            if self._window(day, o.start_day, o.end_day):
                avail[self.region_ids.index(o.region), self.product_ids.index(o.product)] = False
        return avail

    def evaluate(self, day: int, load: F64, rng: np.random.Generator) -> RegionDay:
        """Hourly service state given the day's total requested load per region."""
        infra = self._cfg.infrastructure
        cap = self.capacity_on(day)
        noise = np.exp(rng.normal(0.0, infra.hourly_noise_sigma, self.diurnal.shape))
        util_h = (load / cap)[:, None] * self.diurnal * noise
        over = np.maximum(util_h - 1.0, 0.0)
        p50 = self._base_lat[:, None] * (
            1.0 + infra.latency_load_coeff * util_h**3 + infra.latency_overload_coeff * over
        )
        p95 = p50 * (infra.p95_base_ratio + infra.p95_load_coeff * util_h**2)
        err = np.minimum(
            self._base_err[:, None] + infra.error_slope * np.maximum(util_h - infra.error_knee, 0),
            0.95,
        )
        ratio_h = np.minimum(1.0, 1.0 / np.maximum(util_h, 1e-9))
        w = self.diurnal
        served_ratio = (w * ratio_h).sum(axis=1) / w.sum(axis=1)
        mean_p50 = p50.mean(axis=1)
        degradation = np.maximum(mean_p50 / self._ref_p50 - 1.0, 0.0)
        scarcity = 1.0 + infra.cost_scarcity_coeff * np.maximum(util_h - 0.8, 0.0)
        cost = np.rint(
            self._base_cost[None, None, :] * self._cost_mult[:, None, None] * scarcity[:, :, None]
        ).astype(np.int64)
        return RegionDay(
            capacity=cap,
            util_h=util_h,
            p50_h=p50,
            p95_h=p95,
            err_h=err,
            cost_h=cost,
            served_ratio=served_ratio,
            mean_p50=mean_p50,
            mean_p95=p95.mean(axis=1),
            mean_err=err.mean(axis=1),
            degradation=degradation,
            avail=self.availability_on(day),
        )
