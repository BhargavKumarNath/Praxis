"""Agent fixtures: the REAL executor, tools, permission layer and audit over real stores.

``memory_world`` uses the in-memory stores (no registry, no recovery read model);
``pg_world`` wires every backend to a fresh migrated Postgres database. Only the model
provider is ever doubled (``ScriptedProvider``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from praxis.agent.audit import AuditStore, MemoryAuditStore, PostgresAuditStore
from praxis.agent.config import AgentPolicy, load_agent_policy
from praxis.agent.executor import Session, ToolExecutor
from praxis.agent.requests import (
    ApprovalDesk,
    MemoryRequestStore,
    PostgresRequestStore,
    RequestStore,
)
from praxis.agent.tools import Backends, PostgresRecoverySource
from praxis.agent.wiring import TOOLS
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryStore
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import (
    IncidentManager,
    MemoryIncidentStore,
    PostgresIncidentStore,
    RecordingNotifier,
)
from praxis.pricing.freeze import FreezeStore, MemoryFreezeStore, PostgresFreezeStore
from praxis.pricing.store import MemoryDecisionStore, PostgresDecisionStore
from tests.mlops.helpers import mlops_config

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
PRODUCTS = frozenset({"api_requests", "cpu_minutes", "gpu_minutes"})


@dataclass
class World:
    backends: Backends
    audit: AuditStore
    requests: RequestStore
    freezes: FreezeStore
    desk: ApprovalDesk
    executor: ToolExecutor
    notifier: RecordingNotifier

    def session(self) -> Session:
        return self.executor.new_session()


@pytest.fixture
def policy() -> AgentPolicy:
    return load_agent_policy()


def _world(backends: Backends, audit: AuditStore, policy: AgentPolicy) -> World:
    assert isinstance(backends.requests, (MemoryRequestStore, PostgresRequestStore))
    desk = ApprovalDesk(backends.requests, registry=backends.registry, freezes=backends.freezes)
    notifier = backends.incidents.notifier
    assert isinstance(notifier, RecordingNotifier)
    return World(
        backends, audit, backends.requests, backends.freezes, desk,
        ToolExecutor(TOOLS, backends, audit, policy), notifier,
    )  # fmt: skip


@pytest.fixture
def memory_world(policy: AgentPolicy) -> World:
    freezes = MemoryFreezeStore()
    backends = Backends(
        ops=load_ops_config(),
        incidents=IncidentManager(MemoryIncidentStore(), RecordingNotifier()),
        freezes=freezes,
        requests=MemoryRequestStore(),
        decisions=MemoryDecisionStore(freezes),
        products=PRODUCTS,
        clock=lambda: NOW,
    )
    return _world(backends, MemoryAuditStore(), policy)


@pytest.fixture
def pg_world(pg_engine: Engine, policy: AgentPolicy) -> World:
    return pg_world_for(pg_engine, policy)


def pg_world_for(engine: Engine, policy: AgentPolicy) -> World:
    backends = Backends(
        ops=load_ops_config(),
        incidents=IncidentManager(PostgresIncidentStore(engine), RecordingNotifier()),
        freezes=PostgresFreezeStore(engine),
        requests=PostgresRequestStore(engine),
        registry=ModelRegistry(RegistryStore(engine), mlops_config()),
        decisions=PostgresDecisionStore(engine),
        recovery=PostgresRecoverySource(engine),
        products=PRODUCTS,
        clock=lambda: NOW,
    )
    return _world(backends, PostgresAuditStore(engine), policy)
