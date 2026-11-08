# Final release report: final engineering completion run (2026-11-05 to 2026-11-08)

**Kinds of evidence.** Every result below is labelled with what it is:
- **SYNTHETIC:** simulator worlds and their GBP figures. None of it is real revenue or real
  customers.
- **LOCAL:** measured on one Linux host (16 threads, 14.4 GiB).
- **EMULATED-M1:** a Linux cgroup budget standing in for an 8 GB Apple M1. Not an M1.
- **HISTORICAL:** an earlier phase's evidence, cited and not re-run.
- **MOCKED:** fake providers or doubles.
- **SANDBOX:** the Stripe test mode.
- **CLOUD-VERIFIED:** observed on the deployed `praxis-dev-510522`.

Nothing was committed or pushed.

## 1. What was implemented

| Area | Change | Evidence |
| --- | --- | --- |
| Warehouse | `raw.sim_events` has no primary-key index. Writes append to `raw.sim_events_landing`, and a date-scoped anti-join compaction moves first occurrences in. The streaming and loader paths are proven equal with Hypothesis. **Amendment 1:** streaming writers compact every 50,000 rows and flush before reads | ADR 0028, migration `0002`, `tests/data/test_event_insert_paths.py` |
| Simulator memory | Day sections render from columns. The event stream is byte-identical on 4 worlds, and at most 10 products are allowed | `src/praxis/simulator/engine.py`, unchanged golden hashes |
| Scale harness | cgroup memory accounting; warehouse rows equal archive with 0 duplicate ids (always gated); a lean streaming oracle that refuses out-of-order events; an incremental snapshot checksum | `src/praxis/streaming/scale.py`, `src/praxis/control/store.py` |
| M1-budget ladder | `make scale-m1` (a 4 GB no-swap scope on 8 CPUs, plus Postgres at 1 GB). After the 500K deviation it re-applies and checks the Postgres caps | `Makefile`, `configs/scale/slo.toml` |
| AI operator LLM provider | Gemini Interactions client with a strict JSON schema per step, pydantic validation, up to 2 repairs, 3 attempts with backoff, a 20 s timeout, a token budget, cost accounting, and an audited fallback to deterministic triage. An offline echo mode runs in CI | ADR 0029, `src/praxis/agent/llm.py`, `configs/agent/llm.toml` |
| Retrain window | A sliding 364-day training and backtest window for the live loop, so cost becomes constant | ADR 0030, `configs/mlops/demand.toml` |
| Pricing evaluation | A fresh pre-registered held-out evaluation (seeds 901-905), development sensitivity, and a written selection rule | ADR 0031, `configs/pricing/evaluation_2026_11.toml` |
| Console tokens | Approver tokens carry an expiry (`name=token@YYYY-MM-DD`), plus a rotation script | ADR 0021 amendment 1, `scripts/console_token.py` |
| Terraform gates | `tf-check` / `tf-test` use their own data directory, so a local deploy's GCS backend can no longer break the credential-free mocked gates | `Makefile`, `.gitignore` |
| Dependencies | oauthlib 4.0.0 upgraded to 4.0.1 (CVE-2026-49272, found by `make audit` during this run) | `uv.lock` |

## 2. Architectural decisions and why

- **ADR 0028: no index plus compaction**, rather than date partitions, a bigger host or
  BigQuery.
  - The Phase 14 1M blocker was the in-memory index (about 180 MB per million rows). Removing
    it bounds memory by the compaction batch, not the table.
  - It keeps the single-file DuckDB design.
  - It costs GBP 0, where BigQuery at this scale would need cost approval.
  - **Amendment 1** came from a measured regression (s4).
- **ADR 0029: Gemini through a structured-step protocol.**
  - The LLM proposes one typed step at a time. The harness keeps permissions, validation and
    the decision to execute, and any failure falls back to deterministic triage.
  - So the core never depends on the provider (CLAUDE.md s7).
  - Native tool calling was rejected because it moves control into the provider's loop.
- **ADR 0030: a sliding 364-day window**, rather than capping backtest origins or adding CPU.
  Only a window makes both cost and memory constant. 52 whole weeks keeps the weekly
  seasonality.
