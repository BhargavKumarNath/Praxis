Start Phase 15 (Resilience, Chaos, Security and Cost Hardening). Goal: failure handling becomes
part of the demonstrated system, with every injected failure measured and recorded. Phases 0-14
and the Phase 11 wiring are done. Do not reopen them. Phase 14 closed with 100K PASS and 1M not
run. The 1M run is blocked by the DuckDB primary-key index memory and stays a carry-over: do not
work on it in this phase.

Read first, and only what you need:
- CLAUDE.md (s4 boundaries, s5 Stripe safety, s7 agent rules, s13 API rules, s14 cost, s16
  testing, s17 security, s18 dependencies, s21 source freshness, s22 stop conditions, s24
  contracts).
- project_plan.md, the Phase 15 section (failure injection, security, cost, gate).
- required_test.md: s21 (Phase 15 tests, including the per-failure recovery record) and s23
  (evidence template).
- The "Next" and "Carry-over" sections at the end of project_progress.md.
- What already exists, so you extend it rather than rebuild it: `make event-chaos` and the
  Phase 3 fault plans (docs/event-backbone.md); the Phase 11 alerts, incidents and runbooks
  (docs/runbooks/, configs/ops/, `make ops-check`); the Phase 12 agent availability tests;
  docs/cost-guard.md; infra/terraform/; `make nightly-security`; .trivyignore; ADRs 0018-0022.

Before coding, reply with a short plan (max ~35 lines):
- a failure matrix: one row per required_test.md s21 failure, plus the plan's extras (service
  restart, failed candidate, Stripe duplicate event). For each row give the injection point,
  the expected behaviour, how detection is measured (alert / metric / incident), the recovery
  path, and how data loss and duplicate side effects are checked. Mark which rows are already
  covered by an existing test and what is missing;
- pass criteria for each failure (detection time bound, zero data loss, zero duplicate side
  effects, recovery to a correct oracle state), written to a config (e.g.
  `configs/resilience/failures.toml`) and pinned by a test BEFORE any failure run;
- the security review: least privilege (Terraform IAM), no public admin or destructive
  endpoint (API and console route inventory), secret isolation (no secrets in logs, traces,
  reports or the repo), webhook signature verification, audit history, dependency scanning,
  Terraform review;
- the cost work: monthly cost estimate for demonstration mode against the GBP 0-5 target,
  resource-cleanup command, scale-to-zero and max-instance caps (verified in Terraform /
  `terraform plan`), BigQuery query controls (max bytes billed, dry runs), and budget alerts
  (state plainly that a budget alert is not a spending cap);
- whether "infrastructure can be destroyed / recreated" can be shown without cost (validate /
  plan against a local or emulated target), and what would need my approval;
- every new dependency or tool (prefer pinned Docker images), justified under s18;
- the ADR(s) you will write and anything that needs my approval.
Then proceed without waiting unless something needs approval.

## Method

1. Run `make audit` first.
2. Build the failure suite as tests against real boundaries: Postgres, DuckDB, the Pub/Sub
   emulator, SyntheticPaymentGateway, and the agent with `ScriptedProvider` or a failing
   provider. Inject at the boundary (timeouts, malformed payloads, crashes, restarts, missing
   artifacts, stale features, bursts); never mock the code under test. No arbitrary sleeps as
   synchronisation.
3. For every injected failure record, in a machine-readable report: expected behaviour,
   actual behaviour, detection time, recovery behaviour, data-loss status and
   duplicate-side-effect status (s21). A failure that is not detected, or recovers wrongly, is
   a FAIL and gets reported with its cause. Do not reword the expectation afterwards.
4. Every alert a failure should raise must already exist in configs/ops/ with a complete
   runbook; add a missing one in the same change (`make ops-check`). The final runbook set
   must be complete.
5. Event burst: reuse the Phase 14 harness (`make scale-run`, a development step) or
   event-chaos, at a size that runs in minutes. Seeds: development seed 1, or fresh seeds from
   801 upward recorded in the config. Never a held-out seed listed in CLAUDE.md s24. Fault
   seed 701 belongs to the Phase 14 evidence.
6. Security findings: fix what is in scope; anything accepted goes in .trivyignore (or the
   equivalent) with a reason and an expiry. The known gitleaks false positive
   (tests/payments/test_ids_and_model.py:44) is an owner decision: propose an allowlist entry
   with justification and do not apply it without my approval. Never rewrite git history.
7. Check current official docs (s21) before relying on GCP pricing, quotas, Cloud Run, Pub/Sub,
   BigQuery or Stripe behaviour in the cost estimate or the security review.

## Rules (CLAUDE.md integrity and git rules apply in full)

- Local only, GBP 0. No `terraform apply`, no cloud resource, no BigQuery query and no Stripe
  call without my approval and a cost estimate. Never load test Stripe Sandbox or a public API.
- Never lower a threshold, floor, SLO or pass criterion after seeing a result. Never skip,
  deselect or weaken a test to pass. Never add `continue-on-error` or `|| true` to a gate.
- Coverage: any new critical module (failure detection, recovery or guard logic) needs a 95%
  floor in `[tool.praxis.coverage_floors]` in the same change. Never lower a floor.
- No new `# noqa ... complexity-debt` markers. New packages are placed in a layer in
  pyproject.toml in the same change.
- Keep `docs/checkpoints/phase-15-checkpoint.md` current. Tell me before any run longer than
  about 15 minutes, with expected duration and memory.
- Run `make nightly-security` and `make nightly-perf` before finishing and report them. Run
  `make check` once at the end, in the background; it must exit 0.

## Finish with

- `docs/evidence/phase-15.md` in the s23 template: the failure matrix with the s21 recovery
  record for every failure, the security review with findings and their resolution, the cost
  estimate with sources and dates, the destroy / recreate evidence (or what was not shown and
  why), and the runbook inventory;
- the machine-readable reports (`docs/evidence/phase-15-*.json`);
- ADR(s); updates to docs/architecture.md, docs/cost-guard.md, docs/runbooks/, CLAUDE.md s24
  (any new gates or targets) and project_progress.md;
- a concise report: what changed, results per failure, security and cost findings, open
  decisions, and a plain statement of whether the gate passed (failure tests pass; no critical
  dependency vulnerabilities; infrastructure can be destroyed / recreated; demonstration mode
  within budget; runbook set complete), with its limits. Never commit or push.
