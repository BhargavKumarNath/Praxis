"""required_test.md s18 availability: with the LLM provider disabled, core pricing, billing
and recovery continue safely.

Two parts: (1) no core module imports ``praxis.agent`` at all (checked in a clean
interpreter, on top of the import-linter layer contract); (2) with the provider down and the
agent disabled, a pricing cycle (shadow and execute) runs and executes exactly as without
the agent. The billing / dunning journey (``tests/dunning/test_e2e.py``) runs in the same
suite and has no agent dependency to disable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

from praxis.agent.config import AgentPolicy
from praxis.agent.executor import ToolExecutor
from praxis.agent.operator import Operator
from praxis.agent.provider import DownProvider, Task
from praxis.agent.wiring import TOOLS
from praxis.pricing.config import Mode
from praxis.pricing.service import PricingService
from praxis.pricing.store import MemoryDecisionStore
from tests.agent.conftest import World
from tests.pricing.helpers import AS_OF
from tests.pricing.test_service import inputs, two_product_policy

CORE = [
    "praxis.api.app", "praxis.pricing.service", "praxis.pricing.__main__",
    "praxis.payments.service", "praxis.payments.processor", "praxis.dunning.service",
    "praxis.dunning.executor", "praxis.dunning.consumer", "praxis.mlops.pipeline",
    "praxis.streaming.pipeline", "praxis.ops.monitor",
]  # fmt: skip


def test_core_modules_never_load_the_agent() -> None:
    code = (
        "import importlib, json, sys\n"
        f"for m in {CORE!r}: importlib.import_module(m)\n"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('praxis.agent'))))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_pricing_runs_normally_with_the_provider_down(
    memory_world: World, policy: AgentPolicy
) -> None:
    executor = ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, policy)
    down = Operator(executor, DownProvider(), fallback=None).investigate(
        Task("daily_check", date(2026, 8, 20))
    )
    off = policy.model_copy(update={"agent": policy.agent.model_copy(update={"enabled": False})})
    disabled = Operator(
        ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, off), DownProvider(),
        fallback=None,
    ).investigate(Task("daily_check", date(2026, 8, 20)))  # fmt: skip
    assert down.outcome == "provider_unavailable" and disabled.outcome == "agent_disabled"

    with_agent_down = MemoryDecisionStore()
    baseline = MemoryDecisionStore()
    for store in (with_agent_down, baseline):
        shadow = PricingService(two_product_policy(), inputs(), store).run_cycle(AS_OF)
        assert shadow.audit_complete
        execute = PricingService(two_product_policy(Mode.EXECUTE), inputs(), store).run_cycle(AS_OF)
        assert execute.audit_complete
    strip = [(e.decision_id, e.product, e.to_price_micros) for e in with_agent_down.executions()]
    assert strip == [(e.decision_id, e.product, e.to_price_micros) for e in baseline.executions()]
    assert len(strip) == 1
