# ADR 0025: Crash-isolating consumer flow control

Status: Accepted (2026-11-01, Phase 15 remediation). Amends ADR 0009 ("Acks", and the
measured crash-collateral finding). Evidence: `docs/evidence/phase-15-remediation.md`.

## Context
Pub/Sub counts a delivery attempt for every lease that ends without an ack. A nack, an
expired lease and a crashed process all count the same way. When a process dies, every
message it holds leased loses one attempt, whatever caused the crash. After
`max_delivery_attempts` (5) the broker dead-letters the message.

ADR 0009 measured this, and Phase 15 confirmed it (fault seed 801, `consumer_crash` FAIL):
at a 1 % crash rate the batched warehouse consumer dead-lettered 1,305 healthy
batch-mates. A crashed batch came back as one leased group with the message that had
crashed it, so each further crash cost every member another attempt.

## Decision
Every consumer process limits and groups what it holds leased, using
`praxis.streaming.transport.FlowControl` and `process_pull`. Both pull loops share them:
`memory.drain` for local and test runs, and `pubsub.run_pull_loop` for Pub/Sub.

* **Slow start.** A new process cannot tell a first start from a restart after a crash. It
  leases one message, doubles its window after every pull it completes, up to the batch
  size, and starts over at one after a crash.
* **Evidence tiers inside a pull.** First deliveries run first, as one atomic group for a
  batch consumer. First redeliveries run next, as their own group, never mixed with first
  deliveries. Messages delivered `ISOLATE_FROM_ATTEMPT` (3) or more times, i.e.
  redelivered twice, run last and alone, least-retried first. Every group is acked as soon
  as it completes.
* **Isolation shrinks the lease.** A completed pull that held a repeatedly redelivered
  message drops the window to one, so suspects are leased alone. A first redelivery does
  not shrink the window: a crash redelivers everything the process held, and nacks and
  transient errors redeliver too. Recovery from an outage must not run one message per
  pull.
* **Poison is still bounded by the broker.** A message that kills the process on every
  delivery ends up leased alone, so only its own attempts grow. The existing dead-letter
  policy forwards it after 5 attempts; the alert, runbook and redrive are unchanged. Errors
  that raise (as opposed to crashes) keep the ADR 0009 policy.

How the rule was chosen, all on development data only (fault seeds 801, which was already
spent, and 9001–9003; development poison flows; the `make nightly-perf` chaos world, seed
42):

| Variant | Healthy dead letters (4 seeds) | Chaos throughput | Outcome |
| --- | --- | --- | --- |
| A: any redelivery resets the window to one; every redelivery processed alone | 0 | 433 ev/s | Ran once on seed 802 (12 / 12); rejected after `nightly-perf` measured it |
| Window driven by crashes only; redeliveries batched | 32 and 37 | about 1,750 ev/s | Rejected: unsafe |
| As above, isolating from attempt 3 but without shrinking the window | 24 and 19 | about 980 ev/s | Rejected: unsafe |
| C (chosen): tiers as above, isolation shrinks the window | 0 | about 930–1,040 ev/s | Chosen |
| C, isolating from attempt 4 | 1 | about 1,290 ev/s | Rejected: unsafe |
| C, first redeliveries in groups of 10 | 0 | 927 ev/s | Rejected: no faster, one more knob |
| A fresh-only pull jumps straight to full size | 0 | 879 ev/s | Rejected |

Not chosen:
* Raising `max_delivery_attempts` would hide the collateral and slow down poison handling.
* Durable per-message crash markers or per-batch bisection need a database write per
  suspect, and control-plane access for every consumer.
* A faster warehouse insert path was measured: it cut 3.05 to 2.45 ms per call, which is not
  worth changing the shared loader SQL.

## Consequences
* The final code was judged once on fresh fault seed 803
  (`docs/evidence/phase-15-remediation-failures.json`):
  * 0 dead letters across 249 isolated crashes, with state equal to the oracle;
  * exactly the 12 poison events dead-lettered on each subscription, and no healthy
    message;
  * `dlq_backlog_high` fired on tick 1.
* `tests/streaming` scenario 09b now asserts that a transient crash storm (every event
  crashes once) dead-letters nothing. It used to dead-letter 14 messages. Scenario 09c
  covers a process-killing poison.
