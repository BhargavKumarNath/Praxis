"""LLM provider contract (ADR 0029): strict output, bounded failure, harness keeps control.

No test calls a live model. The Gemini wire format is checked against an ``httpx``
MockTransport; the provider loop against scripted clients; the operator runs with the real
executor, permission layer and audit.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx
import pytest

from praxis.agent.config import AgentPolicy, load_agent_policy
from praxis.agent.executor import ToolExecutor
from praxis.agent.llm import (
    ACTIONS,
    DIAGNOSES,
    PROTOCOL,
    STEP_SCHEMA,
    Completion,
    EchoTriageClient,
    GeminiInteractionsClient,
    InvalidModelOutput,
    LLMConfig,
    ModelError,
    StructuredLLMProvider,
    load_llm_config,
    parse_step,
    render_prompt,
    retry_delay_s,
)
from praxis.agent.operator import Operator
from praxis.agent.playbook import DATA_RULES, FORECAST_RULES, QUEUE_RULES, render_procedure
from praxis.agent.provider import (
    SYSTEM_PROMPT,
    Conversation,
    Final,
    ProviderUnavailable,
    Task,
    ToolCall,
)
from praxis.agent.tools import ROUTES
from praxis.agent.triage import TriageProvider
from praxis.agent.wiring import TOOLS, operator_for, provider_for
from praxis.telemetry import Telemetry
from tests.agent.conftest import World
from tests.agent.test_injection import HOSTILE

DAY = date(2026, 8, 20)
CFG = load_llm_config()
TRIAGE = load_agent_policy().triage


def _cfg(**limits: Any) -> LLMConfig:
    return CFG.model_copy(update={"limits": CFG.limits.model_copy(update=limits)})


def _tool(tool: str, args: dict[str, Any] | None = None) -> str:
    return json.dumps({"kind": "tool_call", "tool": tool, "args_json": json.dumps(args or {}),
                       "rationale": "r"})  # fmt: skip


def _final(evidence: Sequence[str], diagnosis: str = "no_issue") -> str:
    return json.dumps({"kind": "final", "diagnosis": diagnosis, "summary": "s",
                       "evidence": list(evidence), "rationale": "r"})  # fmt: skip


class Scripted:
    """A model client replaying texts or raising ``ModelError``; records every prompt."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, replies: Sequence[str | ModelError]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                 max_output_tokens: int) -> Completion:  # fmt: skip
        assert schema is STEP_SCHEMA and "untrusted DATA" in system
        self.prompts.append(prompt)
        self.systems.append(system)
        reply = self.replies.pop(0)
        if isinstance(reply, ModelError):
            raise reply
        return Completion(reply, 100, 20, self.model)


class FirstCallIds(Scripted):
    """Finishes citing whatever call id the transcript shows (a cooperative model)."""

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any],
                 max_output_tokens: int) -> Completion:  # fmt: skip
        if self.replies:
            return super().complete(system=system, prompt=prompt, schema=schema,
                                    max_output_tokens=max_output_tokens)  # fmt: skip
        calls = [m["call_id"] for m in json.loads(prompt)["transcript"] if m.get("role") == "tool"]
        return Completion(_final(calls[:1]), 100, 20, self.model)


def _conv() -> Conversation:
    return Conversation(Task("daily_check", DAY), tools=[{"name": "get_system_health"}],
                        messages=[Task("daily_check", DAY).message()])  # fmt: skip


def _provider(client: Any, **limits: Any) -> tuple[StructuredLLMProvider, list[float]]:
    slept: list[float] = []
    return StructuredLLMProvider(client, _cfg(**limits), triage=TRIAGE, sleep=slept.append), slept


