# Decision console (Phase 13)

The console shows what Praxis is doing and lets a named person decide the few things that need
a person. It reads real state only: the control plane (Postgres), the warehouse (DuckDB) and
the fleet metrics (ADR 0020). Every figure is SYNTHETIC simulator output. Architecture and
the admin boundary: ADR 0021. Visual design: `docs/design/dashboard.md`.

## Run it

```sh
make pg-up
# optional: a throwaway demo database built through real code paths (development seed 1)
make console-demo
PRAXIS_DATABASE_URL=postgresql+psycopg://praxis@127.0.0.1:55432/praxis_console_demo \
PRAXIS_WAREHOUSE_PATH=data/pricing/seed1-c1000/warehouse.duckdb \
PRAXIS_CONSOLE_APPROVERS="alice=<token of 24+ characters>" \
make console-api           # http://127.0.0.1:8001/v1/console/*
make console-web           # http://127.0.0.1:3000, proxies /api/console/* to the API
```

Without `PRAXIS_CONSOLE_APPROVERS` the console is read-only (admin routes answer 503). Make a
token with `python -c "import secrets; print(secrets.token_urlsafe(24))"` and keep it out of
the repository.

`make console-demo` drops and rebuilds `praxis_console_demo`:

1. the STAGED journey (`tests/e2e/seed_journey.py`): a pricing snapshot with
   `price_change_fraction` 0.8 (limit 0.50) -> the ops monitor opens `price_volatility` -> the
   deterministic triage requests a freeze. The snapshot is published by the seed, not by a
   pricing run;
2. `run-local` (300 customers, 28 days, seed 1): entities, invoices, ledger;
3. 42 dunning journeys on the SyntheticPaymentGateway (`tests/e2e/seed_dunning.py`, the
   `tests/dunning/test_e2e.py` path; a fixed one in three customers fixes the card);
4. the 8 seed-1 shadow pricing cycles (`python -m praxis.pricing decide --mode shadow`);
5. the seed-1 forecaster registered, validated and FORCE-promoted (an audited override by
   actor `console-demo`; the demo has no weeks of shadow days). The ML Ops view shows it as
   forced.

## Views

| View | Question | Reads |
| --- | --- | --- |
| Executive | Is the business on track? | warehouse marts: revenue, gross margin, demand, active customers, churn, recovery, list price index; each against the median of its previous 28 days |
| Pricing | What would the optimiser change? | `pricing_decisions`, `price_executions`; decision detail: candidates with p05..p95, guardrail checks, constraints, lineage, model versions, record SHA-256 |
| Payments | Are failed payments recovering? | `dunning_cases`, `dunning_decisions`, `retry_jobs`, `invoices` |
| ML operations | Can the models be trusted? | `model_versions`, `model_stage_events`, `model_validation_reports`, `model_monitoring_reports`, `retraining_requests`; serving gauges from the fleet view |
| System | Is the platform healthy? | fleet view (`SystemReader`), `build_health`, `dead_letters`, `raw.ingest_batches` + freshness, `configs/console/cost.toml` |
| AI operator | What needs a decision? | `ops_incidents`, agent audit and requests, `pricing_freezes`, `configs/agent/policy.toml` |

## Reading the states

| You see | It means |
| --- | --- |
| SYNTHETIC | Simulator output. Not a real business result. |
| EXTERNAL / CONFIG | Data from an external source (with its batch provenance) or a repository config file. |
| SHADOW | A pricing decision recorded in shadow mode. Not validated for execution (Phase 4 loader defect; Phases 6 and 9 need a fresh pre-registered evaluation before live pricing). |
| NO DATA + reason | The input does not exist yet, for example nothing publishes `queue_backlog` on the Pub/Sub emulator. Never shown as 0. |
| UNAVAILABLE + reason | The store did not answer, or a warehouse table is not built. |
| STALE 3h 12m | A source or process past its limit, or a view whose last good read is over 60 s old or whose refresh failed. The old numbers stay visible, dimmed. |
| N days behind | An executive figure whose latest day is older than the newest figure on the page. |
| Error + correlation id | A failed call. Quote the id when searching the API's structured logs. |

## Deciding

1. Open **AI operator** and paste your approver token. The API answers with your name. The
   browser keeps the token for this tab only.
2. A pending request shows the rationale, the evidence call ids and the investigation trace
   from the audit log.
3. **Approve** runs the existing path: `ApprovalDesk` -> `FreezeStore` (freeze) or
   `ModelRegistry` (rollback, promotion). **Reject** needs a reason. A request that is no
   longer pending is refused (409).
4. **Lift freeze** on an active freeze records your name.

The console cannot set a price, run a payment, promote a model on its own, or bypass the
optimiser or a guardrail. Those actions have no route.

## Tests

* `tests/console/` (pytest, real Postgres and DuckDB): every view's panels, missing and stale
  inputs, provenance, decision detail, model versions, safe errors, the full admin path.
* `frontend/src/test/` (Vitest + Testing Library, `make frontend-check`): stale indicator,
  synthetic, external and shadow labels, no data and measured zero, decision detail, model
  versions, the error state with correlation id, the approver path, and a hygiene check that
  app code imports no fixture and reaches the network only through `lib/api.ts`.
* `frontend/e2e/journey.spec.ts` (Playwright, `make e2e`, CI job `e2e`): alerting signal ->
  incident -> investigation trace -> pending request -> refused token -> authorized approval
  -> active freeze -> a second approval refused.

## Known limits

* Fleet metrics are latest values only; the console draws no history from them.
* `queue_backlog` has no source on the Pub/Sub emulator, so it shows NO DATA there.
* The investigator is deterministic triage, not an LLM. No live LLM provider is configured
  (owner decision, ADR 0019).
* Approver tokens are static configuration with no expiry or rotation.
* The cost panel is an estimate from `configs/console/cost.toml`. No billing data is read.
