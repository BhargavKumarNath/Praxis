# Data platform (Phase 2)

```text
source API ──> raw archive (data/raw/<source>/<batch>.body + .meta.json, append-only)
                  │ parse + validate (pure, per-source adapter)
                  ▼
        DuckDB raw.external_signals / raw.ingest_batches        simulator NDJSON ──> raw.sim_events
                  │                                                                      │
                  └──────────────── dbt: staging (views) ────────────────────────────────┘
                                       │
                         marts: dim_region, dim_customer, dim_product,
                                fct_usage_daily, fct_payments, fct_price_exposures,
                                fct_service_metrics_hourly, fct_external_signals
                                       │
                              features: feat_region_daily
```

## Run it

```bash
make data-dev        # simulate -> load -> ingest external signals -> dbt build -> freshness
make dbt-build       # dbt only (92 models + tests on 1K x 56d)
make data-check      # pytest tests/data (includes dbt integration and negative controls)
make bq-verify BQ_PROJECT=praxis-dev-510522   # live BigQuery sandbox: load, dbt build, 28 checks
.venv/bin/python -m praxis.data {migrate|ingest|replay|load-sim|freshness} --help
```

Without `PRAXIS_FRED_API_KEY` / `PRAXIS_EIA_API_KEY` those sources are skipped and reported
(`missing_credential`); Open-Meteo and NESO Carbon Intensity need no key.
`make data-dev` ingests the simulator's own date range, so `freshness` reports it stale by design.

## Sources

| Source | Endpoint | Key | Grain | Limits handled |
| --- | --- | --- | --- | --- |
| Open-Meteo | `archive-api.open-meteo.com/v1/archive` | no | hourly x region | 31-day chunks, `timezone=GMT`, nulls skipped |
| NESO Carbon Intensity | `api.carbonintensity.org.uk/intensity/{from}/{to}` | no | 30-min, GB | 13-day chunks (API max 14), null `actual` skipped |
| EIA v2 | `api.eia.gov/v2/electricity/rto/region-data/data` | yes | hourly demand, NY and CAL | 30-day chunks, truncation = drift |
| FRED | `api.stlouisfed.org/fred/series/observations` | yes | monthly series | `"."` skipped |

Region-to-location mapping and freshness policies live in `configs/data/sources.toml`.
Hypothesised mechanism: weather and grid demand as regional cost/demand context, carbon
intensity as an operating-cost/sustainability variable, macro series as slow context. None is
claimed to be causal; Phase 4 must test each feature against a baseline.

## Provenance

`raw.ingest_batches` holds per batch: source, series ID, credential-free endpoint, retrieval
time, source timestamp range, schema version, SHA-256 checksum, HTTP status, quality status
(`ok` / `partial` / `quarantined`) and counts. Simulator batches carry `is_synthetic` via
`raw.sim_events.is_synthetic` (tested to be true); external data is real.

## Not done here

GCS upload, ONS and World Bank backfill, and anything beyond the BigQuery sandbox. See ADR 0008.
