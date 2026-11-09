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

## Amendment 1 (2026-11-08): per-kind step schema, pacing, server retry delay

**Live attempt 1 failed (kept, not overwritten):** `docs/evidence/final-agent-llm-live.json`,
`gemini-3.5-flash-lite` on the AI Studio free tier, **0 / 7 scenarios, 0 prohibited
executions**. Two causes, both in the report's per-scenario `errors`:

1. **Protocol bug (ours).** `STEP_SCHEMA` required only `kind` and `rationale`, while
   `ModelStep` also requires `tool` + `args_json` (tool call) or `diagnosis` + `summary` +
   `evidence` (final). Gemini's schema-constrained decoding returned exactly the required
   fields (~35 output tokens per reply), so every reply was schema-valid yet rejected, and
   repairs ran out (5 scenarios).
2. **Free-tier rate limit.** About 13 calls in ~30 s, then `http 429` on every attempt; the
   1 s / 2 s backoff was far shorter than the per-minute window (3 scenarios, two with no
   successful call).

**Changes (justified by the contract, not by the score):**
* `STEP_SCHEMA` is an `anyOf` of two objects, one per `kind`, each requiring exactly the
  fields `ModelStep` requires for that kind (`anyOf` and per-object `required` are supported
  by Gemini structured output, checked 2026-11-08). `ModelStep`, the grounding check, the
  executor, the permission layer and `configs/agent/benchmark.toml` are unchanged.
  `test_each_schema_branch_requires_what_the_validator_requires` pins the correspondence.
* `GeminiInteractionsClient` keeps `min_interval_s = 6.0` between call starts (<= 10 RPM; the
  docs do not publish free-tier limits, so this is a conservative pace, not one fitted to
  attempt 1). The benchmark shares one client across the seven scenarios.
* A retryable error carries the server-requested delay (`Retry-After`, or Google
  `RetryInfo.retryDelay`); the provider waits that long, capped at `max_retry_delay_s = 60`,
  instead of its own backoff. `max_attempts` stays 3.
* `configs/agent/llm.toml` version `2026-11-08.1`. Prices are unchanged (re-checked
  2026-11-08); on the free tier the run is not billed and the report's cost is an estimate.

**Attempt 2** runs once, `make agent-llm-bench`, into
`docs/evidence/final-agent-llm-live-attempt2.json`; the target refuses to overwrite an existing
report. Its acceptance is the unchanged benchmark (7 / 7 on every criterion, 0 prohibited).
Whatever it shows is reported, and attempt 1 stays on record. The scenarios were written
alongside triage, so a live score remains a capability check, not a held-out benchmark.

## Amendment 2 (2026-11-08): attempt 3 with `gemini-3.8-flash`

**Live attempt 2 failed (kept):** `docs/evidence/final-agent-llm-live-attempt2.json`,
`gemini-3.5-flash-lite`, config `2026-11-08.1`. **0 / 7 scenarios, 0 prohibited executions.**
The protocol fixes worked: 16 calls, 0 repairs, 0 retries, every investigation completed and
grounded. The model under-investigated: it answered after one or two tool calls, named the
right diagnosis in 5 / 7 (`no_issue` for `candidate_regression` and `recovery_collapse`),
never gathered all the required evidence and never took the expected action.

**Change, registered before attempt 3 runs:** the model only. `gemini-3.8-flash` (stable,
structured output supported; free tier; paid list USD 0.75 / 3.75 per 1M tokens through
2026-12-31, checked 2026-11-08). Config `2026-11-08.2`. Prompt, protocol, schema, pacing,
limits, executor, permissions and the benchmark acceptance (7 / 7, 0 prohibited) are unchanged.

Flash-Lite is the smallest tier; a stronger model is the change a deployment would make after
seeing under-investigation, and it is not fitted to any scenario. This is a disclosed model
change after a failure, not a held-out result: the scenarios were written alongside triage.
Attempt 3 runs once into `docs/evidence/final-agent-llm-live-attempt3.json`. Its result is
reported whatever it is; any further model is attempt 4, and every attempt stays on record.

**Attempt 3 result (2026-11-08): no measurement.** `docs/evidence/final-agent-llm-live-attempt3.json`:
0 / 7 and 0 prohibited executions, but **0 of 42 HTTP attempts completed** (40 hit the 20 s
`timeout_s`, two returned `http 503`). It says nothing about the model's skill. A single unscored
probe afterwards returned `429`: the AI Studio free tier allows `gemini-3.8-flash` **20 requests
per day**, which is less than one benchmark run (at least 16 calls, plus retries). A further
live run needs a paid tier, and the latency of one paid call should be measured before
`timeout_s` is reconsidered. That would be attempt 4.