* **Throughput trade-off, measured; the perf gate FAILS.** In an A/B against HEAD (`git
  archive`, fresh database each run):
  * With crashes disabled: 2,385 vs 2,383 ev/s, i.e. unchanged.
  * Under the `nightly-perf` chaos injection (0.5 % crashes before and 0.5 % after the side
    effect, per message, plus 10 % duplicates, 600 s delays and reordering): 2,046 vs
    932 ev/s. `make nightly-perf` reports 927–931 ev/s against its floor of 1,218 (0.5 × the
    recorded baseline of 2,436).
  * Why: isolated messages and slow-start ramps mean many more small DuckDB writes, each
    with a fixed cost of about 3 ms per call.
  * The baseline has not been re-recorded (CLAUDE.md s24). Re-recording it as a deliberate
    trade-off, or funding a faster warehouse sink, is the owner's decision.
* A single poison message gives one dead letter per subscription, which is below the
  `dlq_backlog_high` page limit of 10. It is visible on the dashboard and in
  `python -m praxis.streaming dlq`; a storm of them pages.
* Residual risk: there is no hard bound below the broker's five attempts.
  * Messages in their first redelivery still share a batch group, so a second crash in that
    group costs each of them another attempt.
  * An isolated message can still be leased in the same pull as other groups that crash
    before it runs. The window then drops to one, but it can grow again before the message
    returns.
  * The rule made healthy dead letters 0 on every development seed and on fresh seed 803.
    The variant that isolated one attempt later left 1, which shows the margin is real but
    finite.
  * Real Pub/Sub does not preserve redelivery order, so ordering effects seen on the
    in-memory broker can differ.
* The emulator and managed Pub/Sub honour `max_messages` per pull as an upper bound, so
  the window is enforced client-side with no broker feature.

## Amendment 1 (2026-11-01): warehouse write path; the perf gate passes

Owner decision: no new perf baseline; optimise with the reliability rules kept. The lease,
grouping and ack rules above are **unchanged**.

**Bottleneck, measured.** Profiling the `nightly-perf` chaos run put 66 of 145 s (under
cProfile) in `Warehouse.insert_events`: 17,118 calls, 11,263 of them single-message writes of
repeatedly redelivered messages. A one-row write cost 3.06–3.3 ms almost regardless of size
(100 rows: 5.9 ms):
* the temporary NDJSON file: 0.15 ms;
* the `read_json` scan: 0.85 ms;
* DuckDB's `INSERT … ON CONFLICT` into the primary-key table: about 2.2 ms. A plain INSERT
  into the same table costs about 0.7 ms.

So the isolation rule was cheap in lease terms but expensive because each isolated write paid
the conflict statement's fixed cost.

**Change.** `insert_events` is still insert-if-absent with the batch loader's projection, but:
1. ids already in `raw.sim_events` are found by a primary-key lookup (`IN (…)`, flat
   0.2–1.2 ms from 50k to 3M rows) and skipped. The first occurrence of an id inside a
   batch wins, as with `ON CONFLICT DO NOTHING`.
2. Only new rows are written, by a plain INSERT: bound parameters up to 8 rows (measured
   crossover 12–16), NDJSON above that.
3. If another writer adds one of those rows between the lookup and the INSERT, the primary
   key raises `ConstraintException` and the batch is re-run with the loader's original
   `ON CONFLICT` statement. A race cannot duplicate or lose an event.

`tests/data/test_event_insert_paths.py` pins it to the reference loader statement:
* a property test over random batch sequences (duplicates, changed content under the same
  id, both sides of the threshold) checks identical rows and identical NEW counts;
* the race fallback is tested at 1, 8, 9 and 40 rows.

Per call it is now 0.81 ms for 1 row (3.06 before) and 4.1 ms for 100 rows (5.9 before). A
redelivery of a row already stored is now a lookup only: 9,596 of 17,118 calls.

**Not chosen.**
* `SET threads = 1`: it saved 1.4 ms per small write, but it is a database-wide setting
  that would slow analytical queries.
* Batching isolated messages into shared writes: that would undo the isolation.
* More flow-control variants: they trade reliability for throughput (see the table above).
* Staging table plus merge: it changes the warehouse read contract.

**Result.** A/B on the `nightly-perf` world, fresh database per run, interleaved, 3 runs
each:

| Code | ev/s, median (range) |
| --- | --- |
| HEAD | 2,059 (2,019–2,147) |
| Remediated | 980 (939–987) |
| Optimised | 1,718 (1,711–1,800) |

The optimised delivery trace (crashes, deliveries, rounds per subscription) is identical to
the remediated one, with 0 dead letters and an oracle match. The final code passed the fresh
fault seed 804, pre-registered and run once: 12 / 12 (ADR 0023 amendment 3). Chaos
throughput is still about 17 % below HEAD: the price of the extra small writes the isolation
needs, now above the unchanged floor of 1,218.
