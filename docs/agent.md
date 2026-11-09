# AI operator

The operator observes, investigates, explains and requests. It does not set prices, run the
optimiser, execute payments, approve anything, read secrets, or run shell commands or SQL.
Those limits are enforced outside the prompt, by the tool executor and by import contracts, so
they hold whatever the model writes. Decisions: ADR 0019 (the operator) and ADR 0029 (the LLM
provider). Results: [results.md](results.md#ai-operator).

| Piece | Module |
| --- | --- |
| Policy: identity, tier ceiling, budgets, triage thresholds (pinned) | `configs/agent/policy.toml` |
| Tool contract, tiers, provenance labels | `praxis.agent.tools` |
| Read tools / actions and requests | `praxis.agent.evidence` / `praxis.agent.actions` |
| Permission layer and audit order | `praxis.agent.executor` |
| Append-only audit store | `praxis.agent.audit` |
| Guarded requests and the human approval desk | `praxis.agent.requests` |
| Deterministic triage (the fallback, not an LLM) | `praxis.agent.triage` |
| The procedure both triage and the LLM follow | `praxis.agent.playbook` |
| LLM protocol and provider clients | `praxis.agent.llm`, `configs/agent/llm*.toml` |
| Investigation loop, grounding check, outage fallback | `praxis.agent.operator` |

## Tools

| Tier | Tools | Effect |
| --- | --- | --- |
| 0 | `get_system_health`, `get_source_freshness`, `get_queue_health`, `inspect_drift`, `inspect_forecast_error`, `compare_models`, `explain_decision`, `run_pricing_counterfactual`, `inspect_recovery_metrics`, `get_incident`, `get_runbook` | Read only |
| 1 | `open_incident`, `request_retraining`, `collect_diagnostics` | Low-risk actions; retraining never promotes |
| 2 | `freeze_pricing`, `request_rollback`, `request_promotion` | File a request that a named human must approve |

Every tool has a typed schema, validates its arguments and writes an audit event. A tier 1 or 2
call must cite the ids of earlier successful calls in the same session as evidence, and an
investigation may make at most two of them. `run_pricing_counterfactual` returns the
optimiser's recorded evaluation of a price, or `not_evaluated`; it never computes a new one.

## The LLM

The provider proposes one step at a time as JSON against a strict schema. The executor decides
whether the step is allowed, and a grounding check rejects an answer that cites evidence it
never gathered. The system prompt carries the same operating procedure the deterministic triage
follows (`playbook.py`). If the provider is unavailable or returns an invalid step, the run falls
back to triage and the fallback is audited, so the core system never depends on an LLM.

Two providers are implemented: Anthropic (`configs/agent/llm-anthropic.toml`, Claude Opus 5.5)
and Gemini (`configs/agent/llm.toml`). The pinned policy still names `deterministic-triage` as
the provider, so an LLM runs only when it is asked for:

```bash
PRAXIS_ANTHROPIC_API_KEY=... python -m praxis.agent investigate --provider anthropic \
    --kind daily_check --as-of 2026-08-20
```

## Commands

```bash
python -m praxis.agent tools
python -m praxis.agent call get_system_health
python -m praxis.agent investigate --kind daily_check --as-of D
python -m praxis.agent investigate --kind investigate_incident --as-of D --incident inc_...
python -m praxis.agent investigate --kind review_candidate --as-of D --model-version V

python -m praxis.agent requests
python -m praxis.agent approve <request_id> --approver <name>
python -m praxis.agent reject <request_id> --approver <name> --reason "..."
python -m praxis.agent freezes
python -m praxis.agent lift <freeze_id> --by <name>
python -m praxis.agent audit --session <session_id>
```

`investigate` exits 0 only if the run completed and its answer is grounded. Approval runs the
existing path: the registry re-checks shadow readiness on a promotion, a rollback is refused if
the champion changed after the request, and the database refuses a decision by the requester.

## Benchmarks

- `make agent-bench` (part of `make test`) runs seven fixed incident scenarios, each in a fresh
  database with a prompt-injection payload, against the pinned `configs/agent/benchmark.toml`.
  Each scenario also runs with an adversarial provider that obeys the payload. It measures the
  harness and safety layer, using deterministic triage.
- `make agent-llm-bench LLM_PROVIDER=anthropic` is the opt-in live run. It needs a key, costs
  money and refuses to overwrite an earlier report. No test in `make check` calls a live LLM.

## Honest output

- Metrics come from the fleet view when a database is configured, and every result says
  whether it is fleet-wide or only the agent's own process, and which processes are stale.
- A metric without a value is `no_data` and names the feed that should supply it.
- `get_system_health` says whether the ops monitor is running, so an old incident list is not
  mistaken for current alerts.
- Pricing results are labelled as shadow evaluations, not validated for execution.
