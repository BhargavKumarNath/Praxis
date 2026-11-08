# Postmortem: healthy messages dead-lettered by other messages' crashes

| | |
| --- | --- |
| Incident | Resilience row 5, `consumer_crash` |
| First observed | 2026-11-01, Phase 15 resilience suite, fault seed 801 |
| Environment | Local Pub/Sub semantics (in-memory broker with delivery-attempt counting), Postgres, DuckDB; **SYNTHETIC** world (sim seed 1) |
| Severity | Page (`dlq_backlog_high`). Data loss after the runbook: 0. Wrong state after the runbook: none |
| Status | Resolved by ADR 0025. Verified on fresh fault seeds 803 and 804 (12 / 12 each) |
| Blameless | Yes. The cause is a design property recorded in ADR 0009, not an operator error |

Evidence:
- the failure: [`phase-15.md`](../evidence/phase-15.md) and [`phase-15-failures.json`](../evidence/phase-15-failures.json);
- the fix: [`phase-15-remediation.md`](../evidence/phase-15-remediation.md) and [`phase-15-optimisation.md`](../evidence/phase-15-optimisation.md);
- the verification: [`phase-15-remediation-803-failures.json`](../evidence/phase-15-remediation-803-failures.json) and [`phase-15-remediation-failures.json`](../evidence/phase-15-remediation-failures.json) (seed 804);
- the decisions: [ADR 0023](../adr/0023-resilience-suite-and-two-window-alerting.md) and [ADR 0025](../adr/0025-crash-isolating-consumer-flow-control.md).

## Summary

The test injected isolated consumer crashes at 1 % per message. Each crash killed only the
in-flight work. The operational consumer crashed 9 times and the warehouse consumer 239
times. Those crashes **dead-lettered 1,305 healthy messages**; the pre-registered expectation
was 0. The alert fired on the first tick, and the runbook redrive of 1,319 letters restored
the state exactly to the oracle. The system stayed correct, but it was not isolated: one bad
delivery could push its innocent batch-mates toward the DLQ.

## Timeline (2026-11-01)

1. The criteria `configs/resilience/failures.toml` (v1) were pinned before any failure ran.
2. **Attempt 1 FAIL.** After the redrive, the oracle was missing 4 records, because one
   `dlq --redrive` stops at 1,000 letters. Fix: `redrive_all` in the CLI and in `run-local`.
   The criteria were not changed.
3. **Attempt 2 FAIL** (recorded result, seed 801). Data loss 0, but 1,305 innocent messages
   were dead-lettered. The phase gate was **NOT PASSED** (10 / 12).
4. The owner approved remediation. ADR 0025 designed it on development data only.
5. The remediation criteria were pinned before the run. The only change was to the
   `consumer_crash` row, which carries a written amendment: v1 expected the storm to
   dead-letter in-flight messages, which is the collateral itself. Seed 802: 12 / 12 PASS.
6. `nightly-perf` then found that the chaos throughput had **fallen to 433 ev/s** (floor
   1,218). The flow control was redesigned, still on dev data. Of the seven variants, three
   were rejected because they dead-lettered healthy messages.
7. The same criteria were re-registered on fresh seed 803 and run once: 12 / 12. The perf
   gate still failed (about 930 ev/s).
8. Profiling found the bottleneck: the fixed cost of each DuckDB `INSERT ... ON CONFLICT`
   write. ADR 0025 amendment 1 changed it to a primary-key lookup plus a plain INSERT, which
   is property-tested against the loader. Throughput rose to 1,718 ev/s median, and
   `nightly-perf` exited 0 at 1,764 ev/s.
9. Fresh fault seed 804 (final code, run once): **12 / 12 PASS**.

## Root cause

- Pub/Sub counts a delivery attempt for every lease that ends without an ack.
- The warehouse consumer leased and processed each pull as one atomic batch (ADR 0009 batch
  semantics).
- So a crash on one message cost an attempt to every message in the batch, and the crashed
  batch came back as one group.
- At a 1 % crash rate, enough innocent messages collected 5 attempts to reach the DLQ.

## Fix (ADR 0025)

Both pull loops now go through `FlowControl` / `process_pull`:
- **Slow start.** One lease after a (re)start, doubling per completed pull.
- **Evidence tiers.** First deliveries and first redeliveries are processed as separate
  atomic groups, and each group is acked when it completes.
- **A message redelivered twice is leased and processed alone**, so a real poison message
  isolates itself.

The DLQ policy (5 attempts), the alerts, the runbook and the redrive are unchanged.

## Verification

| Seed 804, `consumer_crash` | Measured |
| --- | --- |
| Isolated crashes (12,115 events) | 13 operational and 204 warehouse crashes; **0 dead letters**; state = oracle |
| Poison storm (12 `payment.failed` events that crash on every delivery) | Exactly the 12 poison events dead-lettered on each subscription, **0 healthy** |
| Detection / clearance | `dlq_backlog_high` on tick 1; cleared 2 ticks after the redrive |

## What went well

- Pre-registration made the failure impossible to explain away. The v1 record was kept
  unchanged.
- The oracle proved, at every step, that no data was lost.
- The perf regression gate caught a "fix" that would have cut chaos throughput by 79 %.

## What went badly

- The first remediation (seed 802) was evaluated before its throughput was measured, so a
  second fresh seed was needed.
- The chaos throughput is still about 17 % below the pre-remediation HEAD. That is a known,
  accepted trade-off, inside the floor.

## Follow-ups

| Item | Status |
| --- | --- |
| Crash-isolating flow control in every new pull loop (CLAUDE.md s24) | Done (rule) |
| Streaming writes never go back through ON CONFLICT (`tests/data/test_event_insert_paths.py`) | Done (pinned) |
| `run-local` refuses a non-empty control plane, after a false oracle mismatch on a reused DB | Done |
| Crashes are in-process exceptions, not OS process kills; Pub/Sub was the emulator | Open limitation |

## Second recorded incident (cloud): live tick 7 timeout

On 2026-11-04, live tick 7 (execution `f54vk`) was killed at the 1,500 s step timeout while
`operate` ran a retrain day. The retrain had already completed. The state was not advanced,
so the next tick (`thjzz`) re-ran the same days, found the finished retrain, and did not
repeat it.

Root cause: the cost of a retrain grows with the history, because the backtest covers all of
it. Fix: ADR 0027 amendments 1 and 2 raised the timeout budget and moved to 4 vCPU. A
bounded backtest window is still an open owner decision. Source:
[live-loop.md](../evidence/live-loop.md).