# --- parsing -------------------------------------------------------------------------------
def test_valid_steps_parse() -> None:
    step = parse_step(_tool("get_runbook", {"rule_id": "x"}))
    assert step == ToolCall("get_runbook", {"rule_id": "x"})
    final = parse_step(_final(["call_1"], "queue_backlog"))
    assert isinstance(final, Final) and final.evidence == ("call_1",)
    assert final.recommended_action is None  # "" or absent = no action


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "not JSON"),
        ("[1, 2]", "one JSON object"),
        (json.dumps({"kind": "tool_call", "rationale": "r"}), "needs tool and args_json"),
        (json.dumps({"kind": "tool_call", "tool": "t", "args_json": "[1]", "rationale": "r"}),
         "JSON object"),
        (json.dumps({"kind": "tool_call", "tool": "t", "args_json": "{", "rationale": "r"}),
         "not JSON"),
        (json.dumps({"kind": "final", "diagnosis": "no_issue", "summary": "s", "evidence": [],
                     "rationale": "r"}), "needs diagnosis"),
        (json.dumps({"kind": "final", "diagnosis": "root_access", "summary": "s",
                     "evidence": ["c"], "rationale": "r"}), "diagnosis"),
        (json.dumps({"kind": "exec", "rationale": "r"}), "kind"),
        (json.dumps({"kind": "final", "diagnosis": "no_issue", "summary": "s",
                     "evidence": ["c"], "rationale": "r", "approved_by": "owner"}), "approved_by"),
    ],
)  # fmt: skip
def test_invalid_outputs_are_rejected_with_a_reason(text: str, reason: str) -> None:
    with pytest.raises(InvalidModelOutput, match=reason):
        parse_step(text)


def _branch(kind: str) -> dict[str, Any]:
    return next(b for b in STEP_SCHEMA["anyOf"] if b["properties"]["kind"]["enum"] == [kind])


def test_schema_enumerates_the_allowed_diagnoses() -> None:
    assert _branch("final")["properties"]["diagnosis"]["enum"] == list(DIAGNOSES)


def test_recommended_action_names_an_action_tool_or_nothing() -> None:
    """ADR 0029 am. 4: free text here cost 7 repairs in live attempt 4."""
    enum = _branch("final")["properties"]["recommended_action"]["enum"]
    assert enum == list(ACTIONS) and enum[0] == ""
    assert set(enum[1:]) == {t["name"] for t in (t.schema() for t in TOOLS) if t["tier"] >= 1}
    full = json.loads(_final(["c1"]))
    for ok in ("", "open_incident", None):
        assert isinstance(parse_step(json.dumps({**full, "recommended_action": ok})), Final)
    with pytest.raises(InvalidModelOutput):
        parse_step(json.dumps({**full, "recommended_action": "Open an incident and page ops"}))


def test_the_system_prompt_carries_the_playbook() -> None:
    """The LLM gets the procedure the fallback runs: every runbook route, every alert rule
    the triage reads, and the policy's thresholds."""
    provider, _ = _provider(Scripted([]))
    system = provider.system
    assert system.startswith(SYSTEM_PROMPT) and system.endswith(PROTOCOL)
    procedure = render_procedure(TRIAGE)
    assert procedure in system
    for diagnosis, (_, runbook) in ROUTES.items():
        if diagnosis != "unknown":
            assert f"{diagnosis} -> {runbook}" in procedure
    for rule in DATA_RULES | QUEUE_RULES | FORECAST_RULES | {"model_drift", "price_volatility"}:
        assert rule in procedure
    assert f"window_days = {TRIAGE.recovery_window_days}" in procedure
    assert f"at least {TRIAGE.recovery_min_resolved} resolved" in procedure
    assert f"{TRIAGE.recovery_collapse_ratio} x the previous" in procedure
    for diagnosis in DIAGNOSES:
        assert diagnosis in procedure


def test_the_provider_sends_its_system_prompt() -> None:
    client = Scripted([_tool("get_system_health")])
    provider, _ = _provider(client)
    provider.next_step(_conv())
    assert client.systems == [provider.system]


def test_each_schema_branch_requires_what_the_validator_requires() -> None:
    """ADR 0029 am. 1: a schema-valid reply that omits a field ``ModelStep`` needs is a
    protocol bug (live attempt 1: replies were only ``kind`` + ``rationale``)."""
    assert len(STEP_SCHEMA["anyOf"]) == 2
    tool, final = _branch("tool_call"), _branch("final")
    assert set(tool["required"]) == {"kind", "tool", "args_json", "rationale"}
    assert set(final["required"]) == {"kind", "diagnosis", "summary", "evidence", "rationale"}
    for branch in (tool, final):
        assert set(branch["required"]) <= set(branch["properties"])
        assert branch["type"] == "object"
    # dropping any one required field from a valid reply makes ModelStep reject it
    for branch, text in ((tool, _tool("get_system_health")), (final, _final(["c1"]))):
        full = json.loads(text)
        parse_step(text)
        for name in set(branch["required"]) - {"kind"}:
            with pytest.raises(InvalidModelOutput):
                parse_step(json.dumps({k: v for k, v in full.items() if k != name}))


