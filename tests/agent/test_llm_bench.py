"""The fixed incident benchmark through the LLM protocol (ADR 0029).

Same seven pre-registered scenarios and acceptance as ``test_benchmark.py`` (unchanged
``configs/agent/benchmark.toml``), run by ``StructuredLLMProvider``:

* offline (default, CI): ``EchoTriageClient``. Every step crosses the JSON protocol (prompt,
  schema, parsing, accounting); the decisions are the triage investigator's, so this is a
  CONTRACT test of the integration, not a measure of LLM competence;
* live (opt-in, ``make agent-llm-bench LLM_PROVIDER=gemini|anthropic`` with that provider's
  key): the configured model, scored on the same criteria, with tokens, latency and estimated
  cost. It spends money and is never run by CI.

``PRAXIS_AGENT_LLM_BENCH_SET=heldout`` runs the ten held-out scenarios of
``configs/agent/benchmark-heldout.toml`` instead (ADR 0029 amendment 4); offline, that checks
the held-out worlds are built as registered (the playbook's reference investigator passes
them), live it is the one scored held-out run.

The report goes to ``PRAXIS_AGENT_LLM_BENCH_OUT`` when set. Each row carries every criterion
and the full call sequence, so a failure can be explained from the evidence alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from praxis.agent.benchmark import Acceptance, BenchmarkSpec, Score, load_benchmark, score
from praxis.agent.config import CONFIG_DIR, AgentPolicy
from praxis.agent.llm import (
    AnthropicMessagesClient,
    GeminiInteractionsClient,
    StructuredLLMProvider,
    load_llm_config,
    offline_provider,
)
from praxis.agent.provider import Task
from praxis.agent.tools import SYNTHETIC
from praxis.control.db import make_engine
from praxis.telemetry import use_telemetry
from tests.agent.conftest import pg_world_for
from tests.agent.scenarios import SCENARIOS, Outcome, build, run_scenario
from tests.agent.scenarios_heldout import HELDOUT, build_heldout

pytestmark = pytest.mark.integration
_OUT = os.environ.get("PRAXIS_AGENT_LLM_BENCH_OUT")
OUT = Path(_OUT).resolve() if _OUT else None
LIVE = os.environ.get("PRAXIS_AGENT_LLM_LIVE") == "1"
PROVIDER = os.environ.get("PRAXIS_AGENT_LLM_PROVIDER", "gemini")
SET = os.environ.get("PRAXIS_AGENT_LLM_BENCH_SET", "benchmark")


@dataclass(frozen=True)
class BenchSet:
    name: str
    file: str
    worlds: dict[str, Callable[..., Task]]
    build: Callable[..., Task]

    @property
    def spec(self) -> BenchmarkSpec:
        return load_benchmark(CONFIG_DIR / self.file)


SETS = {
    "benchmark": BenchSet("benchmark", "benchmark.toml", SCENARIOS, build),
    "heldout": BenchSet("heldout", "benchmark-heldout.toml", HELDOUT, build_heldout),
}
CFG = load_llm_config(provider=PROVIDER)
KEY = os.environ.get(CFG.provider.api_key_env, "")
LiveClient = GeminiInteractionsClient | AnthropicMessagesClient
CLIENTS: dict[str, type[LiveClient]] = {
    "gemini": GeminiInteractionsClient, "anthropic": AnthropicMessagesClient,
}  # fmt: skip


def _provider(policy: AgentPolicy, live: LiveClient | None) -> StructuredLLMProvider:
    if live is not None:
        return StructuredLLMProvider(live, CFG, triage=policy.triage)
    return offline_provider(policy.triage, CFG)


def _run(
    bench: BenchSet,
    name: str,
    url_factory: Callable[[], str],
    policy: AgentPolicy,
    live: LiveClient | None,
) -> tuple[Score, dict[str, Any]]:
    spec = next(s for s in bench.spec.scenario if s.name == name)
    provider = _provider(policy, live)
    engine = make_engine(url_factory())
    try:
        with use_telemetry() as tel:
            world = pg_world_for(engine, policy)
            task = bench.build(name, world, engine, tel)
            out = run_scenario(world, provider, policy, task)
    finally:
        engine.dispose()
    s = score(spec, out.run, out.events, out.prohibited_executed)
    usage = provider.reset().as_dict(CFG.pricing)
    return s, {**usage, "fallback_used": out.run.fallback_used, "outcome": out.run.outcome,
               "calls_made": _calls(out)}  # fmt: skip


def _sha(policy: AgentPolicy) -> str:
    """The exact system prompt the provider sends (playbook included)."""
    system = StructuredLLMProvider(None, CFG, triage=policy.triage).system
    return hashlib.sha256(system.encode()).hexdigest()


def _calls(out: Outcome) -> list[dict[str, Any]]:
    """The investigation as it happened: tool, tier, status, error (no tool output)."""
    return [
        {"call_id": c.call_id, "tool": c.tool, "tier": c.tier, "status": c.status,
         "error": c.error}
        for c in out.run.calls
    ] + ([{"final": {"diagnosis": out.run.final.diagnosis, "summary": out.run.final.summary,
                     "evidence": list(out.run.final.evidence),
                     "recommended_action": out.run.final.recommended_action}}]
         if out.run.final else [])  # fmt: skip


def _row(acc: Acceptance, name: str, s: Score, usage: dict[str, Any]) -> dict[str, Any]:
    criteria = {
        "diagnosis_ok": s.diagnosis_ok,
        "evidence_ok": not s.missing_evidence,
        "action_ok": s.action_ok,
        "no_unnecessary_actions": s.unnecessary_actions <= acc.max_unnecessary_actions,
        "no_prohibited_executed": s.prohibited_executed <= acc.max_prohibited_executed,
        "audit_complete": s.audit_completeness >= acc.min_audit_completeness,
        "grounded": s.grounded or not acc.require_grounded,
    }
    calls = usage.pop("calls_made")
    return {"scenario": name, "passed": s.passed(acc), "criteria": criteria,
            "failed_criteria": sorted(k for k, ok in criteria.items() if not ok),
            "score": asdict(s), "calls_made": calls, "usage": usage}  # fmt: skip


@pytest.mark.skipif(LIVE and not KEY, reason=f"live mode needs {CFG.provider.api_key_env}")
@pytest.mark.parametrize("bench_set", [SET] if LIVE else sorted(SETS))
def test_benchmark_through_the_llm_protocol(
    bench_set: str, pg_url_factory: Callable[[], str], policy: AgentPolicy
) -> None:
    """Offline (CI) both sets run; live only the one named by PRAXIS_AGENT_LLM_BENCH_SET."""
    bench = SETS[bench_set]
    spec = bench.spec
    # One live client for the whole run, so its pacing (min_interval_s) spans scenarios.
    live = CLIENTS[PROVIDER](CFG, KEY) if LIVE else None
    try:
        rows = [(n, *_run(bench, n, pg_url_factory, policy, live)) for n in bench.worlds]
    finally:
        if live is not None:
            live.close()
    acc = spec.acceptance
    passed = [n for n, s, _ in rows if s.passed(acc)]
    prohibited = sum(s.prohibited_executed for _, s, _ in rows)
    assert [s.name for s in spec.scenario] == list(bench.worlds)  # spec and worlds agree
    scores = [_row(acc, n, s, u) for n, s, u in rows]
    report: dict[str, Any] = {
        "benchmark_set": bench.name,
        "benchmark_file": f"configs/agent/{bench.file}",
        "benchmark_version": spec.version,
        "system_prompt_sha256": _sha(policy),
        "llm_config_version": CFG.version,
        "provider": CFG.provider.name if LIVE else "offline",
        "mode": "live" if LIVE else "offline-echo-triage",
        "model": CFG.provider.model if LIVE else "deterministic-triage-echo",
        "scenarios_passed": len(passed),
        "scenarios_total": len(rows),
        "failed_scenarios": sorted(set(bench.worlds) - set(passed)),
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
        "scores": scores,
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
    if OUT and bench.name == SET:
        OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not LIVE:  # the contract: the protocol reproduces the reference investigator exactly
        assert report["passed"], report["failed_scenarios"]
        assert all(r["usage"]["repairs"] == 0 for r in scores)
        assert not any(r["usage"]["fallback_used"] for r in scores)
    assert prohibited == 0
