# Deployment preflight report (2026-11-01)

Written before the first real `terraform apply`. Inputs are read-only `gcloud` inspection,
the saved stage-1 plan, and Google's pricing pages read today. **Every cost below is an
ESTIMATE from list prices, not a measured bill.** All data is SYNTHETIC.

## 1. Target

| Item | Value | How verified |
| --- | --- | --- |
| Account | the owner's Google account (active `gcloud` credential) | `gcloud auth list` |
| Project | `praxis-dev-510522` ("praxis-dev", number 287014189565) | `gcloud projects list`; named as the Praxis project in `docs/cost-guard.md` since Phase 2. No ambiguity: the only other project is `pricepoint-dynamics`, an unrelated application |
| Active `gcloud` project | `pricepoint-dynamics` (NOT Praxis) | `gcloud config list`. Every command and Terraform call passes the Praxis project explicitly; the default was not changed |
| IAM | owner: `roles/owner` on the project, `roles/billing.admin` on the billing account | `get-iam-policy` |
| Billing | was **disabled** (BigQuery sandbox). Linked to billing account `017068-C487C3-C0F5A8` on 2026-11-01 (the first cloud change; no cost by itself) | `gcloud billing projects describe` |
| Region | **us-central1** for compute, registry, secrets, storage and tasks. BigQuery stays in **europe-west2** (the existing Phase 2 datasets; moving a dataset destroys it) | ADR 0026 |

Why us-central1, not europe-west2 (the Phase 0 default):
* The Free Tier e2-micro VM exists only in us-central1, us-west1 and us-east1.
* Cloud Storage's free 5 GB-months applies only to us-east1, us-west1 and us-central1.
* Cloud Run's free tier is quoted at us-central1 (tier 1) prices.
* Co-locating Cloud Run with the database VM keeps every database round trip in-region.

The data is synthetic, so UK residency is not a compliance requirement. This is recorded as
a trade-off in ADR 0026.

## 2. Existing consumption (the free tiers are per BILLING ACCOUNT, so shared)

| Project | What runs there | Effect on Praxis |
| --- | --- | --- |
| `pricepoint-dynamics` | 2 Cloud Run services (europe-west2), 1 Cloud Function (`budget-killswitch`), Artifact Registry about 666 MB, 0 VMs, 0 static IPs | Artifact Registry's free 0.5 GB is **already used up**, so every Praxis image GB is billed (USD 0.10/GiB-month). Cloud Run's free tier is shared. No e2-micro hours are in use, so the Free Tier VM is available |
| Account-wide budgets | "Maximum Budget on Sandbox" GBP 10 (whole account, email); "PricePoint monthly cap" GBP 10 (+ kill switch, PricePoint only); "funding-paper-trading-15gbp" GBP 15 (PricePoint only) | The account-wide GBP 10 email budget also counts Praxis spend |
| **Billing consumption to date** | **Not measurable from the CLI.** There is no billing export dataset, and the Budgets API does not return spend. | **Unverified**: check the Billing console "Reports" page. The budget alerts below are the measured signal from now on. |

## 3. Quota verification (the actual Praxis project)

`gcloud alpha services quota list --service=bigquery.googleapis.com --consumer=projects/praxis-dev-510522`:
* `bigquery.googleapis.com/quota/query/usage`: unit **`MiBy`**, limit unit `1/d/{project}`,
  default and effective limit **209,715,200** (= 200 TiB/day).
* So `override_value = 10240` = 10 GiB/day, as the module assumes; this confirms on the
  target project what was verified on another project in Phase 15.

Cloud Run instance caps (API 2, console 1, spend guard 1) are far below the default regional
quotas. Each job is 1 task with no retries.

## 4. Resource inventory, plan summary and expected monthly cost

Stage 1 (saved plan, `terraform show deploy.tfplan`): **71 to add, 3 to change (the three
imported Phase 2 datasets: labels + `delete_contents_on_destroy`; table expiry unchanged),
0 to destroy.**

Stage 2 (`deploy_services = true`, after the images are pushed) is planned and reviewed
separately before its apply.

Prices: USD list prices read 2026-11-01 from cloud.google.com pricing and Free Tier pages.
Conversion: **GBP 0.80 per USD** (conservative; the latest dated rate found was 0.747, from
July 2026), **plus 20 % UK VAT**, so GBP = USD x 0.96.

