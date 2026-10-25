# Runbook: AI operator (agent) failure

Alerts: none

## Trigger

Applies from Phase 12: the agent errors, times out, is rate-limited, or the LLM provider is unavailable. No alert exists yet because the agent does not exist yet.

## Impact

None on pricing, payments or MLOps: the core system runs without the LLM (ADR 0004). Only investigation, explanation and workflow orchestration are lost.

## Diagnose

1. Provider status and quota; agent audit events for the failed run (each tool call is audited).
2. Confirm the core flows are healthy through `GET /v1/ops/health`; an agent failure must not appear there as degraded service.

## Mitigate

1. Disable the agent entry point; operate through the CLIs and runbooks.
2. Never let a human or script impersonate the agent to push a price: use `pricing approve` / `execute` with an attributed approver.
3. Do not retry a failed high-impact tool call blindly; inspect its audit record first.

## Verify

1. Core health unchanged; agent restored or consciously left disabled with a ticket.

## Escalate

Owner if an agent tool call is suspected to have bypassed a guardrail (security incident).