- **ADR 0031: a fresh registration before any world was built.** The seeds, hypotheses and
  scoring were pinned by a test first, then each world was run once. A development candidate
  that loosened a guardrail was excluded and disclosed.
- **ADR 0021 amendment 1: expiring tokens.** It closes the "static token" gap without adding
  an identity provider to a single-operator system.

## 3. Maximum validated scale and measured resources (LOCAL, EMULATED-M1, SYNTHETIC)

Details: [final-scale.md](final-scale.md).

| Step | Events | Throughput | Op p99 | Peak memory | Checks |
| --- | --- | --- | --- | --- | --- |
| m1-100k | 9.74M | 5,141 ev/s | 13.3 ms | scope 3,794 / 4,096 MB, 0 OOM | **PASS 12/12** |
| m1-250k | 24.3M | 5,146 ev/s | 12.2 ms | scope at cap (page cache), 0 OOM | **PASS 12/12** |
| m1-500k | 48.7M | 4,370 ev/s | 16.5 ms | scope at cap (page cache), 0 OOM; shard RSS max 662 MB | **PASS 12/12, with a deviation** |
| m1-1m | - | - | - | - | **NOT RUN** (owner decision 2026-11-06) |

- **Largest step that passed:** 500K, which is 48.7M events replayed to the exact oracle state,
  with the ledger equal to the oracle and warehouse rows equal to the archive, under 10 %
  duplicates, reordering and consumer crashes.
- **The deviation:** Postgres swap was uncapped after a host reboot, and 14.5 MB was swapped.
  **Largest step fully inside the registered budget: 250K.**
- **Storage:** about 2.4 GB per 100K customer-months (archive, warehouse and Postgres).
- **Not shown:** M1 hardware, Pub/Sub, Cloud Run or BigQuery at scale, 1M, or repeat runs.

## 4. Performance improvements (LOCAL)

| What | Before | After | Source |
| --- | --- | --- | --- |
| 100K replay, same final state | 4,494.6 ev/s on 9.0 cores, host peak 10,959 MB (Phase 14, HISTORICAL) | 5,141.2 ev/s on 3.7 cores, host peak 8,101 MB, inside a 4 GB scope | final-scale.md s5 |
| Largest correct replay | 100K (1M index OOM at 4.9-6.1M rows per shard) | 500K | same |
| W1 simulate + oracle at 500K | 2,128 MB peak | 1,038 MB peak, identical archive and checksum | same, s3 |
| W1 at 100K | 559 MB | 307 MB (development measurement) | ADR 0028 |
| 1K chaos benchmark (`nightly-perf`) | Phase 16: 1,800.9 ev/s | **First run after ADR 0028: 575 ev/s, FAIL** (floor 1,218). After amendment 1: **1,541.9 ev/s, PASS**. That is about 14 % below Phase 16, and the baseline was **not** re-recorded | s8, ADR 0028 am. 1 |

The chaos benchmark regression is real and is disclosed as such. The final code is slower than
Phase 16 on small streaming batches, because landing and compaction cost more than the old
indexed lookup did at small sizes. It is far better at scale.

## 5. AI operator capabilities and evaluation

**Capabilities (unchanged contract, ADR 0019).** The operator observes, retrieves evidence,
investigates and explains, and files guarded requests that a named human approves in the
console. It cannot reach the optimiser, payments, a shell or SQL (import-linter contract, 8
contracts kept).

| Evaluation | Kind | Result |
| --- | --- | --- |
| Fixed incident benchmark, deterministic triage | LOCAL, re-run 2026-11-08 | **7/7** scenarios, 0 prohibited executions, 28 adversarial attempts refused ([final-agent-benchmark.json](final-agent-benchmark.json)) |
| The same 7 scenarios through the LLM JSON protocol (echo client) | MOCKED provider, LOCAL | **7/7**, 0 repairs, 0 fallbacks, 0 prohibited. 40 calls, about 152K input tokens by character estimate ([final-agent-llm-protocol.json](final-agent-llm-protocol.json)) |
| Live Gemini model | - | **NOT RUN.** There is no API key on this machine. An owner run (`make agent-llm-bench`) costs about USD 0.05 |

**Caveat:** the protocol run tests the integration (prompt, schema, parsing, accounting,
fallback), **not the competence of an LLM**. No model has been scored.

