# ADR 0008: Local-first data platform (DuckDB + dbt), raw archive first

Status: Accepted (Phase 2)

## Context
Phase 2 needs raw -> staging -> marts -> feature views, external signal ingestion and dbt
tests. The target warehouse is BigQuery, but no cloud resources exist and the budget is
about GBP 0 to 5 per month (CLAUDE.md section 14).

## Decision
* **Local warehouse:** DuckDB file (`data/warehouse/praxis.duckdb`) stands in for BigQuery.
  The same dbt project runs on it via `dbt-duckdb`. Layers are schemas: `raw`, `staging`
  (views), `marts` (facts, dimensions, feature views). The BigQuery profile is not enabled.
* **Portable SQL:** payload extraction and date arithmetic go through `adapter.dispatch`
  macros. DuckDB is the only tested implementation; the BigQuery branches are written to the
  same contract and have never been executed.
* **Partitioning:** every fact is a date-partitioned, clustered table with
  `require_partition_filter`, declared inside `{% if target.type == 'bigquery' %}`
  (dbt-duckdb would read `partition_by` as an external-table option). Every model reading a
  fact bounds the partition column; static tests enforce it, including every dbt test (`get_where_subquery`
  override with explicit all-time bounds). No BigQuery dry run exists.
* **Offline BigQuery check:** `dbt compile --target bigquery` (throwaway key, no network) plus sqlglot over every
  compiled node verifies syntax, native types, column resolution and partition predicates. Dataset names are
  `<PRAXIS_BQ_SCHEMA_PREFIX><layer>` (raw, staging, marts) to match Terraform.
* **Raw archive first:** each fetched body is stored byte-for-byte, content-addressed and
  append-only, before parsing. `batch_id` = hash(source, request, fingerprint) where the
  fingerprint drops volatile fields (Open-Meteo `generationtime_ms`). The archive layout maps
  to GCS object keys.
* **Idempotency:** signals upsert on `record_key` (value excluded, so revisions overwrite;
  newer `retrieved_at` wins). Events insert on `event_id` and ignore duplicates. Batches
  upsert on `batch_id`. Replay (`praxis.data replay`) rebuilds normalised rows from the
  archive with no network.
* **Contract drift quarantines:** a response violating the source contract is archived,
  recorded with `quality_status = quarantined`, and produces no normalised rows. Skipped
  nulls mark a batch `partial`. Truncated EIA pages are drift, never silently complete.
* **Failure isolation:** retries are finite (3 attempts, capped backoff, capped
  `Retry-After`). An outage or missing key on one request is reported and the rest proceed;
  existing data is never modified by a failed fetch.
* **Look-ahead safety:** the feature view uses information available at the end of the
  feature day; FRED values apply only after `macro_release_lag_days` (45) and the observation
  used is exposed so a test verifies it.
* **Credentials:** keys come from `PRAXIS_FRED_API_KEY` / `PRAXIS_EIA_API_KEY`, are added at
  fetch time only and are redacted from stored endpoints and logs (httpx's own INFO log is
  filtered because it prints full URLs).

## Consequences
* Development and CI cost nothing and need no network except optional live ingestion.
* BigQuery is verified in a sandbox project (no billing): `bq-load` mirrors DuckDB `raw` via free load jobs
  (`WRITE_TRUNCATE`, schema from DuckDB, row counts checked, rows past partition expiration reported), dbt builds on
  BigQuery, and live tests check partition enforcement, pruning and exact parity with DuckDB.
* Sandbox limits shape the design: no DML (no MERGE/incremental models yet) and 60-day expiry (verification data
  must be recent and is disposable). Leaving the sandbox needs a billing decision and new cost controls.
* Simulated regions are mapped to real locations as context only (`configs/data/sources.toml`);
  the mapping is an assumption, not evidence that simulated customers sit there.
