# Runbook: AI operator (agent) failure

Alerts: agent_provider_degraded

Exercised end to end by `tests/agent/test_runbook_agent_failure.py` (provider outage drill)
and `tests/resilience/test_agent_unavailable.py` (Phase 15: outage -> alert -> recovery).

## Trigger

- Alert `agent_provider_degraded` (ticket): more than 0.25 provider outages per investigation
  (`agent_provider_failures_total / agent_investigations_total`, at least 3 investigations).
  The CLI publishes its metrics as a snapshot after each investigation, so the ops monitor
  sees them (ADR 0020).
- Without an alert: an agent run ends with outcome `provider_unavailable`, `tool_errors` or
  `step_limit`, or ends `completed` with `grounded = false` (CLI exit code 1, session audit
  events, `agent_investigations_total{outcome}`).

Causes include a provider outage, quota or timeout, a backend the tools read being down, or
the agent being disabled.

## Impact

None on pricing, payments, recovery or MLOps. The core system does not import `praxis.agent`
(import-linter layer contract plus `tests/agent/test_availability.py`). Only investigation,
explanation and request creation are lost. When a provider is configured, the deterministic
triage fallback keeps investigations running (`[agent] fallback` in `configs/agent/policy.toml`).
The pinned policy runs triage as both the provider and the fallback. A live LLM (Anthropic or
Gemini, ADR 0029) runs only when a run selects it with `--provider`, and triage is then its
fallback.

## Diagnose

1. Read the run's audit trail with `python -m praxis.agent audit --session <session_id>`.
   Session rows show `started`, then `fallback` or `provider_unavailable` (with the provider
   name and the error type, never the error text), then the final outcome. Every tool call has
   a `received` and an `outcome` row.
2. Confirm the core flows are healthy through `GET /v1/ops/health`. The only agent signal
   there is `agent_provider_degraded`; no SLO depends on the agent.

## Mitigate

1. Disable the agent by setting `[agent] enabled = false` (a pinned config, so the change needs an
   ADR 0019 amendment and the owner's approval). Every run then ends `agent_disabled` without
   calling a tool. Operate through the CLIs and the other runbooks.
2. Never let a human or a script act as the agent to push a price. Use `pricing approve` /
   `execute` with an attributed approver. Guarded agent requests are completed only by
   `python -m praxis.agent approve <request_id> --approver <name>`, and the requester (the
   agent) can never be the approver.
3. Do not retry a failed high-impact tool call blindly. Inspect its audit record first
   (`audit --session`) and check the request's status with `python -m praxis.agent requests`.

## Verify

1. Core health is unchanged. `agent_provider_degraded` clears once outages fall to 0.25 or
   fewer per investigation (cumulative per process: a restarted agent process starts at 0).
2. Either the agent is restored (a run completes with `grounded = true`, with or without the
   fallback), or it is consciously left disabled with a ticket.

## Escalate

Escalate to the owner if an agent tool call is suspected to have bypassed a guardrail. Treat
that as a security incident: preserve `agent_audit_events` (append-only) and
`agent_action_requests`.
