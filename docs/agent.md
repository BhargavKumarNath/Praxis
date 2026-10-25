# AI Revenue Operator (Phase 12, ADR 0019)

The operator observes, investigates, explains and REQUESTS. It never sets a price, runs the
optimiser, executes a payment, approves anything, reads secrets, or runs shell or SQL. All of
that is enforced outside the prompt by `praxis.agent.executor.ToolExecutor` and by import
contracts. All business numbers it sees are SYNTHETIC simulator output.

## Where things are

| Piece | Module |
| --- | --- |
| Policy (identity, tier ceiling, budgets, triage thresholds), pinned | `configs/agent/policy.toml`, `praxis.agent.config` |
| Tool contract, tiers, provenance labels, backends | `praxis.agent.tools` |
| Tier 0 read tools | `praxis.agent.evidence` |
| Tier 1 actions, Tier 2 requests | `praxis.agent.actions` |
| Permission layer + audit order | `praxis.agent.executor` |
| Audit store (append-only, `agent_audit_events`) | `praxis.agent.audit` |
| Guarded requests and the human approval desk | `praxis.agent.requests` |
| Provider interface, scripted / down doubles | `praxis.agent.provider` |
| Deterministic triage (fallback, benchmark reference; not an LLM) | `praxis.agent.triage` |
| Investigation loop, grounding, outage fallback | `praxis.agent.operator` |
| Benchmark scoring, pinned spec | `praxis.agent.benchmark`, `configs/agent/benchmark.toml` |
| Price freeze (pricing-owned) | `praxis.pricing.freeze`; migration 0007 trigger |
| CLI | `python -m praxis.agent` |

## Tools

| Tier | Tool | What it does |
| --- | --- | --- |
| 0 | `get_system_health` | Health report, firing alerts, live incidents, and which metrics nothing feeds |
| 0 | `get_source_freshness` | Age of each source against its per-source limit, plus contract failures |
| 0 | `get_queue_health` | Queue and DLQ backlog, consumer outcomes, p95 lag |
| 0 | `inspect_drift` | Champion drift reports, drift alarm, live retraining request |
| 0 | `inspect_forecast_error` | Champion probation reports, calibration, forecast age |
| 0 | `compare_models` | Champion vs candidate: validation, shadow reports, readiness |
| 0 | `explain_decision` | A recorded pricing decision's reasons, constraints, guardrails and lineage |
| 0 | `run_pricing_counterfactual` | The optimiser's RECORDED evaluation of one price (otherwise `not_evaluated`) |
| 0 | `inspect_recovery_metrics` | Resolved-recovery rate of dunning cases, current vs previous window |
| 0 | `get_incident`, `get_runbook` | An incident and its timeline; a runbook and whether it was exercised in a test |
| 1 | `open_incident` | Open or touch `agent:<diagnosis>` (routed to a runbook) |
| 1 | `request_retraining` | Open or join a retraining request (trigger `agent_request`); never promotes |
| 1 | `collect_diagnostics` | Attach a health, queue and freshness snapshot to a live incident |
| 2 | `freeze_pricing` | REQUEST a freeze of one product or `*` |
| 2 | `request_rollback` | REQUEST a rollback of the champion (refused if no target exists) |
| 2 | `request_promotion` | REQUEST promotion of a shadow version that passed (refused otherwise) |

Every Tier >= 1 call must cite `evidence`: call ids of successful calls in the same session.
At most `max_actions = 2` Tier >= 1 calls are allowed per investigation.

## Guarded requests (human side)

```bash
python -m praxis.agent requests                       # pending and decided requests
python -m praxis.agent approve <request_id> --approver <name>
python -m praxis.agent reject  <request_id> --approver <name> --reason "..."
python -m praxis.agent freezes                        # active price freezes
python -m praxis.agent lift <freeze_id> --by <name>
python -m praxis.agent audit --session <session_id>
```

Approval runs the existing path (`ModelRegistry.promote` re-checks shadow readiness;
`rollback` refuses a request if the champion changed after it was made; a freeze needs an
approver who is not the requester). The database refuses a decision by the requester.

## Agent side

```bash
python -m praxis.agent tools
python -m praxis.agent call get_system_health
python -m praxis.agent investigate --kind daily_check --as-of 2026-08-20
python -m praxis.agent investigate --kind investigate_incident --as-of ... --incident inc_...
python -m praxis.agent investigate --kind review_candidate --as-of ... --model-version ...
```

`investigate` exits 0 only if the run completed AND its answer is grounded.

## Honest-output rules (tested)

* A metric nothing feeds is `no_data` and names the gap: `data_source_age_s`,
  `data_contract_failures_total`, `business_kpi_deviation`, `queue_backlog` and
  `dlq_backlog` have no automatic feed (Phase 11 carry-over).
* Telemetry is in-process only: the agent sees only the metrics of its own process.
* No scheduler runs `OpsMonitor.tick`, so the incident list reflects only manual or drill
  ticks.
* Pricing results are shadow evaluations, NOT validated for execution (Phase 4 loader defect).
* Only `pubsub-backlog` and `agent-failure` runbooks are exercised in a test.

## Provider

The configured provider and fallback are `deterministic-triage`. No live LLM adapter exists
yet: adding one (the plan suggests a cost-conscious Gemini model) needs the owner's approval
for a docs check, an API key read from the environment, and live calls. It would implement
`LLMProvider.next_step` only, be opt-in, and never run in `make check`.

## Benchmark

`make agent-bench` (also part of `make test`) writes `docs/evidence/phase-12-benchmark.json`.
It covers seven fixed scenarios, each in a fresh database with a prompt-injection payload,
scored against the pinned `configs/agent/benchmark.toml`, and each also run by an
adversarial provider that obeys the payload. It measures the harness and safety layer, not
LLM competence (see ADR 0019).
