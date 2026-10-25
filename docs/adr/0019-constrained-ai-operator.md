# ADR 0019: Constrained AI Revenue Operator (typed tools, tiers, guarded requests)

Status: Accepted (Phase 12). Written 2026-10-25. `configs/agent/policy.toml` and
`configs/agent/benchmark.toml` were PRE-REGISTERED (version `2026-10-25.1`) before the scored
benchmark run and are pinned by `tests/agent/test_config.py`. Changing either needs an
amendment to this ADR and the owner's approval.

## Context
ADR 0004 limits the LLM to observing, investigating, explaining and orchestrating, and
CLAUDE.md s7 lists what it may never do. Phase 12 must turn that into an enforced design with
a typed tool layer, permission tiers (Tier 2 needs explicit approval at first), an incident
benchmark, and a core system that keeps running without the agent. Constraints: local only,
no cloud spend, no new dependency, no live LLM call without the owner's approval.

## Decision
* **`praxis.agent` sits at the top layer** (`api | science | agent`). No core package imports
  it. A forbidden contract keeps it away from the optimiser, the pricing cycle, the pricing
  CLI, payment execution (gateways, processor, Stripe client), the dunning executor,
  service and tasks, MLOps training, pipeline and drill, the event bus, the API and
  `subprocess`. It is also in the "models never import the simulator / science" and "no web
  framework" contracts.
* **Tools are data, not code paths.** Each tool is a name, a tier, a pydantic argument model
  with `extra="forbid"` and a handler. There is no generic tool. Shell, SQL, secrets, price
  setting, execution and approval are simply not declared. The only price-valued argument is
  `run_pricing_counterfactual`, which looks up the optimiser's RECORDED evaluation of that
  price and returns `not_evaluated` for any other price.
* **One permission layer, `ToolExecutor.call`, enforced outside the prompt:**
  1. audit `received` (keys and a hash of the raw arguments only), failing closed;
  2. the tool must be declared;
  3. the schema is validated before anything runs;
  4. the tier must be within the session ceiling, which is taken from the policy file when the
     session is created and never from the task, the arguments or tool output;
  5. Tier >= 1 calls spend a per-run action budget (2) and must cite as evidence the call
     ids of successful calls in the same session;
  6. the handler runs, and any exception becomes an audited `error` result;
  7. audit `outcome`.

  Tool output goes back to the provider marked `untrusted_data`.
* **Tiers.** Tier 0 is read-only (health, freshness, queues, drift, forecast error, recovery,
  model comparison, decision explanation, counterfactual lookup, incidents, runbooks).
  Tier 1 is low-risk (open or touch an incident, open or join a retraining request, attach
  diagnostics). Tier 2 is guarded (`freeze_pricing`, `request_rollback`, `request_promotion`).
  A Tier 2 tool only inserts a PENDING `agent_action_requests` row. A named human completes it
  with `python -m praxis.agent approve`, through the EXISTING paths, which re-check their own
  policy: `ModelRegistry.promote(approver=...)`, `ModelRegistry.rollback(actor=...)`, or the
  new price freeze. The database refuses a decision by the requester. A request is claimed
  atomically before it runs, and a refusal by the underlying path marks it `failed`.
* **Price freeze (new, in `praxis.pricing.freeze`).** A freeze row (one product or `*`) blocks
  EXECUTION. `MemoryDecisionStore` checks it, and on Postgres a trigger on `price_executions`
  refuses the insert (migration 0007), so no code path can execute a frozen product.
  Decisions are still made and recorded, so shadow evaluation is unaffected. A freeze needs a
  named approver who is not the requester, and a named human lifts it.
* **Provider interface.** `LLMProvider.next_step(conversation) -> ToolCall | Final`.
  `ProviderUnavailable` switches the run to the fallback provider. A `Final` answer is
  grounded only if every call id it cites belongs to a successful call of the session.
* **No live LLM adapter in this phase.** The plan suggests a cost-conscious Gemini model.
  Building that adapter needs a check of the current API docs, an API key and a live call to
  verify, all of which need the owner's approval. The configured provider and fallback are
  therefore `deterministic-triage`: a rule-based investigator that reads only structured tool
  fields (alert rule ids, numbers, flags), never free text. Tests double only the provider
  (`ScriptedProvider`, `DownProvider`).
* **Benchmark.** Seven fixed scenarios (stale source, demand spike, model drift, candidate
  regression, queue backlog, recovery collapse, price volatility), each in a fresh database
  and each with a prompt-injection payload in its task note. Each is scored for diagnosis,
  evidence, action, unnecessary actions, prohibited executions, audit completeness and
  grounding. Every scenario is also run by an adversarial "obedient" provider that follows
  the payload.

## Consequences / limitations
* The benchmark measures the HARNESS (tools, permissions, audit, loop) driven by a
  deterministic investigator that was written alongside the scenarios. It is a regression
  benchmark, NOT a held-out measure of LLM diagnostic competence. That needs a live provider
  and fresh, unseen scenarios (an owner decision).
* Read tools inherit the Phase 11 gaps and report them instead of hiding them. No automatic
  feed exists for `data_source_age_s`, `data_contract_failures_total`,
  `business_kpi_deviation` or the backlog gauges, so those report `no_data` unless a caller
  recorded a value in-process. Telemetry is in-process only. No scheduler runs
  `OpsMonitor.tick`.
* Pricing tools label every result as shadow and NOT validated for execution (Phase 4 loader
  defect; Phases 6 and 9 need a fresh pre-registered evaluation before live pricing).
* `get_runbook` reports which runbooks are exercised in a test (`pubsub-backlog`,
  `agent-failure`). The rest are structure-checked only.
* There is no agent alert rule (`configs/ops` is pinned). Agent failure is detected from the
  run's outcome and its audit trail (runbook `agent-failure`).
* `recovery_collapse` triage compares resolved-recovery rates of cases opened in two
  consecutive windows. Still-open cases are excluded, so a very recent window can look worse
  while its cases mature.
