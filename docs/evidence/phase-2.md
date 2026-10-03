# Phase 2 Evidence Report

```text
Phase: 2 - Data Platform and External Signal Ingestion
Date: 2026-10-03
Code revision: c455633 (HEAD) + uncommitted working tree (no commits made, per policy)
Environment: Linux, Python 3.12.14, numpy 2.5.3, duckdb 1.5.6, dbt-core 1.12.5, dbt-duckdb 1.11.0
Simulator events are SYNTHETIC. All four external sources are real and were verified live (see Live
verification); the FRED and EIA test fixtures are still hand-written to the documented shape.
```

## Tests executed

`make check` (ruff, ruff format, mypy strict, pytest with coverage, schema sync, secret scan, frontend,
terraform validate) exits 0. Pytest: **391 passed, 1 skipped** (239 from Phases 0-1 as regression, 152 new; the skip is the live BigQuery
module, which runs only via `make bq-verify`).
Coverage 98.6% (new `praxis.data`: ingest 100%, raw_store 100%, freshness 100%, sources 97-100%, fetch 93%).
Docs checked first (CLAUDE.md s21): Open-Meteo archive, NESO Carbon Intensity, FRED observations, EIA v2.

| Required (required_test.md section 8) | Evidence |
| --- | --- |
| Source contracts: valid parse, required fields, optional fields | `test_sources.py`: all four sources, real captured Open-Meteo and NESO bodies |
| Schema change visible (drift) | 19 mutation cases + garbage bodies per source raise `SchemaDriftError`; ingest quarantines the raw batch, produces no rows, records `quarantined` |
| Timeout, rate limit, bounded retry | `test_fetch.py`: exactly `max_attempts` calls, capped backoff, capped `Retry-After`, no sleep after last attempt, 4xx not retried |
| Idempotency | Same batch twice per source: warehouse, archive, batch table unchanged; changed body = new batch, revises by logical key; crash between archive and warehouse heals on rerun |
| Freshness | `test_freshness.py` fresh / stale / missing / exact boundary / future forecast slot cannot hide staleness; dbt `source freshness` passes when fresh and fails when stale |
| Raw replay | `replay()` with a transport that fails on any request rebuilds byte-identical rows (incl. original `retrieved_at`) into an empty warehouse |
| dbt categories | unique, not_null, relationships, accepted_values, freshness, 11 singular invariants; 92 nodes (20 models + 72 tests) all pass |
| Negative controls | 8 corruptions (duplicate payment, unknown event type, orphan FK, humidity 150, future-dated signal, amount mismatch, non-synthetic event, usage after churn) each fail the intended dbt test; leakage test fails under a longer lag; each control asserts it modified rows; 3 stable runs |
| BigQuery partition filter | Static only: every fact is partitioned with `require_partition_filter`, every read of a fact bounds the partition column, checker has a negative control. **No dry run was possible** (no BigQuery) |
| Gate: one command builds the local path | `make data-dev` |
| Gate: ingestion idempotent | Also confirmed live: second `ingest` = 15 duplicate, 0 stored, 0 new records |

## Performance metrics

Budget fixed before running: whole data path <= 300 s, peak RSS <= 4 GB.

| Run | Result |
| --- | --- |
| `make data-dev` live (1K x 56d, 15 real API requests, 2 sources skipped for no key) | 16.6 s wall, 503 MB peak RSS; 191,057 events, 38,978 signal rows, 15 batches, dbt 92/92 |
| 10K x 28d, load raw | 977,754 events (matches Phase 1) in 3.9 s, 1.65 GB peak RSS |
| 10K x 28d, `dbt build` | 5.3 s, 773 MB; 706,568 usage-day rows; DuckDB file 197 MB |
| Raw archive after live run | 984 KB for 15 batches |

## Live verification (FRED and EIA, after keys were added)