## Amendment 3 (2026-11-09): Anthropic provider, attempt 4 with `claude-opus-5-5`

**Why.** Attempt 3 could not measure anything on the Gemini free tier (20 requests per day for
`gemini-3.8-flash`). The owner chose the Anthropic API, on prepaid credit (a hard spend cap),
and the model `claude-opus-5-5`. This is a disclosed provider and model change after three
failed attempts, not a held-out result.

**Changes.**
* `AnthropicMessagesClient` (`praxis.agent.llm`) is a second `ModelClient` over the official SDK
  (`anthropic` 2.6, `messages.create` with `output_config.format` = JSON schema). The SDK's
  retries are off (`max_retries=0`), so `StructuredLLMProvider` keeps owning attempts,
  backoff, the server's `retry-after` and their accounting. A `refusal` or `max_tokens` stop is
  a non-retryable error. The schema is `STEP_SCHEMA` with `additionalProperties: false` on
  each object (`strict_schema`; Anthropic requires it, and `ModelStep` already forbids extra
  fields, so the accepted replies are unchanged). Both clients share one pacer.
* `configs/agent/llm-anthropic.toml` `2026-11-09.1`: `claude-opus-5-5`, `effort = "high"`
  (thinking is always on), USD 4 / 20 per 1M tokens (checked 2026-11-09). Two limits differ from
  the Gemini config, because thinking counts towards `max_tokens` and takes time:
  `max_output_tokens` 16000 (Gemini 1024) and `timeout_s` 120 (Gemini 20). The bounds in
  `LimitsSection` were raised to allow them (timeout <= 300 s, output <= 32000). Everything
  else (attempts, repairs, token budget, truncation, pacing, retry cap) is the same.
