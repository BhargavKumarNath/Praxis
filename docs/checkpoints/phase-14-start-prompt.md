Start Phase 14 (Scale Engineering). Goal: measured, reproducible evidence of how far the
architecture scales on this machine, with every bottleneck named. Phases 0-13 and the Phase 11
wiring are done. Do not reopen them.

Read first, and only what you need:
- CLAUDE.md (s4 boundaries, s5 Stripe safety, s14 cost, s15 scaling, s16 testing, s18
  dependencies, s21 source freshness, s22 stop conditions, s24 contracts and perf baseline).
- project_plan.md, the Phase 14 section (Steps A and B, investigate list, gate).
- required_test.md: s20 (Phase 14 tests) and s23 (evidence template).
- The "Next" and "Carry-over" sections at the end of project_progress.md.
- docs/evidence/phase-3.md and phase-3-run-local-{1k,10k}.json, phase-1-scale-smoke-10k.json,
  phase-11-wiring.md (telemetry overhead), benchmarks/perf_baseline.json,
  scripts/perf_check.py, docs/event-backbone.md, docs/observability.md, docs/cost-guard.md.

Known measurements to start from (do not re-measure them to look better):
- 10K customers x 28 days = 977,754 events. Simulator alone: 17 s, 219 MB.
- run-local 10K (memory transport, all consumers): 518 events/s, 31 min, peak RSS 1.8 GB.
- event-chaos with telemetry: about 2,137 events/s vs 3,120 stubbed (about 32% overhead).
- Extrapolated, unoptimised: 100K = about 9.8M events, about 5 h and above this machine's
  memory; 1M = about 98M events. Machine: 16 cores, 14 GB RAM, about 260 GB free disk.

Before coding, reply with a short plan (max ~30 lines):
- the scale protocol: workloads (simulator, direct event producer through the consumers, HTTP
  surfaces, SyntheticPaymentGateway), customer counts, days, and what each run measures;
- the SLO targets for 100K and 1M, written to a config (e.g. `configs/scale/slo.toml`) and
  pinned by a test BEFORE any 100K run (s20: thresholds are set before, never after);
- the profiling you will do first and the bottleneck hypotheses;
- every new dependency or tool (k6 / Locust: prefer a pinned Docker image over a Python
  dependency), with a justification under s18;
- where large event history goes: local Parquet / DuckDB as the analytical stand-in, never
  Postgres; any BigQuery use (scanned bytes for representative queries) only in the sandbox,
  with dry runs, and only after my approval;
- the ADR(s) you will write and anything that needs my approval.
Then proceed without waiting unless something needs approval.

## Method

1. Run `make audit` first.
2. Profile before optimising (CPU and memory profiles of run-local at 10K). Name the top
   bottlenecks with numbers.
3. Each optimisation is one change, justified on its own, measured on the same development
   workload before and after, and kept only if it helps without breaking an oracle check
   (`matches_oracle`, subscription consistency, ledger totals). Candidates from the plan:
   batch writes, DB pooling, consumer concurrency, Pub/Sub flow control, telemetry
   aggregation (the 32% item), memory pressure, partitioning / clustering.
4. Ladder (CLAUDE.md s15): 1K -> 10K -> 100K -> 1M. 1M only after 100K meets its
   pre-registered SLOs. If 100K fails, report the failure with its bottleneck and stop for me.
5. Seeds: development seed 1, or fresh seeds from 701 upward recorded in the config. Never a
   held-out seed listed in CLAUDE.md s24.
6. Correctness at scale matters more than speed: every scale run checks its oracle.
7. Cloud Run concurrency and Pub/Sub cloud settings: analyse and document; do not provision
   anything without my approval and a cost estimate.

## Rules (CLAUDE.md integrity and git rules apply in full)

- Local only, GBP 0, unless I approve otherwise. Never load test Stripe Sandbox or a public
  API; replay external sources.
- Never lower an SLO, floor or perf ratio after seeing a result. Re-record
  `benchmarks/perf_baseline.json` only with evidence of a measured optimisation, and tell me.
- Long runs go in the background, resumable, with the checkpoint
  `docs/checkpoints/phase-14-checkpoint.md` kept current. Tell me before any run longer than
  about 15 minutes, with its expected duration and memory.
- No unmeasured claim. Never write "supports N customers" without that run's numbers.
- Run `make nightly-perf` and `make nightly-security` before finishing and report them. Run
  `make check` once at the end, in the background; it must exit 0.

## Finish with

- `docs/evidence/phase-14.md` in the s23 template. For every claimed scale: customer count,
  event count, duration, throughput, p50 / p95 / p99, error rate, queue / consumer lag, CPU,
  memory, database pressure, compute resources, cost, bottleneck and configuration; for 1M
  also changes since 100K, warehouse volume and scanned bytes (or why not measured);
- the machine-readable run reports (`docs/evidence/phase-14-*.json`);
- ADR(s) for the scaling decisions; updates to architecture.md, docs/event-backbone.md,
  CLAUDE.md s24 (any new targets) and project_progress.md;
- a concise report: what changed, measured results per scale, bottlenecks, open decisions, and
  a plain statement of whether the gate passed, with its limits. Never commit or push.
