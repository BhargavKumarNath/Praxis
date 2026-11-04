"""Phase 15 failure 11: the AI operator's LLM provider is unavailable.

Real boundary: the agent's provider interface (a provider that raises ``ProviderUnavailable``
on every call, as an HTTP adapter does on timeouts / 5xx), the Postgres audit, request and
incident stores (``pg_world``), and the ops path that raises ``agent_provider_degraded``.
No LLM is called (CLAUDE.md s24).
"""

from __future__ import annotations

from datetime import date

from praxis.agent.config import AgentPolicy
from praxis.agent.operator import Operator
from praxis.agent.provider import DownProvider, Task
from praxis.agent.triage import TriageProvider
from praxis.pricing.config import Mode
from praxis.pricing.service import PricingService
from praxis.pricing.store import MemoryDecisionStore
from praxis.telemetry import Telemetry
from tests.agent.conftest import World
from tests.pricing.helpers import AS_OF
from tests.pricing.test_service import inputs, two_product_policy
from tests.resilience.harness import Measured, OpsProbe, detection, judge, quiet_fired

DAY = date(2026, 8, 20)
RULE = "agent_provider_degraded"


def _pricing_strip() -> list[tuple[str, str, int]]:
    store = MemoryDecisionStore()
    PricingService(two_product_policy(), inputs(), store).run_cycle(AS_OF)
    PricingService(two_product_policy(Mode.EXECUTE), inputs(), store).run_cycle(AS_OF)
    return [(e.decision_id, e.product, e.to_price_micros) for e in store.executions()]


def test_agent_unavailable(pg_world: World, policy: AgentPolicy, tel: Telemetry) -> None:
    fid = "agent_unavailable"
    triage = TriageProvider(policy.triage)
    baseline_prices = _pricing_strip()
    probe = OpsProbe()
    probe.tick()

    # Inject: the provider fails every call.
    probe.mark()
    with_fallback = [
        Operator(pg_world.executor, DownProvider(), fallback=triage).investigate(
            Task("daily_check", DAY)
        )
        for _ in range(3)
    ]  # fmt: skip
    without = Operator(pg_world.executor, DownProvider(), fallback=None).investigate(
        Task("daily_check", DAY)
    )
    prices_during = _pricing_strip()
    probe.tick()
    ticks, wall = detection(probe, (RULE,))

    # Recover: the provider answers again.
    restored = Operator(pg_world.executor, triage, fallback=triage).investigate(
        Task("daily_check", DAY)
    )
    clear = probe.until_clear((RULE,), max_ticks=5)

    runs = [*with_fallback, without, restored]
    guarded_executed = [
        e for r in runs for e in pg_world.audit.events(session_id=r.session_id)
        # policy max_tier = 2: tier-2 tools only CREATE pending requests; anything above
        # that, executed, would be a guardrail bypass
        if e.phase == "outcome" and (e.tier or 0) > policy.agent.max_tier and e.status == "ok"
    ]  # fmt: skip
    executed_by_agent = sum(
        1 for r in pg_world.requests.recent(500) if r.decided_by == policy.agent.actor
    )  # fmt: skip
    fallback_events = sum(
        1
        for r in runs
        for e in pg_world.audit.events(session_id=r.session_id)
        if e.status == "fallback"
    )  # fmt: skip
    failures = int(tel.snapshot().total("agent_provider_failures_total"))
    record = judge(Measured(
        fid,
        actual=(f"with fallback: {[(r.outcome, r.grounded, r.providers) for r in with_fallback]}; "
                f"without: {without.outcome}; pricing executions identical="
                f"{prices_during == baseline_prices}; guarded executions {len(guarded_executed)}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=(all(r.outcome == "completed" and r.grounded and r.fallback_used
                       for r in with_fallback)
                   and without.outcome == "provider_unavailable"
                   and prices_during == baseline_prices
                   and restored.outcome == "completed" and not restored.fallback_used),
        recovery_actual=f"restored provider: {restored.outcome}, providers {restored.providers}",
        clear_ticks=clear,
        data_loss=sum(1 for r in runs if not pg_world.audit.events(session_id=r.session_id)),
        duplicate_side_effects=len(guarded_executed) + executed_by_agent,
        evidence={"provider_failures_counted": failures,
                  "requests_created": [(r.kind, r.status) for r in pg_world.requests.recent(500)],
                  "fallback_audit_events": fallback_events,
                  "pricing_executions": len(baseline_prices),
                  "incidents_notified": [(e, i.rule_id) for e, i in probe.notifier.sent]},
    ))  # fmt: skip
    assert failures == 4 and fallback_events == 3
    assert record.passed, record.reasons
