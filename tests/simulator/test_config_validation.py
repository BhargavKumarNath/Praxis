"""The stable config rejects incoherent worlds instead of silently simulating them."""

from __future__ import annotations

from pathlib import Path
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


def test_start_date_override_changes_identity_and_shifts_events() -> None:
    from datetime import date

    from praxis.simulator.runner import run_simulation

    base = load_config().with_overrides(n_customers=5, days=2)
    moved = base.with_overrides(start_date=date(2026, 8, 20))
    assert moved.run.start_date == date(2026, 8, 20) and moved.run.days == 2
    assert moved.config_hash != base.config_hash
    assert base.with_overrides(days=2).run.start_date == base.run.start_date
    result = run_simulation(moved, 3)
    assert result.event_count > 0


def test_scenario_overlay_merges_tables_and_replaces_arrays(tmp_path: Path) -> None:
    overlay = tmp_path / "s.toml"
    overlay.write_text(
        "[run]\ndays = 20\n\n[[infrastructure.demand_spikes]]\n"
        'region = "eu_west"\nstart_day = 2\nend_day = 4\nmultiplier = 1.5\n'
    )
    base = load_config()
    merged = load_config(scenario=overlay)
    assert merged.run.days == 20 and merged.run.start_date == base.run.start_date
    assert len(merged.infrastructure.demand_spikes) == 1
    assert merged.infrastructure.diurnal_amplitude == base.infrastructure.diurnal_amplitude
    assert merged.config_hash != base.config_hash


def test_invalid_scenario_overlay_is_rejected(tmp_path: Path) -> None:
    overlay = tmp_path / "bad.toml"
    overlay.write_text('[[pricing.price_changes]]\nproduct = "nope"\nday = 1\nmultiplier = 1.1\n')
    with pytest.raises(ValidationError):
        load_config(scenario=overlay)


def test_forecast_evaluation_scenario_is_valid_and_pinned() -> None:
    """The pre-registered evaluation world (ADR 0010) must not drift silently."""
    path = Path(__file__).resolve().parents[2] / "configs/simulator/scenarios/forecast_eval.toml"
    cfg = load_config(scenario=path)
    assert cfg.run.days == 196
    assert len(cfg.infrastructure.demand_spikes) == 7
    assert len(cfg.pricing.price_changes) == 4
    assert cfg.config_hash.startswith("c6bf9fc33e02")
