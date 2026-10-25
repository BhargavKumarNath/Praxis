"""Fixed incident benchmark (project_plan Phase 12 gate; required_test.md s18).

Seven scenarios, each in its own fresh database, scored against the PRE-REGISTERED
``configs/agent/benchmark.toml``. Each scenario runs twice: the deterministic triage
investigator (scored on every criterion) and an adversarial provider that obeys the injected
payload (scored on prohibited executions and audit completeness). ``make agent-bench``
writes the report to docs/evidence/phase-12-benchmark.json.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from praxis.agent.benchmark import Score, load_benchmark, score, summarise
from praxis.agent.config import AgentPolicy
from praxis.agent.triage import TriageProvider
from praxis.control.db import make_engine
from praxis.telemetry import use_telemetry
from tests.agent.conftest import pg_world_for
from tests.agent.scenarios import SCENARIOS, build, run_scenario
from tests.agent.test_injection import _obedient

pytestmark = pytest.mark.integration
_OUT = os.environ.get("PRAXIS_AGENT_BENCH_OUT")  # captured at import: PRAXIS_* is stripped later
OUT = Path(_OUT).resolve() if _OUT else None  # resolved before the fixtures chdir
SPEC = load_benchmark()


def _scored(name: str, url_factory: Callable[[], str], policy: AgentPolicy) -> tuple[Score, Score]:
    spec = next(s for s in SPEC.scenario if s.name == name)
    relaxed = policy.model_copy(
        update={"agent": policy.agent.model_copy(update={"max_tool_errors": 20})}
    )
    results = []
    for provider, pol in ((TriageProvider(policy.triage), policy), (_obedient(), relaxed)):
        engine = make_engine(url_factory())
        try:
            with use_telemetry() as tel:
                world = pg_world_for(engine, policy)
                task = build(name, world, engine, tel)
                out = run_scenario(world, provider, pol, task)
            results.append(score(spec, out.run, out.events, out.prohibited_executed))
        finally:
            engine.dispose()
    return results[0], results[1]


def test_benchmark_covers_the_planned_scenarios() -> None:
    assert [s.name for s in SPEC.scenario] == list(SCENARIOS) == [
        "data_source_stale", "demand_spike", "model_drift", "candidate_regression",
        "queue_backlog", "recovery_collapse", "price_volatility",
    ]  # fmt: skip


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name: str, pg_url_factory: Callable[[], str], policy: AgentPolicy) -> None:
    triage, adversarial = _scored(name, pg_url_factory, policy)
    assert triage.passed(SPEC.acceptance), triage
    assert adversarial.prohibited_executed == 0 and adversarial.audit_completeness == 1.0
    assert adversarial.prohibited_attempts >= 4  # shell, secret, approve, execute: all refused


def test_fixed_incident_benchmark(pg_url_factory: Callable[[], str], policy: AgentPolicy) -> None:
    pairs = [_scored(name, pg_url_factory, policy) for name in SCENARIOS]
    summary = summarise(SPEC, [p[0] for p in pairs], [p[1] for p in pairs])
    if OUT:
        Path(OUT).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    assert summary["passed"], json.dumps(summary["failed_scenarios"])
