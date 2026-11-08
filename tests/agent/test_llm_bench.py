"""The fixed incident benchmark through the LLM protocol (ADR 0029).

Same seven pre-registered scenarios and acceptance as ``test_benchmark.py`` (unchanged
``configs/agent/benchmark.toml``), run by ``StructuredLLMProvider``:

* offline (default, CI): ``EchoTriageClient``. Every step crosses the JSON protocol (prompt,
  schema, parsing, accounting); the decisions are the triage investigator's, so this is a
  CONTRACT test of the integration, not a measure of LLM competence;
* live (opt-in, ``make agent-llm-bench`` with ``PRAXIS_GEMINI_API_KEY``): the configured Gemini
  model, scored on the same criteria, with tokens, latency and estimated cost. It spends money
  (about USD 0.05 per run at the configured prices) and is never run by CI.

The report goes to ``PRAXIS_AGENT_LLM_BENCH_OUT`` when set.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from praxis.agent.benchmark import Score, load_benchmark, score
from praxis.agent.config import AgentPolicy
from praxis.agent.llm import (
    GeminiInteractionsClient,
    StructuredLLMProvider,
    load_llm_config,
    offline_provider,
)
from praxis.agent.tools import SYNTHETIC
from praxis.control.db import make_engine
from praxis.telemetry import use_telemetry
from tests.agent.conftest import pg_world_for
from tests.agent.scenarios import SCENARIOS, build, run_scenario

pytestmark = pytest.mark.integration
_OUT = os.environ.get("PRAXIS_AGENT_LLM_BENCH_OUT")
OUT = Path(_OUT).resolve() if _OUT else None
LIVE = os.environ.get("PRAXIS_AGENT_LLM_LIVE") == "1"
KEY = os.environ.get("PRAXIS_GEMINI_API_KEY", "")
SPEC = load_benchmark()
CFG = load_llm_config()


def _provider(policy: AgentPolicy) -> StructuredLLMProvider:
    if LIVE:
        return StructuredLLMProvider(GeminiInteractionsClient(CFG, KEY), CFG)
    return offline_provider(policy.triage, CFG)


def _run(
    name: str, url_factory: Callable[[], str], policy: AgentPolicy
) -> tuple[Score, dict[str, Any]]:
    spec = next(s for s in SPEC.scenario if s.name == name)
    provider = _provider(policy)
    engine = make_engine(url_factory())
    try:
        with use_telemetry() as tel:
            world = pg_world_for(engine, policy)
            task = build(name, world, engine, tel)
            out = run_scenario(world, provider, policy, task)
    finally:
        engine.dispose()
    s = score(spec, out.run, out.events, out.prohibited_executed)
    usage = provider.reset().as_dict(CFG.pricing)
    return s, {**usage, "fallback_used": out.run.fallback_used, "outcome": out.run.outcome}


@pytest.mark.skipif(LIVE and not KEY, reason="live mode needs PRAXIS_GEMINI_API_KEY")
def test_benchmark_through_the_llm_protocol(
    pg_url_factory: Callable[[], str], policy: AgentPolicy
) -> None:
    rows = [(name, *_run(name, pg_url_factory, policy)) for name in SCENARIOS]
    acc = SPEC.acceptance
    passed = [n for n, s, _ in rows if s.passed(acc)]
    prohibited = sum(s.prohibited_executed for _, s, _ in rows)
    report = {
        "benchmark_version": SPEC.version,
        "llm_config_version": CFG.version,
        "mode": "live" if LIVE else "offline-echo-triage",
        "model": CFG.provider.model if LIVE else "deterministic-triage-echo",
        "scenarios_passed": len(passed),
        "scenarios_total": len(rows),
        "failed_scenarios": sorted(set(SCENARIOS) - set(passed)),
        "prohibited_executed_total": prohibited,
        "passed": len(passed) >= acc.min_scenarios_passed and prohibited == 0,
        "usage_total": {
            k: sum(u[k] for _, _, u in rows)
            for k in (
                "calls",
                "retries",
                "repairs",
                "input_tokens",
                "output_tokens",
                "estimated_cost_usd",
            )
        },
        "pricing_usd_per_mtok": CFG.pricing.model_dump(mode="json"),
        "scores": [
            {
                "scenario": n,
                "passed": s.passed(acc),
                "diagnosis": s.diagnosis,
                "action": s.action,
                "grounded": s.grounded,
                "steps": s.steps,
                "latency_ms": s.latency_ms,
                "usage": u,
            }
            for n, s, u in rows
        ],
        "notes": [
            SYNTHETIC,
            "offline mode: the deterministic triage investigator's steps serialised through "
            "the LLM JSON protocol; it tests the integration, not a model. Token counts in "
            "offline mode are character estimates (chars / 4), not provider counts."
            if not LIVE
            else "live mode: provider-reported tokens; cost is an estimate from the "
            "configured list prices, not a bill.",
        ],
    }
    if OUT:
        OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not LIVE:  # the contract: the protocol reproduces the reference investigator exactly
        assert report["passed"], report["failed_scenarios"]
        assert all(u["repairs"] == 0 and not u["fallback_used"] for _, _, u in rows)
    assert prohibited == 0
