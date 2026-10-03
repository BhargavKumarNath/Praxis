# ADR 0004: The LLM does not own pricing decisions

Status: Accepted (Phase 0)

## Context
Prices must be reproducible, constrained, auditable and explainable. LLM output is
non-deterministic and prompt-injectable.

## Decision
Statistical models predict, the constrained optimiser decides, deterministic guardrails
protect, and the LLM observes, investigates, explains and orchestrates through typed,
schema-validated, permission-checked and audited tools. Permissions are enforced outside
the prompt. The LLM cannot invent a price, bypass the optimiser or guardrails, run
shell or arbitrary SQL, read secrets, or silently deploy a model or change a live price.

## Consequences
* The core system runs normally when the LLM provider is unavailable.
* High-impact actions require approval in early releases.
* Prompt-injection content is treated as data, never authority.