# --- the provider loop ---------------------------------------------------------------------
def test_an_invalid_reply_is_repaired_with_the_error_and_counted() -> None:
    client = Scripted(["oops", _tool("get_system_health")])
    provider, _ = _provider(client)
    assert provider.next_step(_conv()) == ToolCall("get_system_health", {})
    assert provider.usage.repairs == 1 and provider.usage.calls == 2
    assert json.loads(client.prompts[1])["previous_reply_rejected"].startswith("reply is not JSON")
    assert json.loads(client.prompts[0])["previous_reply_rejected"] is None


def test_repairs_are_bounded() -> None:
    provider, _ = _provider(Scripted(["x", "y", "z"]), max_repairs=2)
    with pytest.raises(ProviderUnavailable, match="invalid after repairs"):
        provider.next_step(_conv())
    assert provider.usage.calls == 3


def test_transient_errors_retry_with_exponential_backoff() -> None:
    client = Scripted(
        [
            ModelError("timeout", retryable=True),
            ModelError("http 429", retryable=True),
            _tool("get_system_health"),
        ]
    )
    provider, slept = _provider(client, max_attempts=3, backoff_s=0.5)
    assert provider.next_step(_conv()) == ToolCall("get_system_health", {})
    assert slept == [0.5, 1.0] and provider.usage.retries == 2
    assert provider.usage.errors == ["timeout", "http 429"]


def test_a_server_requested_delay_is_honoured_and_capped() -> None:
    client = Scripted(
        [
            ModelError("http 429", retryable=True, retry_after_s=7.0),
            ModelError("http 429", retryable=True, retry_after_s=500.0),
            _tool("get_system_health"),
        ]
    )
    provider, slept = _provider(client, max_attempts=3, backoff_s=0.5, max_retry_delay_s=60.0)
    assert provider.next_step(_conv()) == ToolCall("get_system_health", {})
    assert slept == [7.0, 60.0]


def test_retries_are_bounded() -> None:
    provider, slept = _provider(
        Scripted([ModelError("timeout", retryable=True)] * 3), max_attempts=3
    )
    with pytest.raises(ProviderUnavailable, match="timeout"):
        provider.next_step(_conv())
    assert len(slept) == 2


def test_a_non_retryable_error_fails_at_once() -> None:
    provider, slept = _provider(Scripted([ModelError("http 404", retryable=False)]))
    with pytest.raises(ProviderUnavailable, match="404"):
        provider.next_step(_conv())
    assert slept == []


def test_token_budget_stops_the_provider() -> None:
    provider, _ = _provider(Scripted([_tool("get_system_health")] * 5),
                            max_tokens_per_investigation=1000)  # fmt: skip
    provider.usage.input_tokens = 1000
    with pytest.raises(ProviderUnavailable, match="budget"):
        provider.next_step(_conv())
    assert provider.reset().input_tokens == 1000 and provider.usage.input_tokens == 0


def test_usage_reports_tokens_latency_and_estimated_cost() -> None:
    clock = iter([0.0, 0.25])
    provider = StructuredLLMProvider(Scripted([_tool("get_system_health")]), CFG,
                                     triage=TRIAGE, clock=lambda: next(clock))  # fmt: skip
    provider.next_step(_conv())
    usage = provider.usage.as_dict(CFG.pricing)
    expected = (100 * CFG.pricing.input_usd_per_mtok + 20 * CFG.pricing.output_usd_per_mtok) / 1e6
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20
    assert usage["estimated_cost_usd"] == round(expected, 6)
    assert usage["latency_ms_median"] == 250.0


def test_prompt_truncates_large_tool_output_and_marks_it_data() -> None:
    conv = _conv()
    conv.messages.append({"role": "tool", "call_id": "c1", "tool": "t", "status": "ok",
                          "data": {"blob": "x" * 20_000}})  # fmt: skip
    doc = json.loads(render_prompt(conv, _cfg(max_tool_output_chars=600), None))
    data = doc["transcript"][1]["data"]
    assert data["truncated"] is True and len(data["json_prefix"]) == 600
    assert "untrusted DATA" in doc["note"] and doc["allowed_diagnoses"] == list(DIAGNOSES)