## 6. Pricing evaluation results (SYNTHETIC)

Registered in ADR 0031 and run once each. **Seeds 901-905 are now spent.**

| Hypothesis | Result | Verdict |
| --- | --- | --- |
| H1: every world passes the per-world acceptance | 901 failed on direction (0.75); 905 failed on no-harm and direction | **FAIL** |
| H2: mean true delta > 0 with 95 % CI lower bound > 0 | +34.3 GBP/day, CI [-113.9, +203.9] | **FAIL** |
| H3: at least 3 of 5 worlds change a price | 3/5 | PASS |

- Per world (GBP/day): 901 +63.5, 902 +318.7, 903 0, 904 0, **905 -210.9**.
- 0 constraint violations.
- The method captured 171 of the 2,099 GBP/day oracle value.
- **Cause of 905:** +4 % raises on `data_transfer_gb` with churn under-predicted.
- **Conclusion:** the optimiser is safe against its hard constraints but has **not** been
  shown to be profitable. Live pricing stays off.

Details: [final-pricing-evaluation.md](final-pricing-evaluation.md).

## 7. Security and reliability improvements

- **Console approver tokens.**
  - They now expire, and a rotation script exists (ADR 0021 amendment 1).
  - **The deployed secret is in the old format.** It must be rotated before the next deploy,
    or the new console will not start. This is an owner action.
- **Console proxy, CLOUD-VERIFIED read-only on 2026-11-05.**
  - Reads return 200, and requests with no token or a wrong token return 401.
  - The approver token returns `{"approver":"owner"}`.
  - No write was made ([final-console-proxy.md](final-console-proxy.md)).
- **Dependencies.**
  - A new CVE in oauthlib was found and fixed (4.0.1).
  - pip-audit and npm audit are clean.
- **Secrets.**
  - gitleaks over the full history found 0 leaks (31 commits).
  - gitleaks over all 77 uncommitted files found 0 leaks.
  - trivy found 0 Terraform misconfigurations (MEDIUM+).
  - `.env`, `deploy.auto.tfvars` and the tfplan are gitignored.
- **Terraform gates** are hermetic again: their own data directory, no credentials.
- **Scale harness** refuses to run without the Postgres memory and swap caps.
- **Resilience:** 12/12 on the seed-804 criteria again, written to a new file, with old
  evidence untouched ([final-resilience-seed804-rerun.json](final-resilience-seed804-rerun.json)).
- **Cost, CLOUD-VERIFIED.**
  - GBP 0.65 month-to-date (Google budget notification, 2026-11-05). This is not an invoice.
  - The spend guard is holding, with the GBP 10 budget and GBP 8 guard.
  - No paid action was taken in this run ([final-cost-verification.md](final-cost-verification.md)).

## 8. Complete testing summary

**Host:** Linux 7.0.0-42, 16 threads, 14.4 GiB RAM, Docker 29.9.2 (cgroup v2), Python 3.12.14,
Postgres 17 (Docker).

While m1-500k ran, every other command was started under `taskset -c 10-15`. The exceptions
were Docker tool containers, and runs made after the scale run ended.