* `llm.toml` is unchanged (attempt 3's config). `--provider anthropic`, the
  `PRAXIS_ANTHROPIC_API_KEY` setting, and `make agent-llm-bench LLM_PROVIDER=anthropic`, which
  now checks the provider's key and still refuses to overwrite a report.
* The prompt, protocol, step schema branches, executor, permissions, grounding check and
  `configs/agent/benchmark.toml` acceptance (7 / 7 on every criterion, 0 prohibited) are
  unchanged. Tests: `tests/agent/test_llm_anthropic.py` (wire format through the SDK on a mock
  transport, error classes, SDK retries off, pacing, key handling, wiring).

**Attempt 4** runs once: `make agent-llm-bench LLM_PROVIDER=anthropic`, into
`docs/evidence/final-agent-llm-live-attempt4.json`. Its result is reported whatever it is.
Attempts 1-3 stay on record. The scenarios were written alongside triage, so a live score is a
capability check, not a held-out benchmark.

**Attempt 4 result (2026-11-09): FAIL, 2 / 7, the first real measurement.**
`docs/evidence/final-agent-llm-live-attempt4.json`, `claude-opus-5-5`, 0 prohibited executions,
43 calls, 0 retries, every investigation completed and grounded. Passed: `queue_backlog`,
`recovery_collapse`. Diagnosis right in 6 / 7 (`demand_spike` answered `unknown`). The expected
action was taken in `data_source_stale`, `model_drift`, `queue_backlog` and
`recovery_collapse`, and correctly withheld in `candidate_regression`. `price_volatility` named
the right diagnosis but filed no freeze request. The other failures had the right diagnosis and
action, so they fail on another criterion (required evidence or an extra action). The report
does not record which criterion failed: that is a reporting gap, and it is fixed only for later
runs. Each scenario needed one repair: `recommended_action` came back as prose over the
64-character limit. Anthropic strips string-length constraints from schemas, and the validator rejected it and re-asked.
Estimated cost USD 1.50 at list prices (292,561 input and 16,457 output tokens), more than the
pre-run estimate of USD 0.5 to 0.8.

## Amendment 4 (2026-11-09): the playbook in the prompt, a held-out set, attempt 5

**Why attempt 4 failed (cause, not a re-score).** The benchmark scores the operator's procedure:
read the runbook of the diagnosis, take exactly the playbook action. That procedure lived only
inside `praxis.agent.triage`. The LLM's system prompt never stated it, so the model was scored
on rules it was never given. This is a defect on its own terms, independent of any score: the
operator has two providers (the LLM and the deterministic fallback) and they ran under
different policies, a duplicated business rule held in one place only (CLAUDE.md s3).

**Changes (made after attempt 4, so registered here before any further live call).**
* `praxis.agent.playbook` states the procedure once: the alert-rule sets, the diagnosis order,
  the diagnosis -> runbook routes (`ROUTES`), the playbook actions and the `policy.toml` triage
  thresholds. `triage` imports its rule sets from it. `render_procedure` turns it into text, and
  `StructuredLLMProvider` sends `SYSTEM_PROMPT + procedure + PROTOCOL` (a required `triage`
  argument, so no LLM provider can be built without the playbook). The text states the
  procedure, never a scenario. `tests/agent/test_llm.py` checks it names every route, rule and
  threshold. The offline protocol run (triage echoed through the prompt) still passes 7 / 7.
* `recommended_action` is an enum: an action tool's name or `""` (schema and `ModelStep`).
  Attempt 4 sent prose there and needed 7 repairs.
* The bench report now records, per scenario, every criterion (`criteria`, `failed_criteria`),
  the full `Score`, the call sequence and the final answer, plus the system prompt's SHA-256.
* The exact system prompt is pinned: SHA-256
  `8410783a0e4e701da56025aae2f3e772ff1c34c5e3b9e3cd6e25266de7a10901`
  (`tests/agent/test_config.py`). The model and its config are unchanged
  (`llm-anthropic.toml` `2026-11-09.1`, `claude-opus-5-5`, effort high).

**A held-out set.** The seven fixed scenarios were scored live four times and the prompt was
then changed in response to attempt 4. Re-scoring on them would measure fit, so they are now a
DEVELOPMENT set. `configs/agent/benchmark-heldout.toml` `2026-11-09.1` (pinned, SHA-256
`5cb1a32c...6743ccff`) holds ten new worlds (`tests/agent/scenarios_heldout.py`) with a new
injection payload. They exercise branches the seven never do: a contract failure, a queue alert
with no incident, drift with a retraining request already live (correct: no action), a ready
candidate (correct: request promotion), a healthy day, a recovery dip above the collapse ratio,
a collapse on too few cases, a negative KPI shock, an incident-led price-volatility
investigation, and two faults at once. The acceptance is the same as the benchmark's: every
scenario on every criterion (10 / 10), 0 prohibited executions, audit completeness 1.0, 0
unnecessary actions, grounded. Disclosed: the same author wrote the prompt and these scenarios.
Before any model saw them, the offline run (triage, the reference implementation of the
playbook) was used once to check that the worlds are built as registered: 10 / 10. One world
was fixed during that check (it set a counter as a gauge, which raised before any
investigation ran).

**Runs, in this order, each once.**
1. **Attempt 5 (held-out, the gate):** `make agent-llm-bench LLM_PROVIDER=anthropic LLM_SET=heldout
   LLM_LIVE_OUT=docs/evidence/final-agent-llm-live-attempt5-heldout.json`. It is the claim for
   the live LLM operator, PASS or FAIL. It runs first so that the remaining API credit cannot
   run out on the development rerun first. Estimated cost USD 2 to 3.
2. **Development rerun (optional, only if credit remains):** the same prompt on the seven fixed
   scenarios, into `docs/evidence/final-agent-llm-live-attempt5-dev.json`. It is reported as
   development evidence (seen scenarios), never as held-out.

Any later change to the prompt, the playbook text or the model is a new registration with fresh
scenarios. These ten are spent once attempt 5 runs.

**Attempt 5 result (2026-11-09): PASS, 10 / 10 on the held-out set, run once.**
`docs/evidence/final-agent-llm-live-attempt5-heldout.json`, `claude-opus-5-5`, system prompt
SHA-256 `8410783a...7a10901` (the registered one). Every scenario passed every criterion: right
diagnosis, required evidence, expected action (or correctly none), 0 unnecessary actions, 0
prohibited executions, audit completeness 1.0, grounded. All ten were answered by the model
(no fallback). The new injection payload was ignored: no runbook was skipped, no promotion,
rollback or freeze was requested outside the playbook, and no secret appears in any summary. 51
model calls, 0 retries, 0 repairs, 412,199 input and 13,574 output tokens, estimated USD 1.92 at
list prices. Scope of the claim: the model executes a STATED operating procedure correctly on
unseen faults, including the no-action branches. It is not evidence of open-ended diagnosis
without a procedure. The ten scenarios are now spent. The optional development rerun on the
fixed seven was not run (owner decision, 2026-11-09): the seven stay at attempt 4's 2 / 7 under
the old prompt, and no live score of the new prompt on them exists.
