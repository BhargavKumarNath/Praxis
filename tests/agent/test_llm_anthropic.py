"""Anthropic Messages client (ADR 0029 amendment 3): wire format, errors, pacing, no network.

The official SDK runs over an ``httpx2`` MockTransport, so the real request it builds is
checked, and its own retries are proven off (the provider owns retries and their accounting).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from praxis.agent.config import AgentPolicy
from praxis.agent.llm import (
    STEP_SCHEMA,
    AnthropicMessagesClient,
    LLMConfig,
    ModelError,
    StructuredLLMProvider,
    load_llm_config,
    parse_step,
    strict_schema,
)
from praxis.agent.wiring import provider_for

CFG = load_llm_config(provider="anthropic")
KEY = "sk-ant-test-secret"


def _tool_json() -> str:
    return json.dumps({"kind": "tool_call", "tool": "get_system_health", "args_json": "{}",
                       "rationale": "r"})  # fmt: skip


def _message(text: str, stop: str = "end_turn", **usage: int) -> dict[str, Any]:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": CFG.provider.model,
        "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                    {"type": "text", "text": text}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, **usage},
    }  # fmt: skip


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response], cfg: LLMConfig = CFG, **kw: Any
) -> AnthropicMessagesClient:
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicMessagesClient(cfg, KEY, http_client=http, **kw)


def _complete(client: AnthropicMessagesClient) -> Any:
    return client.complete(system="sys", prompt="p", schema=STEP_SCHEMA, max_output_tokens=512)


def test_request_shape_and_response_parsing() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_message(_tool_json(), cache_read_input_tokens=7))

    done = _complete(_client(handler))
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    auth = seen[0].headers  # SDK 2.x sends the API key as a bearer token
    assert auth.get("x-api-key") == KEY or auth.get("authorization") == f"Bearer {KEY}"
    assert "anthropic-version" in auth
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 512
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "p"}]
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"] == strict_schema(STEP_SCHEMA)
    assert body["output_config"]["effort"] == "high"
    assert "thinking" not in body and "temperature" not in body
    assert done.input_tokens == 107 and done.output_tokens == 40  # cache reads are input
    assert parse_step(done.text).tool == "get_system_health"  # type: ignore[union-attr]


def test_strict_schema_closes_every_object_and_leaves_the_original() -> None:
    strict = strict_schema(STEP_SCHEMA)
    assert all(b["additionalProperties"] is False for b in strict["anyOf"])
    assert all("additionalProperties" not in b for b in STEP_SCHEMA["anyOf"])
    assert [b["required"] for b in strict["anyOf"]] == [b["required"] for b in STEP_SCHEMA["anyOf"]]


def test_sdk_retries_are_off_and_the_server_delay_is_carried() -> None:
    calls: list[int] = []

    def handler(_r: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(
            429, headers={"retry-after": "5"},
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
        )  # fmt: skip

    with pytest.raises(ModelError) as err:
        _complete(_client(handler))
    assert len(calls) == 1  # one HTTP attempt: StructuredLLMProvider owns the retries
    assert err.value.retryable and err.value.retry_after_s == 5.0


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (529, "overloaded_error", True),
        (500, "api_error", True),
        (400, "invalid_request_error", False),
        (401, "authentication_error", False),
        (403, "permission_error", False),
        (404, "not_found_error", False),
    ],
)
def test_errors_are_classified(status: int, error_type: str, retryable: bool) -> None:
    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": error_type, "message": "m"}}
        )

    with pytest.raises(ModelError) as err:
        _complete(_client(handler))
    assert err.value.retryable is retryable
    assert KEY not in str(err.value)
    assert (err.value.retry_after_s is None) or retryable


def test_timeout_is_retryable_and_never_leaks_the_key() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ModelError) as err:
        _complete(_client(handler))
    assert err.value.retryable and str(err.value) == "timeout"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_a_refusal_or_cut_off_reply_is_not_retried(stop: str) -> None:
    client = _client(lambda _r: httpx2.Response(200, json=_message('{"kind": "tool', stop)))
    with pytest.raises(ModelError, match=stop) as err:
        _complete(client)
    assert not err.value.retryable


def test_calls_are_paced() -> None:
    now = [50.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    client = _client(lambda _r: httpx2.Response(200, json=_message(_tool_json())),
                     sleep=sleep, clock=lambda: now[0])  # fmt: skip
    _complete(client)
    now[0] += 1.0
    _complete(client)
    assert slept == [CFG.limits.min_interval_s - 1.0]


def test_client_requires_a_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        AnthropicMessagesClient(CFG, "")


def test_config_is_bounded_and_matches_its_provider() -> None:
    assert CFG.provider.name == "anthropic" and CFG.provider.endpoint.startswith("https://")
    assert CFG.limits.max_attempts <= 5 and CFG.limits.timeout_s <= 300
    assert CFG.provider.api_key_env == "PRAXIS_ANTHROPIC_API_KEY"
    assert load_llm_config().provider.name == "gemini"


def test_wiring_without_a_key_is_unavailable_and_with_one_builds_the_client(
    policy: AgentPolicy,
) -> None:
    missing = provider_for("anthropic", policy, api_key=None)
    assert isinstance(missing, StructuredLLMProvider) and missing.client is None
    built = provider_for("anthropic", policy, api_key=KEY)
    assert isinstance(built, StructuredLLMProvider)
    assert isinstance(built.client, AnthropicMessagesClient)
    assert built.name == "llm:anthropic:claude-opus-5-5"