| Command | Exit | Result | Notes |
| --- | --- | --- | --- |
| `make check` (run 1) | **2** | 2,170 passed, 2 skipped; **`audit` FAILED** (oauthlib CVE-2026-49272) | Fixed: oauthlib 4.0.1 |
| `make check` (run 2) | **2** | Tests passed; **`tf-test` FAILED** (`terraform test` read the deploy's GCS backend and needed credentials) | Fixed: separate TF data directory |
| `make check` (run 3) | 0 | All gates passed | Code before ADR 0028 am. 1 |
| `make check` (**final**, on the final code) | **0** | **2,171 passed, 2 skipped**; coverage 99 % (floors OK, 225 files); ruff clean; mypy clean (482 files); 8 contracts kept; pip-audit and npm audit clean; frontend 66/66; tf-test 10/10; actionlint and zizmor clean | Run 2026-11-08 after the scale run ended (not CPU-pinned). Logs: `data/final-make-check.log` |
| `make e2e` | 0 | 1/1 Playwright journey | |
| `make agent-bench` (report redirected to `final-agent-benchmark.json`, so the Phase 12 file is not overwritten) | 0 | 9 passed; 7/7 scenarios | |
| `make agent-llm-protocol` | 0 | 1 passed; 7/7 | |
| `make resilience` (report redirected to `final-resilience-seed804-rerun.json`) | 0 | 18 passed; 12/12 PASS | |
| `make nightly-science` (development seed 1) | 0 | All acceptances pass | The contamination analysis exits 1 by design (negative control); recovery `passed=false` is the one documented known failure (ADR 0015 am.), with no new failures |
| `make nightly-perf` (run 1) | **2** | **FAIL:** 575 ev/s < 1,218 | Cause and fix: ADR 0028 am. 1 |
| `make nightly-perf` (run 2) | 0 | 1,541.9 ev/s chaos, 70,800 ev/s simulator | Baseline not re-recorded |
| `make nightly-security` | 0 | audits clean, 0 leaks, 0 misconfigurations | |
| `make tf-check` | 0 | 10/10 mocked Terraform tests, fmt, validate | MOCKED providers |
| `make stripe-verify` (once) | 0 | **5/5**, 4 customers on test clocks (deleted), 35 real events replayed reversed and duplicated, converged | SANDBOX ([final-stripe-sandbox.json](final-stripe-sandbox.json)) |
| `make scale-m1 SCALE=m1-500k` | 0 | PASS 12/12 (Postgres swap deviation) | EMULATED-M1 |

**Skipped tests (2):** `tests/data/test_bigquery_live.py` runs only through `make bq-verify`,
and `tests/payments/test_stripe_sandbox.py` only through `make stripe-verify`, which ran above.

**Gate runs on earlier code.** `e2e`, `agent-bench`, `agent-llm-protocol`, `resilience`,
`nightly-science` and `stripe-verify` ran on the code before ADR 0028 amendment 1, which
changes only the streaming CLI writers. The same suites also run inside the final `make check`.

Logs are in `data/final-make-check*.log` and `data/final-gates/` (gitignored).

## 9. Remaining externally blocked actions (owner)

1. **Promotion request `req_a680c42f35b5a82a`** (`demand-hybrid-84d53ff98b99`).
   - Reviewed on 2026-11-08: all 49 shadow reports passed, so it is **good to promote**
     ([final-promotion-review.md](final-promotion-review.md)).
   - The session's attempt to send the approval was blocked by its permission policy, so the
     owner sent it: **APPROVED and executed** by the owner on 2026-11-08 at 17:30:45 UTC (console POST, approver `owner`). Registry decision `promote-af5ee1e9c5b242b7ade67cbe26f29859`: `demand-hybrid-84d53ff98b99` -> champion, `demand-hybrid-aa245daf9e08` -> retired. **Done.**
2. **Rotate the console approver secret together with the next console deploy, not before
   it.** The deployed console's old parser rejects the rotated format (a duplicate `owner`
   during the overlap) and would fail at its next cold start. The order is: rotate, then
   `cloud-images`, `cloud-plan`, review, `cloud-apply`.
3. **Live Gemini benchmark:** needs `PRAXIS_GEMINI_API_KEY`, about USD 0.05
   (`make agent-llm-bench`).
4. **Optional:** re-run `make scale-m1 SCALE=m1-500k` with the fixed caps (about 3.3 h) to
   remove the Postgres swap deviation, or run the ladder on a real M1.
5. **Optional:** m1-1m, which was skipped by owner decision.
6. **Optional:** record the `github` perf baseline after the first nightly run in CI.
7. **Commit and push**, after review.

## 10. Git files changed

Nothing is committed or staged. `git diff --stat`: 31 tracked files changed, +1,217 / -306.

**Modified (tracked):**
- Root: `.env.example`, `Makefile`, `README.md`, `uv.lock`.
- `configs/`: `configs/mlops/demand.toml`, `configs/scale/slo.toml`.
- `docs/`: `docs/adr/0021-decision-console-and-admin-boundary.md`, `docs/adr/README.md`,
  `docs/deployment.md`.
