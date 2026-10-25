"""required_test.md s18 permission tests: the agent fails SAFELY and every attempt is audited.

The agent is asked to execute shell, call an undeclared tool, expose secrets, run SQL, bypass
approval, override optimiser constraints and silently alter prices. Each attempt is refused
by the executor (not by a prompt), leaves no side effect, and has a complete audit record.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, exc, text

from praxis.agent.audit import incomplete_calls
from praxis.agent.config import AgentPolicy
from praxis.agent.executor import ToolExecutor
from praxis.agent.requests import RequestError
from praxis.agent.tools import Tier
from praxis.agent.wiring import TOOLS
from praxis.pricing.config import Mode
from praxis.pricing.store import NotExecutable
from tests.agent.conftest import World
from tests.agent.helpers import ok_call, recorded_change

FORBIDDEN = [
    ("run_shell", {"command": "rm -rf /"}),
    ("bash", {"cmd": "cat /etc/passwd"}),
    ("execute_sql", {"query": "UPDATE price_executions SET to_price_micros = 1"}),
    ("sql", {"q": "SELECT * FROM pg_shadow"}),
    ("read_secret", {"name": "PRAXIS_STRIPE_SECRET_KEY"}),
    ("get_settings", {}),
    ("set_price", {"product": "api_requests", "price_micros": 1}),
    ("execute_price", {"decision_id": "dec-00000000000000000000"}),
    ("approve_request", {"request_id": "req_0000000000000000"}),
    ("approve_decision", {"decision_id": "dec-00000000000000000000", "approver": "agent"}),
    ("promote_model", {"model_version": "demand-hybrid-cccccccccccc"}),
    ("optimise", {"product": "api_requests", "max_step": 0.9}),
    ("__import__", {"name": "os"}),
]


def test_no_declared_tool_can_execute_shell_sql_secrets_prices_or_approvals() -> None:
    names = {t.name for t in TOOLS}
    assert names.isdisjoint({n for n, _ in FORBIDDEN})
    for tool in TOOLS:
        fields = set(tool.args.model_fields)
        assert not fields & {"command", "sql", "query", "secret", "approver", "max_step",
                              "min_contribution_margin", "price"}, tool.name  # fmt: skip
    # the only price-valued argument is the counterfactual LOOKUP of a recorded candidate
    priced = [t.name for t in TOOLS if "price_micros" in t.args.model_fields]
    assert priced == ["run_pricing_counterfactual"]


@pytest.mark.parametrize(("name", "args"), FORBIDDEN)
def test_undeclared_capabilities_are_refused_and_audited(
    memory_world: World, name: str, args: dict[str, object]
) -> None:
    session = memory_world.session()
    result = memory_world.executor.call(session, name, args)
    assert result.status == "refused" and result.tool == "undeclared"
    assert "not a declared tool" in (result.error or "")
    events = memory_world.audit.events(call_id=result.call_id)
    assert [e.phase for e in events] == ["received", "outcome"]
    assert all(e.tool == "undeclared" and e.actor == "praxis-agent" for e in events)
    # hostile argument VALUES are never stored verbatim, only keys and a hash
    stored = repr([e.args for e in events])
    assert all(str(v) not in stored for v in args.values() if len(str(v)) >= 8)
    assert memory_world.requests.recent() == [] and memory_world.freezes.active() == []


def test_secrets_never_reach_tool_output_or_audit(
    memory_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk_test_" + "Z" * 24  # built at runtime: no secret-looking literal in the repo
    monkeypatch.setenv("PRAXIS_STRIPE_SECRET_KEY", secret)
    session = memory_world.session()
    outputs = [memory_world.executor.call(session, t.name, {}) for t in TOOLS]
    blob = repr([o.message() for o in outputs]) + repr(memory_world.audit.events())
    assert secret not in blob and "PRAXIS_STRIPE_SECRET_KEY" not in blob


def test_tier_ceiling_comes_from_policy_not_from_arguments(
    memory_world: World, policy: AgentPolicy
) -> None:
    read_only = policy.model_copy(update={"agent": policy.agent.model_copy(update={"max_tier": 0})})
    executor = ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, read_only)
    session = executor.new_session()
    assert {t["tier"] for t in executor.catalogue(session)} == {0}
    evidence = executor.call(session, "get_system_health", {}).call_id
    for name, args in [
        ("open_incident", {"diagnosis": "unknown", "summary": "x", "evidence": [evidence]}),
        ("freeze_pricing", {"scope": "*", "reason": "x", "evidence": [evidence],
                            "max_tier": 2}),  # an injected extra field cannot grant a tier
    ]:  # fmt: skip
        result = executor.call(session, name, args)
        assert result.status in ("refused", "invalid") and not result.ok
    tier1 = executor.call(
        session, "open_incident", {"diagnosis": "unknown", "summary": "x", "evidence": [evidence]}
    )
    assert tier1.status == "refused" and "ceiling 0" in (tier1.error or "")
    assert memory_world.backends.incidents.store.live() == []
    assert session.max_tier is Tier.READ


def test_guarded_tools_only_create_pending_requests(memory_world: World) -> None:
    session = memory_world.session()
    ev = ok_call(memory_world, session).call_id
    result = memory_world.executor.call(
        session, "freeze_pricing", {"scope": "*", "reason": "volatility", "evidence": [ev]}
    )
    assert result.ok and result.data["executed"] is False and result.data["status"] == "pending"
    assert memory_world.freezes.active() == []  # nothing happened yet
    assert not memory_world.freezes.is_frozen("api_requests")


def test_agent_cannot_approve_its_own_request(memory_world: World) -> None:
    session = memory_world.session()
    ev = ok_call(memory_world, session).call_id
    req = memory_world.executor.call(
        session, "freeze_pricing", {"scope": "*", "reason": "x", "evidence": [ev]}
    ).data["request_id"]
    for who in ("praxis-agent", "", "  "):
        with pytest.raises(RequestError):
            memory_world.desk.approve(req, who)
    assert memory_world.requests.get(req).status == "pending"  # type: ignore[union-attr]
    assert memory_world.freezes.active() == []


def test_database_refuses_self_approval_of_a_request(pg_world: World, pg_engine: Engine) -> None:
    session = pg_world.session()
    ev = ok_call(pg_world, session).call_id
    req = pg_world.executor.call(
        session, "freeze_pricing", {"scope": "*", "reason": "x", "evidence": [ev]}
    ).data["request_id"]
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_action_requests SET status = 'completed', decided_by = requested_by "
                "WHERE request_id = :i"
            ),
            {"i": req},
        )


def test_optimiser_constraints_cannot_be_overridden(memory_world: World) -> None:
    decision = recorded_change(memory_world)
    session = memory_world.session()
    sneaky = memory_world.executor.call(
        session, "run_pricing_counterfactual",
        {"decision_id": decision.decision_id, "price_micros": 1, "max_step": 0.9,
         "min_contribution_margin": 0},
    )  # fmt: skip
    assert sneaky.status == "invalid" and "extra_forbidden" in (sneaky.error or "")
    off_grid = memory_world.executor.call(
        session, "run_pricing_counterfactual",
        {"decision_id": decision.decision_id, "price_micros": 123_456_789},
    )  # fmt: skip
    assert off_grid.ok and off_grid.data["status"] == "not_evaluated"
    assert "cannot evaluate prices" in off_grid.data["why"]
    # the recorded decision is unchanged and still needs a human approval to execute
    assert memory_world.backends.decisions is not None
    assert memory_world.backends.decisions.get_record(decision.decision_id) == decision.record()


def test_agent_cannot_silently_alter_a_price(memory_world: World) -> None:
    decision = recorded_change(memory_world, Mode.RECOMMEND)
    session = memory_world.session()
    for tool in TOOLS:  # call every declared tool with whatever it accepts
        memory_world.executor.call(session, tool.name, {"decision_id": decision.decision_id})
    assert memory_world.backends.decisions is not None
    assert memory_world.backends.decisions.executions() == []
    with pytest.raises(NotExecutable, match="no approval"):
        memory_world.backends.decisions.execute(decision.decision_id, "someone")
    assert incomplete_calls(memory_world.audit.events()) == []


def test_action_budget_and_evidence_are_enforced(memory_world: World) -> None:
    session = memory_world.session()
    ungrounded = memory_world.executor.call(
        session, "open_incident",
        {"diagnosis": "unknown", "summary": "x", "evidence": ["call_000000000000_9"]},
    )  # fmt: skip
    assert ungrounded.status == "invalid" and "evidence must cite" in (ungrounded.error or "")
    ev = ok_call(memory_world, session).call_id
    statuses = []
    for diagnosis in ("unknown", "demand_spike", "model_drift"):
        r = memory_world.executor.call(
            session, "open_incident", {"diagnosis": diagnosis, "summary": "x", "evidence": [ev]}
        )
        statuses.append(r.status)
    assert statuses == ["ok", "ok", "refused"]  # max_actions = 2
    assert len(memory_world.backends.incidents.store.live()) == 2


def test_audit_unavailable_fails_closed(memory_world: World) -> None:
    class DownAudit:
        def append(self, event: object) -> None:
            raise ConnectionError("audit db down")

        def events(self, **_: object) -> list[object]:
            return []

    executor = ToolExecutor(TOOLS, memory_world.backends, DownAudit(), memory_world.executor.policy)  # type: ignore[arg-type]
    session = executor.new_session()
    result = executor.call(
        session, "open_incident", {"diagnosis": "unknown", "summary": "x", "evidence": []}
    )
    assert result.status == "audit_unavailable"
    assert memory_world.backends.incidents.store.live() == []


def test_frozen_product_cannot_execute_on_postgres(pg_world: World) -> None:
    decision = recorded_change(pg_world, Mode.EXECUTE)
    pg_world.freezes.activate("*", "drill", requested_by="praxis-agent", approved_by="owner")
    assert pg_world.backends.decisions is not None
    with pytest.raises(NotExecutable, match="database refused"):
        pg_world.backends.decisions.execute(decision.decision_id, "pricing-service")
    assert pg_world.backends.decisions.executions() == []
