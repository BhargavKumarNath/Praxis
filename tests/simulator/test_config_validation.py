"""The stable config rejects incoherent worlds instead of silently simulating them."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from praxis.simulator.config import SimulationConfig, load_config
from praxis.simulator.infrastructure import Infrastructure
from praxis.simulator.population import generate_population
from tests.simulator.sim_helpers import scenario


def dump() -> dict[str, Any]:
    return load_config().model_dump(mode="json")


def rejects(mutate: Any) -> None:
    raw = dump()
    mutate(raw)
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(raw)


def test_default_config_loads_and_hash_is_stable() -> None:
    a, b = load_config(), load_config()
    assert a.config_hash == b.config_hash and len(a.config_hash) == 64
    assert a.with_overrides(n_customers=5).config_hash != a.config_hash
    assert a.with_overrides().config_hash == a.config_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["products"].append(dict(r["products"][0])),
        lambda r: r["regions"].append(dict(r["regions"][0])),
        lambda r: r.update(tiers=list(reversed(r["tiers"]))),
        lambda r: r["industries"][0].update(mix_alpha=[1.0]),
        lambda r: r["regions"][0].update(unavailable_products=["nope"]),
        lambda r: r["infrastructure"].update(
            capacity_shocks=[
                {"region": "mars", "start_day": 1, "end_day": 2, "capacity_multiplier": 0.5}
            ]
        ),
        lambda r: r["infrastructure"].update(
            capacity_shocks=[
                {"region": "eu_west", "start_day": 5, "end_day": 5, "capacity_multiplier": 0.5}
            ]
        ),
        lambda r: r["infrastructure"].update(
            demand_spikes=[
                {"region": "eu_west", "start_day": 1, "end_day": 3, "multiplier": 2, "product": "x"}
            ]
        ),
        lambda r: r["infrastructure"].update(
            demand_spikes=[{"region": "mars", "start_day": 1, "end_day": 3, "multiplier": 2}]
        ),
        lambda r: r["infrastructure"].update(
            product_outages=[{"region": "eu_west", "product": "x", "start_day": 1, "end_day": 3}]
        ),
        lambda r: r["pricing"].update(
            price_changes=[{"product": "x", "day": 1, "multiplier": 1.1}]
        ),
        lambda r: r["pricing"].update(
            interventions=[
                {
                    "id": "a",
                    "product": "api_requests",
                    "start_day": 1,
                    "end_day": 10,
                    "treated_fraction": 0.5,
                    "price_multiplier": 1.1,
                    "salt": "s",
                },
                {
                    "id": "b",
                    "product": "api_requests",
                    "start_day": 5,
                    "end_day": 12,
                    "treated_fraction": 0.5,
                    "price_multiplier": 1.2,
                    "salt": "t",
                },
            ]
        ),
        lambda r: r["pricing"].update(
            interventions=[
                {
                    "id": "a",
                    "product": "api_requests",
                    "start_day": 1,
                    "end_day": 10,
                    "treated_fraction": 0.5,
                    "price_multiplier": 1.1,
                    "salt": "s",
                },
                {
                    "id": "a",
                    "product": "cpu_minutes",
                    "start_day": 1,
                    "end_day": 10,
                    "treated_fraction": 0.5,
                    "price_multiplier": 1.1,
                    "salt": "t",
                },
            ]
        ),
        lambda r: r["billing"].update(retry_offsets_days=[3]),
        lambda r: r["billing"].update(retry_offsets_days=[7, 3]),
        lambda r: r["billing"].update(retry_offsets_days=[3, 40]),
        lambda r: r["billing"].update(retry_offsets_days=[0, 3]),
        lambda r: r["billing"].update(failure_reason_weights={"insufficient_funds": 1.0}),
        lambda r: r["population"].update(method_beta={"card": [1, 1]}),
        lambda r: r["run"].update(days=0),
        lambda r: r["population"].update(unknown_field=1),
    ],
)
def test_incoherent_configs_are_rejected(mutate: Any) -> None:
    rejects(mutate)


def test_non_overlapping_interventions_on_one_product_are_allowed() -> None:
    raw = dump()
    raw["pricing"]["interventions"] = [
        {
            "id": "a",
            "product": "api_requests",
            "start_day": 1,
            "end_day": 5,
            "treated_fraction": 0.5,
            "price_multiplier": 1.1,
            "salt": "s",
        },
        {
            "id": "b",
            "product": "api_requests",
            "start_day": 5,
            "end_day": 9,
            "treated_fraction": 0.5,
            "price_multiplier": 1.2,
            "salt": "t",
        },
    ]
    SimulationConfig.model_validate(raw)


def test_every_customer_gets_a_product_even_with_an_extreme_mix_threshold() -> None:
    cfg = scenario(n_customers=500, population={"min_mix_share": 0.95})
    pop = generate_population(cfg, 1)
    assert np.allclose(pop.mix.sum(axis=1), 1.0)
    assert np.all((pop.mix > 0).sum(axis=1) >= 1)


def test_product_specific_demand_spike_only_touches_that_product() -> None:
    cfg = scenario(
        n_customers=100,
        infrastructure={
            "demand_spikes": [
                {
                    "region": "eu_west",
                    "start_day": 2,
                    "end_day": 4,
                    "multiplier": 3.0,
                    "product": "gpu_minutes",
                }
            ]
        },
    )
    infra = Infrastructure(cfg, generate_population(cfg, 1))
    spike = infra.spike_on(3)
    r, g = infra.region_ids.index("eu_west"), infra.product_ids.index("gpu_minutes")
    assert spike[r, g] == 3.0 and spike.sum() == spike.size + 2.0
    assert np.all(infra.spike_on(0) == 1.0)
