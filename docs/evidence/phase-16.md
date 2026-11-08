# Phase 16 evidence: Final Evaluation and Case Study

```text
Phase: 16 - Final Evaluation and Case Study
Date: 2026-11-05
Code revision: 86ac9e5 (clean tree at start); the working tree adds documentation only:
               docs/case-study/, docs/evidence/phase-16*, README.md. No code, config,
               threshold, SLO, policy or model was changed.
Environment: local Linux 7.0, 16 logical cores, 14.4 GiB RAM, Python 3.12, Postgres 17 and
             the Pub/Sub emulator in Docker, Terraform 1.16.5 (Docker), Stripe Sandbox (test
             mode); read-only gcloud on praxis-dev-510522
All decision data is SYNTHETIC. Stripe results are Sandbox behaviour. Nothing is a real
business result.
```

This phase measures and documents. It does not improve results. The case study is
[`docs/case-study/README.md`](../case-study/README.md). The machine-readable test report is
[`phase-16-test-report.json`](phase-16-test-report.json).

## Tests executed (required_test.md s22)

| s22 item | Command | Exit | Duration | Result |
| --- | --- | --- | --- | --- |
| 1-3 unit, property and integration suites, plus every CI gate | `make lint typecheck arch ops-check test coverage-gate events-check schemas secrets audit frontend-check workflow-lint` | 0 | 1,493 s | 2,114 passed, 2 skipped (opt-in live suites). Coverage 98.84 %, floors OK on 224 files. 8 / 8 import contracts kept. mypy clean on 475 files. Vitest 66 passed |
| 12 Terraform validation | `make tf-check` on a clean copy of `infra/terraform` | 0 | 15 s | `terraform test` 10 / 10 (mocked providers). fmt and validate OK |
| 5 statistical evaluations (dev) | `make nightly-science` (seed 1) | 0 | 833 s | All dev acceptances reproduce, with only the documented recovery dev known failure (below) |
| 6 E2E business journey | `make e2e` | 0 | 9 s | Playwright 1 / 1: alert, incident, trace, pending request, authorized approval |
| 7 agent incident benchmark | pytest `tests/agent/test_benchmark.py`, report redirected to `phase-16-agent-benchmark.json` | **1 (harness error)**, then 0 on retry | 1 s / 13 s | Retry: 7 / 7, 0 prohibited executions, 28 adversarial attempts refused. **Deterministic triage, NOT an LLM** |
| 10 resilience suite | pytest `tests/resilience`, report redirected to `phase-16-resilience-seed804-rerun.json` | **1 (harness error)**, then 0 on retry | 6 s / 101 s | Retry: 12 / 12 PASS against `remediation-804.toml` (same criteria hash) |
| 11 security checks | `make nightly-security` (also `audit` inside `check`) | 0 | 60 s | pip-audit 0, npm 0, gitleaks over the full history: no leaks, trivy 0 |
| performance regression | `make nightly-perf` | 0 | 75 s | Simulator 72,555 ev/s (109.5 MB). Chaos 1,800.9 ev/s (536.4 MB). Within baseline |
| 4 Stripe Sandbox | `make stripe-verify` | 0 | 79 s | 5 / 5 in test mode (happy path, decline then recovery, Test Clock renewal and cancellation, idempotent enrolment, real events replayed reversed and duplicated) |
| 8 100K scale | not re-run | - | - | **Cited**: Phase 14 PASS, 10 / 10 SLOs ([phase-14.md](phase-14.md)) |
| 9 1M scale | not run | - | - | Not feasible on this host, and not approved |
| 13 final test report | generated | - | - | [`phase-16-test-report.json`](phase-16-test-report.json) |

**Harness errors.** The first agent-benchmark and resilience invocations ran pytest directly,
so that their reports would not overwrite the recorded Phase 12 and Phase 15 files. In doing
that I omitted `PRAXIS_TEST_DATABASE_URL`, which the Makefile exports. Every test errored at
setup, and no test executed. Both were re-run once with that variable added and nothing else
changed. Both records are kept in the test report.

## Results

### Statistical: the development world reproduces (seed 1; no held-out seed was run)

| Model | Phase 16 run | Recorded dev evidence | Same? |
| --- | --- | --- | --- |
| Forecast | 11 / 11. vWAPE 0.13492 vs naive 0.17419 (ratio 0.775). Coverage 50 / 80 % 0.4934 / 0.7915. `panel-edca81519a0e3165` | `phase-4-backtest-dev-seed1.json`: vWAPE 0.13492, coverage 0.4942 / 0.7918, `panel-ef9f1156730d4511` | vWAPE identical. The data_version and the 4th decimal of coverage differ (explained below) |
| Elasticity | PASS. Pooled -1.3546 vs truth -1.3372 (1.3 %, z 0.91) | `phase-5-dev-seed1-recovery.json` | Identical checks and `data_version` |
| Contamination | Detection and IV pass. The world's validity gates fail by design, so no artifact is saved | Phase 5 | Same behaviour |
| Pricing dev shadow (Phase 9 method) | PASS. 32 hold, 8 frozen, true delta 0 | `phase-9-dev-seed1-shadow.json` | Same decisions. The policy id differs (re-pinned at the third amendment) |
| Recovery dev | Only the documented known failure (`champion_truth_recovery`), `new_failures: []`. Net value per episode: champion 984.66, baseline 909.61 | `phase-8-dev-seed1-evaluation.json` | Identical policy values. The model id differs (it hashes the code revision) |