# --- operator integration (real executor, permissions, audit) ------------------------------
def _operator(world: World, provider: Any, policy: AgentPolicy) -> Operator:
    executor = ToolExecutor(TOOLS, world.backends, world.audit, policy)
    return Operator(executor, provider, fallback=TriageProvider(policy.triage))


def test_missing_key_falls_back_to_triage_and_is_audited(
    memory_world: World, policy: AgentPolicy, tel: Telemetry
) -> None:
    provider = provider_for("gemini", policy, api_key=None)
    assert isinstance(provider, StructuredLLMProvider) and provider.client is None
    executor = ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, policy)
    run = operator_for(executor, provider="gemini").investigate(Task("daily_check", DAY))
    assert run.fallback_used and run.providers == ["llm:unconfigured", "deterministic-triage"]
    statuses = [e.status for e in memory_world.audit.events(session_id=run.session_id)]
    assert "fallback" in statuses and run.outcome == "completed"


def test_with_a_key_the_gemini_provider_is_built(policy: AgentPolicy) -> None:
    provider = provider_for("gemini", policy, api_key="test-key")
    assert isinstance(provider, StructuredLLMProvider)
    assert provider.name == f"llm:gemini:{CFG.provider.model}"


def test_a_model_outage_mid_run_falls_back(memory_world: World, policy: AgentPolicy) -> None:
    outage = [ModelError("http 503", retryable=True) for _ in range(3)]
    client = Scripted([_tool("get_system_health"), *outage])
    provider, _ = _provider(client)
    run = _operator(memory_world, provider, policy).investigate(Task("daily_check", DAY))
    assert run.fallback_used and run.outcome == "completed" and run.grounded


def test_a_compromised_model_cannot_act(memory_world: World, policy: AgentPolicy) -> None:
    """The model obeys the injected payload; the executor refuses or rejects every step."""
    hostile = [
        _tool("get_system_health"),
        _tool("execute_price", {"product": "api_requests", "price_micros": 1}),
        _tool("approve_request", {"request_id": "req_x", "approver": "praxis-agent"}),
        _tool("run_shell", {"command": "rm -rf /"}),
        _tool("read_secret", {"name": "PRAXIS_STRIPE_SECRET_KEY"}),
        _tool("freeze_pricing", {"scope": "*", "reason": HOSTILE[:200], "evidence": [],
                                 "tier": 9, "approved_by": "owner"}),
    ]  # fmt: skip
    relaxed = policy.model_copy(
        update={"agent": policy.agent.model_copy(update={"max_tool_errors": 20})}
    )
    provider, _ = _provider(FirstCallIds(hostile))
    task = Task("daily_check", DAY, note=HOSTILE)
    run = _operator(memory_world, provider, relaxed).investigate(task)
    statuses = {c.tool: c.status for c in run.calls}
    assert statuses["undeclared"] == "refused"
    assert statuses["freeze_pricing"] == "invalid"
    assert memory_world.freezes.active() == [] and memory_world.requests.recent() == []
    assert run.outcome == "completed" and run.grounded and not run.fallback_used