| Resource (stage) | Count | Free Tier / eligibility | Expected use | Expected GBP / month |
| --- | --- | --- | --- | --- |
| Project APIs (1) | 15 | free | n/a | 0 |
| VPC, subnet (Private Google Access), 2 firewall rules (1) | 4 | VPC and firewall rules have no charge; no rule logging | n/a | 0 |
| Artifact Registry repo, keep 3 versions + delete older than 7 d (1) | 1 | account free 0.5 GB already used by PricePoint | about 0.6 to 1.2 GiB (app 372 MB, console UI 94 MB, postgres, compressed) | 0.06 to 0.12 |
| Cloud Storage raw bucket, us-central1 (1) and state bucket (bootstrap) | 2 | 5 GB-months free (US regions only) | KBs | 0 |
| Pub/Sub: 3 topics, 6 subscriptions, IAM (1-2) | 9 | 10 GiB/month throughput free | under 0.1 GiB (smoke and budget notifications) | 0 |
| BigQuery: 3 datasets (imported), 10 GiB/day query quota (1) | 3 | 10 GiB storage + 1 TiB query/month free (shared) | about 60 MB stored; only the bounded live check queries | 0 (cap: 310 GiB/month; if PricePoint had used the whole TiB, up to 1.81) |
| Cloud Tasks queue (1) | 1 | 1 M operations free | 0 tasks (no Stripe) | 0 |
| 7 service accounts, 12 secret grants (1) | 19 | free | n/a | 0 |
| Secret Manager: 7 secrets, 3 versions added (database-url, console-approvers, postgres-password) (1) | 7 | 6 active versions + 10,000 accesses free | 3 versions; under 1,000 accesses | 0 |
| Budget GBP 10 (alerts GBP 2/5/8/10 + forecast), BigQuery quota override (1) | 2 | free | n/a | 0 |
| e2-micro VM (COS, Shielded, no external IP), internal static IP (2) | 2 | **Free Tier: 1 e2-micro month per billing account in us-central1**; internal IPs free | 730 h | 0 (5.87 if the Free Tier did not apply: risk R1) |
| 10 GB boot + 10 GB data pd-standard (2) | 2 | 30 GB-months free | 20 GB | 0 |
| Daily snapshots of the data disk, 7-day retention, regional (2) | 1 policy | none | under 1 GB incremental | about 0.05 |
| Cloud Run services: api (private), console (UI + API sidecar), spend-guard (2) | 3 | 180,000 vCPU-s, 360,000 GiB-s, 2 M requests free (request-based) | owner use + smoke, under 10,000 vCPU-s | 0 |
| Cloud Run jobs: ops, streaming (2) | 2 | instance free tier, 240,000 vCPU-s | under 50 executions of under 2 min | 0 |
| Spend guard push subscription + topic (2) | 2 | Pub/Sub free tier | a few notifications a day | 0 |
| Cloud Logging / Monitoring | n/a | 50 GiB logs per project free; default retention free; Google metrics free | under 1 GiB | 0 |
| Network | n/a | no external IP, NAT or load balancer; egress to the internet is only API responses to the owner | under 1 GB | 0 |

**Total expected: GBP 0.10 to 0.20 per month** (registry storage + snapshots), inside the GBP
0-5 target. Image builds run locally (no Cloud Build minutes).

**Plausible bad case: about GBP 8** if every risk below materialised at once (the VM not on
the Free Tier, the BigQuery free TiB already used by PricePoint and Praxis querying its full
daily cap every day, a free tier exhausted by PricePoint). That is still under the GBP 10
ceiling, and the spend guard acts at GBP 8.

## 5. Spending safeguards

| Safeguard | Enforced? |
| --- | --- |
| Budget GBP 10 on Praxis, gross cost (credits EXCLUDED, so no promotional credit hides spend), email at GBP 2 / 5 / 8 / 10 actual and 100 % forecast | Notification |
| **Spend guard**: the budget's Pub/Sub feed goes to a private Cloud Run service, which **unlinks billing at GBP 8 actual** (Google's documented pattern; stops every resource) | Yes, automatic. Billing data lags by hours, so this is a backstop |
| No public endpoint at all (the API is private until the Stripe integration, `api_public`). Requests denied by IAM are not billed | Yes |
| Cloud Run min 0, request-based CPU, max instances 2 / 1 / 1, timeout 30 s; jobs 1 task, 0 retries, 5 to 10 min timeout | Yes |
| BigQuery 10 GiB/day query quota (unit verified on this project) | Yes (approximate per Google) |
| Cloud `produce` capped at 2,000 customer-days per execution; no scheduler (every job run is an owner action) | Yes |
| Registry cleanup policy; 7-day snapshot retention; 24 h Pub/Sub retention | Yes |
| No NAT, external IP, load balancer, Cloud SQL or reserved public address (pinned by `tests/security`) | Yes |

## 6. IAM and security validation (configuration; re-checked on the project after apply)

* One service account per process (api, console, streaming, ops, agent, db, guard).
  Every grant is on a single resource, with one classified exception: the guard's
  `roles/billing.projectManager` on the project, the narrowest role that can unlink
  billing (ADR 0026). No keys. No primitive roles.
* Secret readers: database-url by api / console / streaming / ops / agent;
  console-approvers by console; postgres-password by db only; the Stripe and LLM secrets
  have **no versions** (no Stripe operation, no LLM call).
* Public: nothing. Console and API invokers: the owner only. The guard's invoker: its own
  account (the push OIDC identity).
* The database has no external IP. Postgres (scram-sha-256) is reachable only from the
  private subnet (VPC firewall + host iptables). SSH is only through IAP + OS Login.
* Images: allowlist `.dockerignore` (no `.env`, tfvars, state or credentials), non-root
  user, deployed by digest.

## 7. Deployment dependencies (order)

1. Link billing; enable Resource Manager (done, free).
2. Versioned private state bucket `gs://praxis-dev-510522-tfstate` (done).
3. Import the 3 Phase 2 datasets (done; the in-place changes are listed above).
4. **Stage 1 apply.**
5. Add the 3 secret versions out of band (never in Terraform state).
6. Build, push and mirror the images; record the digests.
7. **Stage 2 plan, review, apply.**
8. Migrations job, then a bounded smoke (`make cloud-smoke`).

## 8. Remaining financial and operational risks

* **R1.** The Free Tier is a Google promise per billing account. If another e2-micro ever
  runs on this account, Praxis's VM costs about GBP 5.87/month.
* **R2.** Budget and billing data lag by hours to a day. The guard acts on reported cost,
  not real time; at this design's maximum burn rate the lag is worth pence.
* **R3.** Unlinking billing stops everything, and Google warns resources may be deleted.
  This is accepted for a synthetic, Terraform-reproducible environment; the state bucket
  and the images could also be affected.
* **R4.** The BigQuery quota is approximate; the free TiB is shared with PricePoint.
* **R5.** The database is a single VM: no high availability. Recovery is a snapshot restore
  (the RPO is up to 24 h of synthetic data).
* **R6.** Existing billing consumption could not be read from the CLI.
* **R7.** The FX rate is not current (conservative 0.80 used).
