# Phase 3 Evidence Report

```text
Phase: 3 - Event Backbone and Stateful Processing
Date: 2026-10-04
Code revision: 41dc5d0 (HEAD) + uncommitted working tree (no commits made, per policy)
Environment: Linux, Python 3.12.14, postgres:17-alpine (Docker, 127.0.0.1:55432), Pub/Sub emulator
             (gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators, 127.0.0.1:8085), duckdb 1.5.6,
             sqlalchemy 2.1.3, alembic 1.20.0, psycopg 3.3.6, google-cloud-pubsub 2.42.0
All events are SYNTHETIC (Phase 1 simulator). No cloud resource was created or used.
```

Design: `docs/event-backbone.md`, ADR 0009 (order-independent processing). Terraform: `infra/terraform/modules/pubsub`.

## Tests executed

`make check` (ruff, ruff format, mypy strict, pytest with coverage against local Postgres and the Pub/Sub emulator,
`events-check` chaos smoke, schema sync, secret scan, frontend, terraform validate) exits 0.
Pytest: **553 passed, 1 skipped** in 290 s, including the 7 emulator integration tests (the skip is the live
BigQuery module, which runs only via `make bq-verify`). Coverage 98.6% total; critical modules: control store 100%,
redrive 100%, codec 99%, memory broker 99%, projections 98%, runtime 98%. The only warning is a third-party
Starlette deprecation in `fastapi.testclient`.

| Required (required_test.md section 9) | Test (`tests/streaming/test_event_scenarios.py`) |
| --- | --- |
| 1. normal event | `test_scenario_01_normal_event` |
| 2. exact duplicate | `test_scenario_02_exact_duplicate` |
| 3. duplicate after consumer restart | `test_scenario_03_duplicate_after_consumer_restart` |
| 4. out-of-order pair | `test_scenario_04_out_of_order_pair`, `test_scenario_04b_randomly_reordered_and_delayed_stream` |
| 5. malformed payload | `test_scenario_05_06_invalid_messages_go_straight_to_dlq` |
| 6. unsupported schema version | same test (envelope `schema_version` 2 -> DLQ reason `unsupported_schema_version`) |
| 7. temporary database failure | `test_scenario_07_temporary_database_failure`, `test_scenario_07b_outage_longer_than_retry_budget_dead_letters_then_redrives` |
| 8. consumer failure before ack | `test_scenario_08_consumer_failure_before_ack` |
| 9. failure after side effect, before ack | `test_scenario_09_failure_after_side_effect_before_ack`, `test_scenario_09b_crash_storm_burns_retries_of_in_flight_messages` |
| 10. poison event to DLQ | `test_scenario_10_poison_event_to_dlq` (in-memory) and `test_poison_message_is_forwarded_by_pubsub_after_bounded_attempts` (emulator: exactly 5 attempts) |
| 11. replay from archived event | `test_scenario_11_replay_from_archived_events` (fresh DB, identical checksum) |

Each scenario checks the five required assertions: no duplicate side effect (processed rows, ledger, warehouse
rows), final state correct (snapshot vs expected), DLQ count observable (`dead_letters` table and metrics), trace ID
preserved (trace and correlation IDs in every sink), retries bounded (delivery attempts <= 5).

Other Phase 3 tests: codec contract (`test_codec.py`), topology = Terraform drift check (`test_topology.py`),
broker semantics (`test_memory_broker.py`), archive and producer (`test_archive_producer.py`), runtime failure
policy (`test_runtime.py`), consumers (`test_consumers.py`), Pub/Sub adapter (`test_pubsub_unit.py`), CLI
(`test_cli.py`), order-independent folds incl. property tests over permutations (`test_projections.py`),
migrations and the DB-level transition guard (`tests/control/`), and 7 emulator integration tests
(`tests/integration/test_pubsub_emulator.py`: subscription filter, DLQ after exactly 5 attempts, NACK redelivery,
end-to-end with producer-side duplicates, every CLI command).

| Gate (project_plan.md Phase 3) | Evidence |
| --- | --- |
| Duplicate events do not duplicate money or state changes | scenarios 2, 3, 9; chaos runs with 10% duplicates + crashes: ledger entries and total equal the oracle; `payment_ledger` unique per invoice and CHECK constraints in the DB |
| Out-of-order tests pass | scenarios 4 and 4b; projection property tests (any permutation gives the same state); chaos runs with reorder and up to 600 s delay match the oracle |
| DLQ is observable | `dead_letters` table (reason, attempt, source subscription, event, trace IDs), `dlq` CLI, per-subscription metrics and structured logs; emulator confirms Pub/Sub forwarding after 5 attempts |
| Replay produces the expected final state | scenario 11 (fresh DB, same checksum); `replay` CLI on the emulator republishes every archived event |
| Correlation IDs trace an event across services | `test_correlation_id_traces_an_event_across_services`: same IDs in operational (Postgres), warehouse (DuckDB) and monitoring |
| Measured event latency is recorded | emulator benchmark below; every report carries per-subscription processing and end-to-end p50/p95/p99 |

## Performance metrics

Chaos runs: `python -m praxis.streaming run-local` (in-memory broker with Pub/Sub semantics, real Postgres and
DuckDB). Faults: 10% duplicates, delay up to 600 s, reordering, 0.5% consumer crashes, seed 42. Correctness is
checked against an independent oracle (Phase 1 `StreamValidator` over the sorted events).