Run 2026-10-03, a few small requests per source, strict mode, no load testing.
* FRED (CPIAUCSL, FEDFUNDS, PPIACO, 2025-06..2026-03): 3 batches stored, 29 rows, 0 quarantined/rejected. CPI batch
  `partial` (1 genuine `"."` missing month, skipped and counted). Values plausible (CPI 321-330, fed funds 3.6-4.3%).
* EIA (NY and CAL hourly demand, 2026-01-05..11): 2 batches, 336 rows (168 each), `ok`. Respondent codes `NY` and
  `CAL` confirmed. Demand 14-22 GWh (NY), 25-34 GWh (CAL).
* Re-ingest of both: 5 duplicate, 0 stored, 0 new records.
* The key appears in no archive file, warehouse row or stored endpoint (`api_key=[REDACTED]`).
* Full `make data-dev` with all four sources: 22 batches, 41,675 rows, dbt 92/92. US regions get EIA demand on 56/56
  days; macro features start 2026-01-05 using the 2025-11-01 observation (Dec value only usable from 2026-01-15).
  Freshness reports all four stale (backfilled historical window), as designed.

## BigQuery readiness, option A (offline static check, added after the first PASS)

No credentials, network or cost: `dbt compile --target bigquery` with a throwaway generated key, then sqlglot
(bigquery dialect) over all 92 compiled nodes (`tests/data/test_bigquery_static.py`, 10 tests incl. negative controls).
* Every node parses as BigQuery; no non-native type names; every column and table resolves against the schema built by
  the DuckDB run; every scan of a partitioned fact bounds its partition column; dataset names follow Terraform.
* **Found and fixed (would have failed in BigQuery):** `cast(... as varchar)` and `as double` (now dispatched type macros);
  the `raw` dataset missing from Terraform (added); source schema prefix not rendered; and **32 dbt tests (28 generic,
  4 singular) scanned partitioned facts with no date bound**, which `require_partition_filter` rejects. Fixed with a
  `get_where_subquery` override plus explicit all-time bounds (tests still see every row; they scan all partitions).
* Still NOT proven: execution, JSON function behaviour, load path, partition pruning, bytes scanned. Option B (real
  dry run in a new `praxis-dev` project) is pending approval.
* Checker limit: a date predicate on any same-named column counts as a bound.

## BigQuery live verification, option B (2026-10-03)

Project `praxis-dev-510522`, **BigQuery sandbox: billing unlinked and verified `billingEnabled: False`**, so Google caps
cost at zero; every real query also sets `maximum_bytes_billed` = 1 GB. Command: `make bq-verify BQ_PROJECT=...`.
Data: 1K customers x 42 days ending 7 days ago (sandbox expires partitions older than 60 days), live external signals.

| Check (pre-registered in `tests/data/test_bigquery_live.py`) | Result |
| --- | --- |
| Raw load (free load jobs, `WRITE_TRUNCATE`, row counts verified) | 5 tables; 146,034 events, 31,254 signals loaded |
| `dbt build --target bigquery` | 92/92 pass (20 models, 72 tests) |
| Physical layout | every fact DAY-partitioned on its date column, clustered, `require_partition_filter = true` |
| Unfiltered query on any fact | rejected by BigQuery (5/5) |
| Partition pruning, one day x 10 <= all | usage 193,724 vs 8,325,973 B; service 12,552 vs 527,184 B; signals 430 vs 6,312,706 B |
| Parity with DuckDB (counts + integer sums, 9 marts) | exact match on every mart |
| Feature view float features | equal within 1e-9 relative |
| Re-load idempotent | raw counts unchanged |

Total: 28 live tests pass, all on the second run (see failures 7 and 8).

## Statistical metrics

None claimed. Sanity only: Jan-Feb feature means were physically plausible (Singapore 26.2 C, Frankfurt 2.9 C,
London 7.0 C, New York -2.8 C). The mapping of simulated regions to real places is an assumption, not a finding.

## Failures found

