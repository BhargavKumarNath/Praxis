"""Assemble the operator from the control plane (CLI) or from in-memory stores (drills)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from praxis.agent.actions import ACTION_TOOLS
from praxis.agent.audit import AuditStore, PostgresAuditStore
from praxis.agent.config import AgentPolicy
from praxis.agent.evidence import READ_TOOLS
from praxis.agent.executor import ToolExecutor
from praxis.agent.operator import Operator
from praxis.agent.provider import LLMProvider
from praxis.agent.requests import ApprovalDesk, PostgresRequestStore
from praxis.agent.tools import Backends, PostgresRecoverySource, Tool
from praxis.agent.triage import TriageProvider
from praxis.config import Environment
from praxis.mlops.config import load_mlops_config
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryStore
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import IncidentManager, LogNotifier, PostgresIncidentStore
from praxis.ops.system import build_reader
from praxis.pricing.config import load_policy
from praxis.pricing.freeze import PostgresFreezeStore
from praxis.pricing.store import PostgresDecisionStore

TOOLS: tuple[Tool, ...] = READ_TOOLS + ACTION_TOOLS


class UnknownProvider(ValueError):
    """The configured provider has no adapter in this build."""


def provider_for(name: str, policy: AgentPolicy) -> LLMProvider:
    if name == TriageProvider.name:
        return TriageProvider(policy.triage)
    raise UnknownProvider(f"no adapter for provider {name!r} (only {TriageProvider.name})")


def operator_for(executor: ToolExecutor) -> Operator:
    policy = executor.policy
    fallback = provider_for(policy.agent.fallback, policy) if policy.agent.fallback else None
    return Operator(executor, provider_for(policy.agent.provider, policy), fallback=fallback)


@dataclass(frozen=True)
class ControlPlane:
    backends: Backends
    audit: AuditStore
    desk: ApprovalDesk


def control_plane(
    engine: Engine, environment: Environment, warehouse_path: Path | None = None
) -> ControlPlane:
    registry = ModelRegistry(RegistryStore(engine), load_mlops_config(), environment=environment)
    requests = PostgresRequestStore(engine)
    freezes = PostgresFreezeStore(engine)
    backends = Backends(
        ops=load_ops_config(),
        incidents=IncidentManager(PostgresIncidentStore(engine), LogNotifier()),
        freezes=freezes,
        requests=requests,
        registry=registry,
        decisions=PostgresDecisionStore(engine),
        recovery=PostgresRecoverySource(engine),
        products=frozenset(load_policy().products),
    )
    # Fleet-wide metrics and their inputs (ADR 0020), the same view as the health API.
    reader = build_reader(engine, warehouse_path)
    backends.system = lambda: reader.read(backends.clock())
    return ControlPlane(
        backends,
        PostgresAuditStore(engine),
        ApprovalDesk(requests, registry=registry, freezes=freezes),
    )
