"""Simulator configuration (stable, validated, hashable).

The TOML file ``configs/simulator/default.toml`` is the stable configuration. Every
number that shapes the world lives there, so a run is fully described by
``(config, seed)``. ``config_hash`` identifies the exact world in manifests.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "simulator" / "default.toml"
TIERS = ("starter", "growth", "enterprise")
PAYMENT_METHODS = ("card", "direct_debit", "wallet")
FAILURE_REASONS = (
    "insufficient_funds",
    "card_declined",
    "expired_card",
    "processor_error",
    "authentication_required",
)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Product(_Cfg):
    id: str
    ref_price_micros: int = Field(gt=0)
    base_cost_micros: int = Field(gt=0)
    load_weight: float = Field(gt=0, description="capacity load units per billing unit")
    compute_intensive: bool = False  # demand scales with customer compute_intensity
    emits_requests: bool = False  # usage also produces request.completed events


class Region(_Cfg):
    id: str
    share: float = Field(gt=0)
    utc_offset_hours: int = Field(ge=-12, le=14)
    base_latency_ms: float = Field(gt=0)
    base_error_rate: float = Field(ge=0, lt=1)
    cost_multiplier: float = Field(gt=0)
    target_utilization: float = Field(gt=0, lt=1)
    unavailable_products: tuple[str, ...] = ()


class Industry(_Cfg):
    id: str
    share: float = Field(gt=0)
    season_amp: float = Field(ge=0, lt=1)
    season_peak_dow: float = Field(ge=0, lt=7)
    elasticity_scale: float = Field(gt=0)
    mix_alpha: tuple[float, ...]


class TierSpec(_Cfg):
    id: str
    share: float = Field(gt=0)
    base_load_median: float = Field(gt=0)
    base_load_sigma: float = Field(gt=0)
    elasticity_log_mu: float
    elasticity_log_sigma: float = Field(gt=0)
    base_fee_minor: int = Field(ge=0)
    churn_hazard_daily: float = Field(gt=0, lt=1)
    conversion_base: float = Field(gt=0, lt=1)


class Population(_Cfg):
    n_customers: int = Field(gt=0)
    new_customer_fraction: float = Field(ge=0, lt=1)
    existing_tenure_mean_days: float = Field(gt=0)
    churn_sens_beta: tuple[float, float]
    service_sens_beta: tuple[float, float]
    growth_mean: float
    growth_sd: float = Field(gt=0)
    compute_intensity_sigma: float = Field(gt=0)
    elasticity_clip: tuple[float, float]
    dispersion_typical: float = Field(gt=0)
    dispersion_noisy: float = Field(gt=0)
    noisy_volume_fraction: float = Field(ge=0, le=1)
    method_shares: tuple[float, float, float]
    method_beta: dict[str, tuple[float, float]]
    risky_payer_fraction: float = Field(ge=0, le=1)
    risky_beta: tuple[float, float]
    min_mix_share: float = Field(ge=0, lt=1)


class CapacityShock(_Cfg):
    region: str
    start_day: int = Field(ge=0)
    end_day: int = Field(gt=0)
    capacity_multiplier: float = Field(gt=0)


class DemandSpike(_Cfg):
    region: str
    start_day: int = Field(ge=0)
    end_day: int = Field(gt=0)
    multiplier: float = Field(gt=0)
    product: str | None = None


class ProductOutage(_Cfg):
    region: str
    product: str
    start_day: int = Field(ge=0)
    end_day: int = Field(gt=0)


class Infrastructure(_Cfg):
    diurnal_amplitude: float = Field(ge=0, lt=1)
    diurnal_peak_local_hour: float = Field(ge=0, lt=24)
    hourly_noise_sigma: float = Field(ge=0)
    latency_load_coeff: float = Field(ge=0)
    latency_overload_coeff: float = Field(ge=0)
    p95_base_ratio: float = Field(gt=1)
    p95_load_coeff: float = Field(ge=0)
    error_knee: float = Field(gt=0)
    error_slope: float = Field(ge=0)
    cost_scarcity_coeff: float = Field(ge=0)
    capacity_shocks: tuple[CapacityShock, ...] = ()
    demand_spikes: tuple[DemandSpike, ...] = ()
    product_outages: tuple[ProductOutage, ...] = ()


class PriceChange(_Cfg):
    product: str
    day: int = Field(ge=0)
    multiplier: float = Field(gt=0)


class Intervention(_Cfg):
    id: str
    product: str
    start_day: int = Field(ge=0)
    end_day: int = Field(gt=0)
    treated_fraction: float = Field(gt=0, lt=1)
    price_multiplier: float = Field(gt=0)
    salt: str
    # Failure injection: share of control units charged the treatment price (logged as
    # control). Excluded from the canonical JSON when 0, so worlds without contamination keep
    # the config_hash (and every event id and golden checksum) they had before the field existed.
    contamination_fraction: float = Field(
        default=0.0, ge=0.0, lt=1.0, exclude_if=lambda v: v == 0.0
    )


class Pricing(_Cfg):
    price_changes: tuple[PriceChange, ...] = ()
    interventions: tuple[Intervention, ...] = ()


class Billing(_Cfg):
    period_days: int = Field(gt=0)
    max_attempts: int = Field(ge=1, le=6)
    retry_offsets_days: tuple[int, ...]
    retry_success_base: float = Field(ge=0, le=1)
    retry_success_slope: float = Field(ge=0, le=1)
    failure_reason_weights: dict[str, float]


class Behaviour(_Cfg):
    churn_price_beta: float
    churn_latency_beta: float
    churn_error_beta: float
    churn_failure_beta: float
    burden_decay: float = Field(gt=0, lt=1)
    churn_hazard_cap: float = Field(gt=0, le=1)
    demand_service_beta: float = Field(ge=0)
    conversion_price_beta: float
    conversion_clip: tuple[float, float]
    tier_change_daily: float = Field(ge=0, lt=1)


class Run(_Cfg):
    start_date: date
    days: int = Field(gt=0)


class SimulationConfig(_Cfg):
    run: Run
    population: Population
    products: tuple[Product, ...]
    regions: tuple[Region, ...]
    industries: tuple[Industry, ...]
    tiers: tuple[TierSpec, ...]
    infrastructure: Infrastructure
    pricing: Pricing = Pricing()
    billing: Billing
    behaviour: Behaviour

    @model_validator(mode="after")
    def _check(self) -> Self:  # noqa: C901, PLR0912 - complexity-debt
        pids = [p.id for p in self.products]
        rids = [r.id for r in self.regions]
        if len(set(pids)) != len(pids) or len(set(rids)) != len(rids):
            raise ValueError("product and region ids must be unique")
        if tuple(t.id for t in self.tiers) != TIERS:
            raise ValueError(f"tiers must be exactly {TIERS} in order")
        for ind in self.industries:
            if len(ind.mix_alpha) != len(pids):
                raise ValueError(f"industry {ind.id}: mix_alpha needs {len(pids)} entries")
        for r in self.regions:
            if any(p not in pids for p in r.unavailable_products):
                raise ValueError(f"region {r.id}: unknown unavailable product")
        known = set(rids)
        for s in self.infrastructure.capacity_shocks:
            _need(s.region in known and s.start_day < s.end_day, "capacity shock")
        for sp in self.infrastructure.demand_spikes:
            _need(sp.region in known and sp.start_day < sp.end_day, "demand spike")
            _need(sp.product is None or sp.product in pids, "demand spike product")
        for o in self.infrastructure.product_outages:
            _need(o.region in known and o.product in pids and o.start_day < o.end_day, "outage")
        for c in self.pricing.price_changes:
            _need(c.product in pids, "price change product")
        _check_interventions(self.pricing.interventions, pids)
        b = self.billing
        if len(b.retry_offsets_days) != b.max_attempts - 1:
            raise ValueError("retry_offsets_days needs max_attempts - 1 entries")
        if any(o < 1 for o in b.retry_offsets_days) or list(b.retry_offsets_days) != sorted(
            b.retry_offsets_days
        ):
            raise ValueError("retry offsets must be increasing and >= 1")
        if b.retry_offsets_days and b.retry_offsets_days[-1] >= b.period_days:
            raise ValueError("retries must finish inside the billing period")
        if set(b.failure_reason_weights) != set(FAILURE_REASONS):
            raise ValueError("failure_reason_weights must cover every failure reason")
        if set(self.population.method_beta) != set(PAYMENT_METHODS):
            raise ValueError("method_beta must cover every payment method")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def with_overrides(
        self,
        *,
        n_customers: int | None = None,
        days: int | None = None,
        start_date: date | None = None,
    ) -> Self:
        update: dict[str, object] = {}
        if n_customers is not None:
            update["population"] = self.population.model_copy(update={"n_customers": n_customers})
        run_update: dict[str, object] = {}
        if days is not None:
            run_update["days"] = days
        if start_date is not None:
            run_update["start_date"] = start_date
        if run_update:
            update["run"] = self.run.model_copy(update=run_update)
        return self.model_copy(update=update)


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(f"invalid {what} definition")


def _check_interventions(items: tuple[Intervention, ...], pids: list[str]) -> None:
    for i, a in enumerate(items):
        _need(a.product in pids and a.start_day < a.end_day, f"intervention {a.id}")
        for b in items[i + 1 :]:
            if a.id == b.id:
                raise ValueError("intervention ids must be unique")
            overlap = a.start_day < b.end_day and b.start_day < a.end_day
            if overlap and a.product == b.product:
                raise ValueError("overlapping interventions on one product are not allowed")


def load_config(path: Path | None = None, scenario: Path | None = None) -> SimulationConfig:
    """Load the stable world, optionally overlaid with a scenario TOML.

    A scenario names only what differs from the base world (e.g. ``run.days`` or
    ``infrastructure.demand_spikes``). Tables merge key by key; arrays and scalars replace.
    The merged result is validated as a whole and gets its own ``config_hash``.
    """
    raw = tomllib.loads((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    if scenario is not None:
        raw = _deep_merge(raw, tomllib.loads(scenario.read_text(encoding="utf-8")))
    return SimulationConfig.model_validate(raw)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out