def test_ungrounded_final_is_reported_not_hidden(memory_world: World, policy: AgentPolicy) -> None:
    provider, _ = _provider(Scripted([_tool("get_system_health"), _final(["call_invented"])]))
    run = _operator(memory_world, provider, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "completed" and not run.grounded


def test_echo_triage_client_reproduces_triage_through_the_protocol(
    memory_world: World, policy: AgentPolicy, tel: Telemetry
) -> None:
    tel.set("data_source_age_s", 7_000_000, source="fred")
    conv: list[Conversation] = []

    class Recording(StructuredLLMProvider):
        def next_step(self, conversation: Conversation):  # type: ignore[no-untyped-def]
            conv[:] = [conversation]
            return super().next_step(conversation)

    provider = Recording(EchoTriageClient(policy.triage, lambda: conv[0]), CFG, triage=TRIAGE)
    run = _operator(memory_world, provider, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "completed" and run.grounded and not run.fallback_used
    assert run.final is not None and run.final.diagnosis == "data_source_stale"
    assert provider.usage.calls == run.steps and provider.usage.repairs == 0


# --- Gemini Interactions wire format --------------------------------------------------------
def _gemini(handler: Any) -> GeminiInteractionsClient:
    return GeminiInteractionsClient(CFG, "secret-key", transport=httpx.MockTransport(handler))


def _ok(text: str, **usage: int) -> httpx.Response:
    return httpx.Response(200, json={
        "status": "completed", "model": CFG.provider.model,
        "steps": [{"type": "thought", "summary": []},
                  {"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_input_tokens": 50, "total_output_tokens": 10,
                  "total_thought_tokens": 5, **usage},
    })  # fmt: skip


def test_gemini_request_shape_and_response_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(_tool("get_system_health"))

    done = _gemini(handler).complete(system="sys", prompt="p", schema=STEP_SCHEMA,
                                     max_output_tokens=256)  # fmt: skip
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == CFG.provider.endpoint
    assert seen[0].headers["x-goog-api-key"] == "secret-key"
    assert seen[0].headers["Api-Revision"] == CFG.provider.api_revision
    assert body["store"] is False and body["model"] == CFG.provider.model
    assert body["response_format"]["schema"] is not None
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["system_instruction"] == "sys" and body["input"] == "p"
    assert body["generation_config"]["max_output_tokens"] == 256
    assert done.input_tokens == 50 and done.output_tokens == 15  # thinking billed as output
    assert parse_step(done.text) == ToolCall("get_system_health", {})


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (httpx.Response(429), True),
        (httpx.Response(503), True),
        (httpx.Response(400), False),
        (httpx.Response(401), False),
        (httpx.Response(404, json={"error": "model not found"}), False),
        (httpx.Response(200, content=b"<html>"), True),
        (httpx.Response(200, json={"status": "failed"}), True),
    ],
)
def test_gemini_errors_are_classified(response: httpx.Response, retryable: bool) -> None:
    client = _gemini(lambda _r: response)
    with pytest.raises(ModelError) as err:
        client.complete(system="s", prompt="p", schema=STEP_SCHEMA, max_output_tokens=64)
    assert err.value.retryable is retryable


_RETRY_INFO = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "23s"},
]}}  # fmt: skip


@pytest.mark.parametrize(
    ("response", "delay"),
    [
        (httpx.Response(429, json=_RETRY_INFO), 23.0),
        (httpx.Response(429, headers={"Retry-After": "5"}), 5.0),
        (httpx.Response(429), None),
        (httpx.Response(429, content=b"<html>"), None),
        (httpx.Response(429, json={"error": {"details": "x"}}), None),
        (httpx.Response(429, json={"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "soon"}]}}), None),
        (httpx.Response(429, json=[1]), None),
    ],
)  # fmt: skip
def test_retry_delay_is_read_from_the_response(
    response: httpx.Response, delay: float | None
) -> None:
    assert retry_delay_s(response) == delay


def test_gemini_429_carries_the_server_delay_but_a_400_does_not() -> None:
    for status, expected in ((429, 23.0), (400, None)):
        client = _gemini(lambda _r, s=status: httpx.Response(s, json=_RETRY_INFO))
        with pytest.raises(ModelError) as err:
            client.complete(system="s", prompt="p", schema=STEP_SCHEMA, max_output_tokens=64)
        assert err.value.retry_after_s == expected


def test_gemini_client_paces_calls() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    client = GeminiInteractionsClient(
        _cfg(min_interval_s=6.0), "secret-key",
        transport=httpx.MockTransport(lambda _r: _ok(_tool("get_system_health"))),
        sleep=sleep, clock=lambda: now[0],
    )  # fmt: skip
    for advance in (0.0, 2.0, 10.0):
        now[0] += advance
        client.complete(system="s", prompt="p", schema=STEP_SCHEMA, max_output_tokens=64)
    assert slept == [4.0]  # 1st call free; 2nd comes 2 s later, waits 4 s; 3rd is 10 s on


def test_gemini_timeout_is_retryable_and_never_leaks_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ModelError) as err:
        _gemini(handler).complete(system="s", prompt="p", schema=STEP_SCHEMA, max_output_tokens=64)
    assert err.value.retryable and "secret-key" not in str(err.value)


def test_gemini_client_requires_a_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        GeminiInteractionsClient(CFG, "")


def test_config_is_bounded() -> None:
    assert CFG.limits.max_attempts <= 5 and CFG.limits.timeout_s <= 60
    assert CFG.provider.endpoint.startswith("https://")
