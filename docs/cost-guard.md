# Cost Guard

Target: roughly £0 to £5 per month for development and demonstration. **Owner ceiling
(2026-11-01): £10 per month for the whole project.** Budget alerts are notifications, not
hard caps; the spend guard (ADR 0026) is the automatic shutdown.

> **Deployed 2026-11-01** to `praxis-dev-510522` (ADR 0026). For the live inventory, costs
> and safeguards see "Deployment (ADR 0026)" at the end of this file,
> `docs/evidence/deployment-preflight.md` and `docs/deployment.md`. Sections below are kept
> as the history of each phase.

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
| GitHub Actions | Running since Phase 1 (`ci.yml` per push, `nightly.yml` daily) | £0: public repository, standard GitHub-hosted runners are free |

## Before provisioning anything

State what is created, why, expected monthly cost, the cap or quota set, and how to
destroy it. Wait for approval.

## Phase 2 BigQuery

| Resource | State | Cost control |
| --- | --- | --- |
| Project `praxis-dev-510522` | BigQuery sandbox, billing disabled (verified) | Google caps at zero; sandbox limits 10 GiB storage, 1 TiB query/month |
| Datasets `praxis_dev_{raw,staging,marts}` | about 60 MB, expire after 60 days | `maximum_bytes_billed` 1 GB per query in profile and tests; free load jobs and dry runs |

Linking billing to this project is a spend decision for the user, not an implementation step.

## Phase 3 event backbone

| Resource | State | Cost control |
| --- | --- | --- |
| Pub/Sub topics / 4 subscriptions + DLQ IAM | Declared in Terraform only, never applied | Emulator for all development and CI; 24h retention; subscriptions never expire |
| Postgres control plane | Local Docker container (`make pg-up`) | No cloud database; Supabase or Cloud SQL is a later, approved step |
| Emulator / Postgres in CI | GitHub-hosted service containers | £0 |

`ensure_topology` refuses to create Pub/Sub resources without `PUBSUB_EMULATOR_HOST`:
cloud topology is Terraform's job, never a side effect of running code.


## Phase 15 cloud posture and demo-mode estimate (ADR 0024)

Nothing below is applied. `make tf-test` proves plan / apply / destroy against mocked
providers at GBP 0; a real apply or `make cloud-destroy CONFIRM=destroy-<project>` is an
owner action.

| Guard | Where | Enforced? |
| --- | --- | --- |
| Cloud Run min 0, request-based CPU, max instances 1..10 (dev: API 2, console 1), timeout <= 60 s | `modules/cloud_run` (validated) + `Settings.cloud_run_max_instances` | Yes (instance cap) |
| Monthly budget GBP 5 (validated <= 5), alerts at 50/90/100 % actual and 100 % forecast | `modules/cost_controls` (needs `billing_account`) | No: notification only, Google keeps billing |
| BigQuery `QueryUsagePerDay` 10 GiB/day (31 days <= 1 TiB free tier) | `modules/cost_controls` (google-beta) | Yes, approximately (Google: queries can overshoot) |
| Services declared only with `deploy_services = true` | `envs/dev` | Yes |

