# Cost Guard

Target: roughly £0 to £5 per month for development and demonstration. Budget alerts are
notifications, not hard caps.

## Rules

1. Nothing is applied to GCP without explicit user approval. Phase 0 only runs
   `terraform fmt` and `terraform validate` (no credentials, no spend).
2. Develop against local emulation first; use scale-to-zero services.
3. Every Cloud Run service sets `max_instances` (default cap 2, hard cap 10 in
   `Settings.cloud_run_max_instances`).
4. BigQuery: partition large facts, dry-run before expensive queries, short default
   partition expiration in dev.
5. Pub/Sub retention is short (24h); GCS is the replay source.
6. Bulk history lives in GCS / BigQuery, never Postgres.
7. Keep the Stripe cohort small. Never load test Stripe Sandbox or public free APIs.
8. Run scale simulations locally first; progress 1K, 10K, 100K, 1M only with measurements.
9. Verify current GCP pricing and free tiers before provisioning; they change.

## Phase 0 estimate

| Resource | Provisioned in Phase 0? | Expected cost |
| --- | --- | --- |
| GCS raw bucket | No (declared only) | Free tier at dev volumes |
| Pub/Sub topics / subscriptions | No (declared only) | Free tier at dev volumes |
| BigQuery datasets | No (declared only) | Free tier at dev volumes |
| GitHub Actions | Not pushed | £0 |

## Before provisioning anything

State what is created, why, expected monthly cost, the cap or quota set, and how to
destroy it. Wait for approval.

## Phase 2 BigQuery

| Resource | State | Cost control |
| --- | --- | --- |
| Project `praxis-dev-510522` | BigQuery sandbox, billing disabled (verified) | Google caps at zero; sandbox limits 10 GiB storage, 1 TiB query/month |
| Datasets `praxis_dev_{raw,staging,marts}` | about 60 MB, expire after 60 days | `maximum_bytes_billed` 1 GB per query in profile and tests; free load jobs and dry runs |

Linking billing to this project is a spend decision for the user, not an implementation step.