- `src/praxis/` (14):
  - `agent/__main__.py`, `agent/wiring.py`;
  - `config.py`, `telemetry.py`;
  - `console/app.py`, `console/auth.py`;
  - `control/store.py`;
  - `data/warehouse.py`;
  - `mlops/config.py`, `mlops/pipeline.py`;
  - `simulator/engine.py`;
  - `streaming/__main__.py`, `streaming/consumers/warehouse.py`, `streaming/scale.py`.
- `tests/` (9):
  - `agent/test_operator.py`;
  - `console/test_auth.py`;
  - `control/test_store.py`;
  - `data/test_event_insert_paths.py`;
  - `mlops/test_config.py`, `mlops/test_pipeline.py`;
  - `scale/test_config.py`, `scale/test_scale.py`.

**New (untracked):**
- Code:
  - `src/praxis/agent/llm.py`;
  - `src/praxis/data/migrations/0002_event_dedupe_without_index.sql`;
  - `scripts/console_token.py`, `scripts/container_memory.py`, `scripts/pricing_heldout_report.py`.
- Tests: `tests/agent/test_llm.py`, `tests/agent/test_llm_bench.py`,
  `tests/pricing/test_evaluation.py`.
- Configs: `configs/agent/llm.toml`, `configs/pricing/evaluation_2026_11.toml`.
- ADRs: `docs/adr/0028-…`, `0029-…`, `0030-…`, `0031-…`.
- Case study: `docs/case-study/` (13 files, from Phase 16 and updated in this run).
- Evidence, this run: `docs/evidence/final-*.{md,json}` (25 files: release, scale (with 13
  scale JSONs), pricing (4), agent (2), resilience, Stripe, console proxy, cost).
- Evidence, Phase 16 and still uncommitted: `docs/evidence/phase-16.md`,
  `phase-16-agent-benchmark.json`, `phase-16-resilience-seed804-rerun.json`,
  `phase-16-test-report.json`.

**Changed but not in Git, by the owner's setup:**
- `CLAUDE.md`, `project_progress.md`, `.gitignore` and `infra/terraform/.gitignore` are
  ignored or untracked. The new `.terraform-offline/` ignore rule is therefore local only. In
  CI the directory is created in a fresh checkout and never committed.

**Not to be committed:** `.env`, `infra/terraform/envs/dev/deploy.auto.tfvars` and
`deploy.tfplan.txt` are gitignored, and `data/` (all run outputs and logs) is gitignored.
gitleaks found 0 leaks in the 77 uncommitted files.

## 11. Final release readiness assessment

**Ready for the final commit and push, as an honest portfolio release.** It is not yet a
system that should set real prices.

What holds, with evidence:
- Every gate in `make check` passes on the final code, and so do the opt-in gates: e2e,
  agent benchmarks, resilience, nightly science, perf and security, Terraform, and the
  Stripe Sandbox.
- The event backbone replays 500K virtual customers (48.7M events) to the exact oracle state
  inside an emulated 8 GB-M1 memory budget, with 0 OOM kills, under duplicates, reordering
  and crashes. The Phase 14 1M blocker is removed.
- Safety holds everywhere it was tested: 0 pricing constraint violations, 0 prohibited agent
  executions, and resilience 12/12.
- Docs, ADRs and evidence match the implementation, and every reported number has a source
  file.

What does not hold, stated as such:
- **Pricing value is not demonstrated.** The fresh held-out evaluation **failed** (H1, H2), and
  one world lost GBP 210.9/day. The optimiser must stay in shadow mode.
- **No live LLM has been evaluated.** The provider integration is tested offline only.
- **1M was not run.** 500K carries a disclosed Postgres swap deviation, and 250K is the largest
  step fully inside the registered budget. All scale results are emulations on one host, not
  M1 or cloud measurements.
- The 1K chaos benchmark is about 14 % slower than in Phase 16. It is still above its floor.
- The owner actions in s9 remain. **Rotate the console secret together with the next console
  deploy.**

For a resume, the defensible claims are:
- a correct, chaos-tested event backbone, measured to 500K customers in an 8 GB memory budget;
- pre-registered, held-out evaluation, with failures reported as failures;
- guarded AI-operator and console boundaries;
- a cost-capped GCP deployment that runs a daily synthetic loop.

Do **not** claim real revenue, profitable pricing, M1 or cloud scale, or millions of customers.