**Why the forecast data_version differs.** The recorded dev file predates the 2026-10-05
loader determinism fix ([phase-4.md](phase-4.md), "Known limitations"). Two rebuilds after
that fix both gave `panel-edca81519a0e3165` (`data/forecast/determinism.log`), which is the
version reproduced here. The point forecast is identical. Coverage moves only in the 4th
decimal, as recorded at the fix.

### Held-out results: cited, never re-run

All held-out results are cited from their phase evidence, including every failure. The
mapping is in [`docs/case-study/experiment-cards.md`](../case-study/experiment-cards.md):
- E1 / E2: forecast, seed 42. E3: elasticity, seed 42.
- E4: pricing. Seed 42 FAIL, 43 PASS, 44 / 45 FAIL, 201 / 203 PASS, 202 FAIL, 301-303 PASS
  (holds only).
- E5: recovery, seed 42. E6: the controlled seed-42 example.
- E7: lifecycle. Seed 601 FAIL, 621 PASS.
- E8: agent. E9: scale. E10: cost.

### Re-runs of the pinned benchmarks (not held-out worlds)

- The agent benchmark equals Phase 12: 7 / 7, `prohibited_executed_total` 0,
  `provider_tokens_total` 0.
- Resilience on fault seed 804 equals the recorded report on every measured outcome. The only
  difference is the `db_timeout` lock duration, 5.14 s vs 5.15 s.

### Live loop (operating environment; read-only; not gate evidence)

The first scheduled daily tick ran on 2026-11-05 at 03:13 UTC
(`gs://praxis-dev-live-praxis-dev-510522/reports/2026-11-05.json`):
- status `operated`, behind_days 0;
- steps 23 + 9 + 18 + 14 + 2 s;
- drift alarm (residual and segment drift);
- shadow `demand-hybrid-84d53ff98b99` still ready for promotion;
- champion still `demand-hybrid-aa245daf9e08`;
- the promotion request is still PENDING the owner.

Nothing was triggered, edited or approved.

## Performance metrics

- `nightly-perf`: chaos 1,800.9 ev/s (Phase 15 final: 1,764). Simulator 72,555 ev/s.
- The full pytest suite took 22 m 53 s.
- Scale is cited: 100K at 4,494.6 ev/s, op p99 21.3 ms, state equal to the oracle. 1M was
  not run.

## Statistical metrics

See Results. No held-out metric was produced in this phase.

## Failures found

- Two harness errors in my own runner (above). No test executed in those attempts.
- No failing test, gate or check in the final invocations.
- No new defect was found in the system.

## Fixes

None to code, configuration or tests. The runner's environment was corrected for the retries
only.

## Known limitations

See [`docs/case-study/limitations.md`](../case-study/limitations.md). The carried-over caveats
are:
- **Phase 9** passed with holds and freezes only: no price moved on the held-out worlds.
- **Phase 12**'s benchmark is the deterministic triage, **not an LLM**.
- **Phase 14**'s 1M run was **NOT RUN**, so its gate did not pass.
- **Phase 15** passed only after an owner-approved remediation. The earlier FAILs (seed 801,
  10 / 12) stand.
- The live loop's retrain cost grows with the history. This is an open owner decision.
- The Phase 6 and Phase 9 shadow results were measured with the pre-Phase-10 forecast-loader
  defect. A fresh pre-registered evaluation is needed before live pricing.
- The 100K result was cited, not re-run, and predates ADR 0025 am. 1. The final code's 100K
  throughput is unmeasured.

## Cloud cost incurred

- No cloud resource was created and no paid query was run. Read-only gcloud listings only.
- The Stripe Sandbox (test mode) cost nothing.
- **Actual project spend to date has not been measured.** There is no billing export. The
  owner should read it from the Billing console. Estimates are in
  [`cost-report.md`](../case-study/cost-report.md): deployment GBP 0.10-0.20 a month, plus
  USD 0.5-3 a month for the live loop.

## Gate

**PASS**, with the limits stated below.

## Reason

The Phase 16 gate (project_plan.md) requires a case study that clearly separates simulated
results, real external data, Stripe Sandbox behaviour, measured production-like performance,
assumptions and limitations, and never presents synthetic uplift as real revenue.
[`docs/case-study/README.md`](../case-study/README.md) section 1 has one subsection for each of
the six. Every number links to an evidence file. The banner and each card label the synthetic
results.

All twelve evidence-package documents exist:
- architecture, data lineage, decision trace;
- model cards, experiment cards;
- SLO report, load-test report, incident postmortem;
- cost report, security assumptions, limitations, reproducibility.

The s22 suite was run. Every item that was run exits 0 on its final invocation, and the two
harness errors are disclosed.

Limits of this PASS:
- It certifies the evidence package and the final acceptance run. It does not turn earlier
  NOT PASSED gates into passes: Phase 14 (1M) remains not passed.
- s22 item 8 (100K) was cited, not re-run, and item 9 (1M) was not run.
- The cost figures are estimates.
