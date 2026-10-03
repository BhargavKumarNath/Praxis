"""Scenario configs and single-pass event collection for simulator tests."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from praxis.simulator.config import SimulationConfig, load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import Population, generate_population

SEED = 42


def scenario(
    n_customers: int = 1000, days: int = 56, **sections: dict[str, Any]
) -> SimulationConfig:
    """Default world with selected sections shallow-merged (e.g. infrastructure=...)."""
    base = load_config().with_overrides(n_customers=n_customers, days=days).model_dump(mode="json")
    for name, patch in sections.items():
        base[name] = {**base[name], **patch}
    return SimulationConfig.model_validate(base)


class Collected:
    def __init__(self, config: SimulationConfig, population: Population) -> None:
        self.config = config
        self.population = population
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.events.values())


def collect(config: SimulationConfig, seed: int = SEED, keep: set[str] | None = None) -> Collected:
    population = generate_population(config, seed)
    out = Collected(config, population)
    for event in Engine(config, seed, population).run():
        if keep is None or event["event_type"] in keep:
            out.events[event["event_type"]].append(event)
    return out