| Run | Events | Wall | Throughput | Peak RSS | Oracle | Dead letters (before -> after redrive) |
| --- | --- | --- | --- | --- | --- | --- |
| 1K x 28d (`make events-local`, 2026-10-04) | 99,979 | 56.7 s | 1,763 ev/s | 545 MB | match, checksum `c8928ca9...` | 100 -> 0 |
| 10K x 28d (2026-10-04) | 977,754 | 31 min 44 s | 518 ev/s | 1.8 GB | match, checksum `5dc6a62d...` | 300 -> 0 |

Both: 0 subscription inconsistencies, warehouse rows = events published, 0 pending or orphaned events, processing
time per message p50: operational 2.6 ms, warehouse 0.09 ms, monitoring 0.03 ms. The 1K checksum is identical to
the run before the warehouse fix (deterministic). Earlier in the session the same 1K run took 42-45 s (2,220-2,385
ev/s), so wall time varies about 30% with machine load.

Live latency over the Pub/Sub emulator (`emulator-bench`: paced producer, concurrent pull consumers):

| Offered rate | Events | Operational e2e p50 / p95 / p99 | Monitoring e2e p50 / p95 / p99 | Oracle |
| --- | --- | --- | --- | --- |
| 200 ev/s | 4,299 | 71 / 133 / 144 ms | 48 / 71 / 77 ms | match, 0 dead letters |
| 2,000 ev/s | 11,990 | 589 / 1,462 / 1,621 ms | 913 / 1,247 / 1,277 ms | match, 0 dead letters |

At 2,000 ev/s the consumers fall behind (latency is backlog). The warehouse consumer is drained after publishing
(DuckDB has a single writer), so its end-to-end figure is backlog, not live latency.

Raw reports: `phase-3-run-local-1k.json`, `phase-3-run-local-10k.json`, `phase-3-emulator-bench-200eps.json`,
`phase-3-emulator-bench-2000eps.json`.

## Statistical metrics

None claimed. Correctness is exact equality with the oracle, not a statistical test.

## Failures found

1. `Warehouse.insert_events` counted the whole table after every batch, so runs slowed down as the table grew
   (found with cProfile).
2. Crash storms dead-letter innocent in-flight messages: a crash nacks the whole in-flight batch, so repeated
   crashes use up the 5 delivery attempts of messages that were never faulty (100 at 1K, 300 at 10K, all warehouse
   batches). No data is lost; each is a dead letter until redrive.
3. Redriven messages had no send timestamp, so end-to-end latency fell back to the broker's publish time. In the
   in-memory broker that is a virtual 2026-01-01 clock, so the max latency read about 276 days. Found in the 1K
   evidence run.
4. The oracle assumed sorted input; the producer does not sort.
5. Monitoring and DLQ metrics were keyed by consumer role instead of subscription, so two environments mixed.
6. Exponential backoff overflowed (`OverflowError`) on the DLQ inspector subscription, which has no attempt cap.
7. `LocalPipeline` rebuilt its workers on every drain, losing in-memory consumer state.
8. Test defects: an invoice-twin test assumed the wrong tie-break (the canonical order breaks ties by event_id), and
   the `DatabaseOutage` helper could not target a later statement.

## Fixes

1. The insert uses the row count the INSERT returns; regression test added.
2. Documented in ADR 0009 and `docs/event-backbone.md`. Workers ack progressively per message and batch workers fall
   back to one-by-one processing on failure; `run-local` reports dead letters before and after redrive. Tests 09b
   (pins exactly 14 dead letters, then redrive restores the exact state) and 07b.
3. Redrive stamps a fresh send time (`praxis_sent_at`). Test `test_redriven_messages_measure_latency_from_the_redrive`
   fails without the fix. The 10K report predates the fix: its end-to-end **max** values are this artifact (300 of
   about 1.08M deliveries). Its p50, p95 and p99 are not affected, because 0.03% is below the 1% tail.
4. `expected_state` sorts by `occurred_at` (stable sort) before validating.
5. Metrics are keyed by `delivery.subscription`.
6. Backoff exponent capped at 32.
7. Workers persist across drains.
8. The test asserts that exactly one creation applies; `DatabaseOutage.fail_next(n, after=)`.

## Known limitations

* **10K throughput does not scale linearly.** 518 ev/s at 10K against 1,763-2,385 ev/s at 1K (2K -> 4K scaled
  linearly). Not profiled yet. Lead: the warehouse drain needed 735,676 rounds at 10K against 3,465 at 1K (212x for
  10x data). Crash fallbacks to one-by-one processing appear to multiply the number of rounds, and each round has
  per-lease overhead (`MemoryBroker._expire_leases` / `next_wakeup`). This is a hypothesis to confirm with
  `py-spy` before Phase 14 scale work. It concerns the in-memory test broker, not Pub/Sub.
* In-memory end-to-end latency is wall time inside a batch run (queueing), not service latency; use the emulator
  figures for latency.
* Pub/Sub verified on the emulator only; no real topic, subscription or IAM was applied. The emulator does not
  enforce IAM, so DLQ forwarding permissions are checked only statically (Terraform).
* Redrive is an operator action (`dlq --redrive`), not automatic.
* One local Postgres; no connection-pool or failover testing. DB pressure was not measured beyond wall time.
* The emulator benchmark used 50-200 customers; no scale test over the emulator.

## Cloud cost incurred

GBP 0. Postgres and the Pub/Sub emulator run in local Docker containers. Terraform changed but was not applied.

## Gate

PASS.

## Reason

Every gate item has a passing test, and the 1K and 10K chaos runs meet it end to end: duplicates, delay, reordering
and crashes give exactly the oracle's state and money, every dead letter is recorded and recoverable, replay into a
fresh database reproduces the checksum, IDs trace across all three sinks, and latency is measured on a real Pub/Sub
API (emulator). Scaling past 10K is Phase 14 work; the 10K slowdown is recorded above, not hidden.
