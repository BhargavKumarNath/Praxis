# Data platform

The warehouse is DuckDB locally and BigQuery in the cloud, with the same dbt project on both.
Simulator events and real external data land in raw tables, dbt builds staging views, marts
and a regional feature view, and every model reads the marts, never the simulator.

```text
public APIs ──> raw archive (data/raw/<source>/<batch>.body + .meta.json, append-only)
                    │ per-source adapter: parse + validate
                    ▼
     raw.external_signals / raw.ingest_batches          simulator events ──> raw.sim_events
                    └───────────── dbt staging ─────────────┘
                                     │
          marts: dim_customer, dim_product, dim_region, fct_usage_daily, fct_payments,
                 fct_price_exposures, fct_service_metrics_hourly, fct_invoices, ...
                                     │
                         features: feat_region_daily
```

```bash
make data-dev       # simulate, load, ingest external signals, dbt build, freshness report
make dbt-build      # dbt only: 92 models and their tests on a 1K x 56-day world
make data-check     # the data tests, including dbt integration and negative controls
make bq-verify BQ_PROJECT=<project>   # the same build on the BigQuery sandbox (28 checks)
python -m praxis.data {migrate|ingest|replay|load-sim|freshness} --help
```

## External sources

| Source | Key needed | Grain | Handling |
| --- | --- | --- | --- |
| Open-Meteo archive | no | hourly per region | 31-day chunks, GMT, nulls skipped |
| NESO Carbon Intensity | no | 30 minutes, GB | 13-day chunks (API max 14) |
| EIA v2 grid demand | yes | hourly, NY and CAL | 30-day chunks; truncation is treated as drift |
| FRED | yes | monthly series | missing values (`"."`) skipped; a 45-day release lag is respected in features |

Without `PRAXIS_FRED_API_KEY` or `PRAXIS_EIA_API_KEY` those sources are skipped and reported as
`missing_credential`. Region mappings and freshness limits live in `configs/data/sources.toml`.

None of these signals is claimed to drive simulated demand, and in the forecast ablation they
gave no lift. They are here to exercise real ingestion: changing schemas, missing values,
rate limits and provenance.

## Provenance and quarantine

Every fetched response is archived unmodified, addressed by the SHA-256 of its content minus
volatile fields. `raw.ingest_batches` records, per batch, the source and series, a
credential-free endpoint, retrieval time, source timestamp range, schema version, checksum,
HTTP status and quality status (`ok`, `partial` or `quarantined`). A response that breaks its
source contract is archived and quarantined without producing rows. Simulator rows carry
`is_synthetic = true`, and a test checks it.

## Warehouse writes

`raw.sim_events` has no primary key (ADR 0028). An in-memory index on `event_id` was what ran
out of memory at 1M customers. Inserts append to `raw.sim_events_landing`, and a date-scoped
anti-join compaction moves first occurrences into the main table, the same path for the
loader and the streaming consumer. Readers that need every row call `flush()` first.

Loads are deterministic: service context is recomputed with exact DECIMAL sums, so rebuilding a
world from its seed gives a bit-identical panel and `data_version`.