1. httpx logs full request URLs at INFO, so an API key in the query string reached logs (caught by a test).
2. Open-Meteo `generationtime_ms` differs on every response, so each re-fetch became a new batch (found by the live
   re-run: 25 batches instead of 15). No logical duplicates, but archive growth.
3. Two dbt invariants of mine contradicted the payload contract: `utilization <= 1.5` (contract: unbounded, overload is
   modelled) and `throttled <= units` (contract: throttled is the rejected remainder). Corrected to the contract
   before any other change; the data and thresholds were not tuned to pass.
4. Test defects: EIA fixture served NY rows to the CAL request (the facet check correctly quarantined it); a negative
   control modified no rows; one control used an unordered `LIMIT 1` and was flaky.
5. `.gitignore` rule `data/` also ignored `src/praxis/data`, `tests/data`, `configs/data`.

6. Because of that rule, `ruff check .` and the secret scan had silently skipped `src/praxis/data` and `tests/data`
   in the first gate run (pytest and mypy ran them). After the fix: 9 long lines and one false-positive secret match
   (a dummy-key helper) were found and fixed; `make check` re-run, exit 0.

7. First live run: 3 FRED rows dated 2026-08-01 (63 days old) were accepted by the load, then dropped by the sandbox's
   60-day partition expiration, so external-signal parity was 31,251 vs 31,254. Documented sandbox behaviour, not a
   pipeline bug. The live test was amended (reason in its header): parity on partitions inside the retention window
   read from BigQuery, plus a stricter check that BigQuery holds nothing older and that the expired rows are the only
   gap. The loader now reports `expiring_rows` and warns before BigQuery silently drops data.
8. My idempotency test opened the DuckDB file read-write while another fixture held it; it now reloads from a copy.
   The first pruning check for signals measured the expired partition (0 bytes, trivially true); it now picks a day
   inside retention and requires non-zero bytes.

## Fixes

1. httpx logger redaction filter installed by `HttpFetcher`; `configure_logging` quiets httpx/httpcore.
2. `Source.fingerprint` strips volatile fields for `batch_id`; raw bytes remain untouched. Test added.
3. Invariants rewritten to the contract and the reason recorded in the SQL.
4. Fixture handler echoes the requested respondent; controls assert modified rows and use `ORDER BY`.
5. `.gitignore` changed to `/data/` at the user's request.

## Known limitations

* FRED and EIA live-verified only by this manual run; no automated test calls a live API (by design). Fixtures for
  both remain hand-written.
* BigQuery verified only in the sandbox at 1K x 42 days; the sandbox expires tables and partitions after 60 days, so the
  datasets are disposable and `make bq-verify` rebuilds them. No DML, so no incremental/MERGE models yet. No GCS staging.
* No BigQuery tables, GCS upload, ONS or World Bank backfill. Terraform datasets unchanged; no resource applied.
* Pricing "decision" facts do not exist yet; `fct_price_exposures` holds prices shown plus experiment arm.
* `make data-dev` ingests the simulator's date range, so `freshness` reports it stale by design (informational).
* Open-Meteo's free tier is for non-commercial use; review terms before any commercial use.
* Latest-vintage-wins for revised values; earlier vintages live only in the raw archive.

## Cloud cost incurred

GBP 0. Only cloud resources: BigQuery sandbox project `praxis-dev-510522` (billing disabled) with 3 datasets of about
60 MB that expire within 60 days. Load jobs and dry runs are free; queries totalled well under the 1 TiB free tier.
About 60 small requests to free public APIs across all runs, no load testing.

## Gate

PASS. The BigQuery caveat is closed: execution, partition enforcement, pruning and parity verified live in the sandbox.

## Reason

Both plan gates are met and evidenced: one command builds the local data path, and ingestion is idempotent (unit,
integration and live). Every required section 8 category has passing tests with negative controls, including the
BigQuery partition-filter and dry-run checks, now verified against a real (sandbox) BigQuery.
