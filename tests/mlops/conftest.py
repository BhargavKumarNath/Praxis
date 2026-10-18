from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine

from praxis.config import Environment
from praxis.mlops.config import MlopsConfig
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.scoring import load_data
from praxis.mlops.store import RegistryStore
from praxis.mlops.training import Candidate, train_candidate, utc_midnight
from tests.mlops import helpers as h


@dataclass(frozen=True)
class World:
    clean: Path
    drifted: Path
    root: Path

    def date(self, i: int) -> date:
        return h.day(h.panel(), i)


@pytest.fixture(scope="session")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    root = tmp_path_factory.mktemp("mlops-world")
    p = h.panel()
    return World(
        h.write(root / "clean.duckdb", p), h.write(root / "drift.duckdb", h.shifted(p)), root
    )


def _train(world: World, warehouse: Path, end_day: int, bad: bool = False) -> Candidate:
    end = world.date(end_day)
    cfg = h.bad_forecast_config() if bad else h.forecast_config()
    return train_candidate(
        load_data(warehouse, end=end),
        cfg,
        h.mlops_config().monitoring,
        world.root / "models",
        code_revision="test-rev",
        now=utc_midnight(end),
    )


@pytest.fixture(scope="session")
def champion(world: World) -> Candidate:
    """Trained on targets through day 111 (both warehouses are identical up to day 125)."""
    return _train(world, world.clean, h.TRAIN_END_DAY)


@pytest.fixture(scope="session")
def good(world: World) -> Candidate:
    """Retrained on four more weeks of clean data."""
    return _train(world, world.clean, h.TRAIN_END_DAY + 28)


@pytest.fixture(scope="session")
def bad(world: World) -> Candidate:
    """Same data as ``good``, deliberately misconfigured."""
    return _train(world, world.clean, h.TRAIN_END_DAY + 28, bad=True)


@pytest.fixture
def cfg() -> MlopsConfig:
    return h.mlops_config()


@pytest.fixture
def registry_store(pg_engine: Engine) -> RegistryStore:
    return RegistryStore(pg_engine)


@pytest.fixture
def make_registry(registry_store: RegistryStore, cfg: MlopsConfig) -> Callable[..., ModelRegistry]:
    def make(**kwargs: object) -> ModelRegistry:
        kwargs.setdefault("environment", Environment.LOCAL)
        return ModelRegistry(registry_store, cfg, **kwargs)  # type: ignore[arg-type]

    return make


@pytest.fixture
def registry(make_registry: Callable[..., ModelRegistry]) -> Iterator[ModelRegistry]:
    yield make_registry()