Prices checked 2026-11-01 on the official pages (USD; billed in GBP at Google's SKU rates):

| Service | Free tier per month (billing account) | Beyond | Source |
| --- | --- | --- | --- |
| Cloud Run, request-based | 180,000 vCPU-s, 360,000 GiB-s, 2 M requests | per vCPU-s / GiB-s by region tier | [cloud.google.com/run/pricing](https://cloud.google.com/run/pricing) |
| BigQuery on-demand | 1 TiB queries (sandbox: 10 GiB storage) | USD 6.25 / TiB | [cloud.google.com/bigquery/pricing](https://cloud.google.com/bigquery/pricing) |
| Pub/Sub (Message Delivery Basic) | 10 GiB throughput | USD 40 / TiB | [cloud.google.com/pubsub/pricing](https://cloud.google.com/pubsub/pricing) |
| Secret Manager | 6 active versions, 10,000 accesses | USD 0.03 / 10,000 accesses | [cloud.google.com/secret-manager/pricing](https://cloud.google.com/secret-manager/pricing) |
| Cloud Tasks | 1 M billable operations | USD 0.40 / M | [cloud.google.com/tasks/pricing](https://cloud.google.com/tasks/pricing) |

Demo-mode estimate (one owner, a few hundred requests a day, dev-size data):

| Item | Expected use | Estimate |
| --- | --- | --- |
| Cloud Run API + console | well under 1 % of the free tier | GBP 0 |
| Pub/Sub | < 1 GiB | GBP 0 |
| BigQuery | < 10 GiB stored, capped 10 GiB/day queried | GBP 0 |
| Secret Manager | 6 secrets x 1 version = the free 6 | GBP 0 (a 7th version costs ~USD 0.06/month) |
| Cloud Tasks | < 10,000 operations | GBP 0 |
| GCS raw archive, Artifact Registry images | < 1 GB each | under GBP 0.20 (not re-verified this phase) |
| Postgres control plane | local / free-tier host | GBP 0; a managed Cloud SQL instance would exceed the GBP 5 target and is not declared |

Total: about GBP 0 to 0.20 a month, inside GBP 0-5. This is an **estimate from list
prices, not a measured bill**: nothing was deployed. Open items: confirm the BigQuery quota
unit (MiB/day) on the first apply with `gcloud alpha services quota list`; the free tiers
are per billing account, so other projects on the same account consume them too.

### Pre-apply checklist (Phase 15 remediation, 2026-11-01): blocking for any real apply / destroy

Nothing was applied, destroyed or deployed in Phase 15 or its remediation. Before the
owner approves any real infrastructure operation:

1. **BigQuery quota unit: verified MiB per day (2026-11-01, Phase 15 optimisation).**
   * Read-only inspection, no change made: `gcloud alpha services quota list
     --service=bigquery.googleapis.com --consumer=projects/<id>
     --filter="metric:bigquery.googleapis.com/quota/query/usage"` on an existing project
     returned metric unit **`MiBy`**, limit unit `1/d/{project}` (the module's
     `limit = "/d/project"`) and default / effective limit `209715200`.
   * Google's `MetricDescriptor.unit` grammar
     ([reference](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.metricDescriptors))
     defines `By` = byte and `Mi` = mebi (2^20), so `MiBy` = MiB.
   * [Quotas and limits](https://docs.cloud.google.com/bigquery/quotas) documents the
     default of `QueryUsagePerDay` as 200 TiB per project per day, and 200 x 2^20 =
     209,715,200: the listed default agrees only with MiB. `override_value =
     bigquery_daily_query_mib` (10,240 = 10 GiB/day) is therefore in the right unit.
   * Remaining assumptions:
     * The unit is a service definition, not per project; it was read on an existing
       project, not the Praxis one, which does not exist yet.
     * The quota is approximate, not a hard cap ("BigQuery might occasionally run a query
       that exceeds a quota"), resets at midnight Pacific Time and covers on-demand pricing
       only ([custom quotas](https://docs.cloud.google.com/bigquery/docs/custom-quotas)).
   * On the first approved apply, re-run the listing on the Praxis project and expect
     `effectiveLimit` = `10240`.
2. **IAM and secrets.**
   * Review `terraform plan`: no project-level or primitive role, no key, `allUsers` only on
     the API service, and one accessor per secret for the named processes.
   * `tests/security/test_terraform_posture.py` enforces this, but only on the
     configuration, not on the project.
3. **Destroy / recreate.**
   * `make tf-test` proves the graph with mocked providers only.
   * `make cloud-destroy` needs `CONFIRM=destroy-<project>` and is never run by CI.
4. **Cost.**
   * Re-read the list prices on the day.
   * The budget alert is a notification, never a spending cap.
   * Cloud Run `max_instance_count` ≤ 10 and the BigQuery quota are the only enforced
     limits, and the quota is approximate.


## Deployment (ADR 0026, 2026-11-01): live safeguards

| Guard | Where | Enforced? |
| --- | --- | --- |
| Budget **GBP 10** on Praxis, **gross** cost (credits excluded); email at GBP 2 / 5 / 8 / 10 actual + 100 % forecast | `modules/cost_controls` | Notification |
| **Spend guard: unlinks billing at GBP 8 actual** (budget topic -> OIDC push -> `praxis.api.spend_guard`) | `modules/spend_guard` | Yes (acts on reported cost, which lags by hours) |
| Nothing public (API private until Stripe: `api_public`); IAM-denied requests are not billed | `envs/dev`, `tests/security` | Yes |
| No external IP, NAT, load balancer, Cloud SQL or public address | `tests/security/test_terraform_posture.py` | Yes |
| Database = Free Tier e2-micro, 10 GB boot + 10 GB data (<= 30 GB free), us-central1 only | `modules/database` (validated) | Yes |
| Cloud Run min 0 (service and template), request-based CPU, max 2 / 1 / 1, timeout 30 s; jobs 1 task, 0 retries, <= 10 min (the live job <= 4 h) | `modules/cloud_run`, `modules/cloud_run_job` | Yes |
| BigQuery 10 GiB/day; unit re-verified on `praxis-dev-510522` (`MiBy`, default 209,715,200) | `modules/cost_controls` | Yes (approximate) |
| Cloud `produce` <= 2,000 customer-days; never scheduled; every streaming job run is an owner action | `praxis.streaming.__main__` | Yes |
| Live loop (ADR 0027): Scheduler runs `praxis-dev-live` once a day and `praxis-dev-ops` once a day, nothing else (<= 3 triggers, daily cron only, no retries); live job 4 vCPU / 8 GiB, <= 4 h (amendments 1-2); live bucket versioned, noncurrent versions deleted after 2 d | `modules/schedule`, `modules/storage`, `tests/security` | Yes |
| Registry keeps 3 versions + deletes older than 7 d; snapshots kept 7 d; Pub/Sub retention 24 h | modules | Yes |

The free tiers are per billing account. Artifact Registry's free 0.5 GB is already used by
another project on the same account, so Praxis images are billed (about USD 0.06 to 0.12 a
month). The expected total is GBP 0.10 to 0.20/month: an **estimate**; see the preflight
report for every line and risk.
