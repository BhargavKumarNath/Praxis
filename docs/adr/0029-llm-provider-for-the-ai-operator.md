# ADR 0029: LLM provider for the AI operator (structured steps, harness keeps control)

Status: Accepted (2026-11-05, owner-delegated: "choose the strongest option"). Amends ADR 0019
("No live LLM adapter in this phase"). Evidence: `docs/evidence/final-agent-llm-protocol.json`
(offline); live evidence pending an API key (`make agent-llm-bench`).

## Context
ADR 0019 built the operator's harness (typed tools, permission tiers, audit, grounding,
pending requests for a named human) and drove it only with a deterministic triage
investigator. The plan names a cost-conscious Gemini model. The deployment already has an
`llm-api-key` secret readable only by the agent identity (ADR 0024), with no version.

## Options
1. Provider-native tool calling (Gemini `function_call` steps, Anthropic `tool_use`).
2. **One JSON step per turn with a response schema**, validated by our own pydantic model,
   behind a `ModelClient` protocol (chosen).
3. An SDK framework (LangChain etc.).

Native tool calling differs per provider and per API generation: Google's docs now steer new
code from `generateContent` to the Interactions API, with different field names. Option 2
needs only "JSON matching this schema", which every serious provider supports, keeps the
validator and the repair loop ours, and makes the offline mode exact. Option 3 adds a heavy
dependency and hides the loop we must audit.

## Decision
* `praxis.agent.llm.StructuredLLMProvider` implements `LLMProvider`. A turn sends the task,
  the session's tool catalogue (tier-filtered by the executor) and the transcript (tool output
  truncated, labelled untrusted DATA). It asks for one step:
  `{"kind": "tool_call", "tool", "args_json"}` or `{"kind": "final", "diagnosis" (enum),
  "summary", "evidence" (call ids)}`. `ModelStep` is strict (`extra = forbid`). An extra
  field such as `approved_by` makes the reply invalid.
* **The model never acts.** A valid tool call goes to the existing `ToolExecutor`, which
  checks permissions from `configs/agent/policy.toml` and audits the call: an undeclared tool
  is refused, a Tier 2 tool only files a pending request, and extra arguments are invalid. A
  final answer is graded by the operator's grounding check.
* **Bounded failure.** Each HTTP call has a 20 s timeout. There are 3 attempts with
  exponential backoff on timeouts, 408/409/429/5xx and non-completed interactions. A 400,
  401, 403 or 404 (bad request, auth, unknown model) is not retried. Up to 2 repairs are made
  for invalid replies, each re-asked with the validation error. A token budget of 120K per
  investigation applies. Each failure ends in `ProviderUnavailable`, so the operator switches
  to deterministic triage and audits the `fallback`. A missing key behaves the same way.
* **Provider:** Gemini Interactions API (`POST /v1beta/interactions`, `x-goog-api-key`,
  `Api-Revision: 2026-05-20`, `store: false`, `response_format` with the JSON schema). Model:
  `gemini-3.5-flash-lite`, the current Flash-Lite at USD 0.30 / 2.50 per 1M input / output
  tokens, checked 2026-11-05. A different provider means writing one more `ModelClient`.
* **Accounting.** Calls, retries, repairs, input and output tokens (thinking counted as
  output), latency and an estimated cost from the configured list prices. These go to the CLI
  (`investigate --provider gemini`, `llm_usage`) and to telemetry: `agent_llm_tokens_total`,
  `agent_llm_errors_total` and `agent_llm_invalid_outputs_total`.
* **Offline mode.** `EchoTriageClient` returns triage's steps serialised as model JSON, so
  the whole protocol runs in CI with no network. It is labelled a contract double, not an LLM.
* The pinned policy keeps `provider = "deterministic-triage"`. Switching the configured
  provider is an owner action once a key exists. Per run, `--provider gemini` selects the
  LLM.

## Evaluation
* `tests/agent/test_llm.py` covers malformed JSON, non-objects, missing fields, a bad
  diagnosis enum, non-object args, extra authority fields, repair and repair bounds, retry,
  backoff and retry bounds, non-retryable errors, the budget, cost accounting, prompt
  truncation, the missing-key fallback (audited), a mid-run outage fallback, a compromised
  model following an injection (refused, invalid, no side effects), ungrounded answers, and
  the Gemini wire format and error classes (MockTransport).
* `tests/agent/test_llm_bench.py` runs the seven pre-registered incident scenarios
  (`configs/agent/benchmark.toml`, unchanged) through the protocol. Offline it gives 7 / 7,
  with 0 repairs, 0 fallbacks and 0 prohibited executions. Its token figures are character
  estimates.
* **Not yet measured:** a live model's diagnostic skill, latency and real token cost. Run
  `make agent-llm-bench` with `PRAXIS_GEMINI_API_KEY`, which costs about USD 0.05 at the
  configured prices. The scenarios were written alongside triage, so a live score is a
  capability check, not a held-out benchmark.

## Consequences
* The core system never imports `praxis.agent`, so an LLM outage cannot stop pricing, billing,
  recovery or MLOps (`tests/agent/test_availability.py`).
* Prompts carry synthetic operational data only: no secrets (tools cannot read them) and no
  real customer data.
