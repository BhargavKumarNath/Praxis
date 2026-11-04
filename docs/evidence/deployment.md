# Deployment evidence (2026-11-01, ADR 0026)

Project `praxis-dev-510522`. Compute in us-central1, BigQuery in europe-west2. All data is
SYNTHETIC; there is no Stripe call, no LLM call and no real customer. Preflight (target, quota, costs, risks):
`deployment-preflight.md`. Runbook: `docs/deployment.md`. Machine-readable smoke:
`deployment-smoke.json`.

## 1. What was applied

| Apply | Result |
| --- | --- |
| Stage 1 (foundation) | 71 added, 3 changed in place (the imported Phase 2 datasets: labels, `delete_contents_on_destroy`), 0 destroyed |
| Stage 2 (runtime, `deploy_services = true`) | VM, disks, snapshot policy, 3 services, 2 jobs and the spend guard added. The first attempt failed on the console's sidecar probe: the sidecar bound to 127.0.0.1, so its TCP startup probe failed. Fixed to bind 0.0.0.0; the tainted, never-serving console was replaced (1 destroyed) |
| Rollout of the new app image | 0 added, 5 changed in place, 0 destroyed (images by digest) |
| Subnet flow logs (trivy GCP-0076 from `nightly-security`) | 0 added, 1 changed in place, 0 destroyed |
| Final `terraform plan -detailed-exitcode` | exit 0, "No changes. Your infrastructure matches the configuration." |

The state holds 93 resources:
* 15 APIs;
* 7 service accounts;
* 7 secrets with 11 reader grants;
* 3 topics and 6 subscriptions with 14 Pub/Sub grants;
* 3 Cloud Run services with 3 invoker grants, and 2 jobs;
* the VM, its data disk, an internal address and the snapshot policy;
* a VPC, a subnet and 2 firewall rules;
* the registry, the raw bucket, 3 datasets, the queue, the budget and the quota override;
* 1 project-level grant (the spend guard's).

Migrations ran as a Cloud Run job (`praxis.streaming migrate`) up to 0008. This proves VPC
egress, secret access and Postgres authentication from Cloud Run.

## 2. Smoke (`make cloud-smoke`): 46 / 46 PASS on the final run

| Area | Measured |
| --- | --- |
| Exposure | No `allUsers` / `allAuthenticatedUsers` binding on any service. Anonymous `/`, `/v1/ops/health` and `/v1/budget` get 403 from IAM on all 3 services |
| Spend guard IAM | The service policy's only invoker is `praxis-dev-guard`. A project Owner still gets through (200, `ignore` for an empty body) because `roles/owner` includes `run.invoker` (section 4) |
| API | Model `demand-hybrid-24c8b0b1de4d` loaded (checksum verified at start). Forecast is **503 `features_unavailable`, "latest feature day 2026-09-13 is 49 days old (limit 7)"**: the ADR 0010 freshness contract working against a real clock (the synthetic world ends 2026-09-13). `/v1/ops/health` 200; no OpenAPI schema; Stripe webhook 503 (unconfigured) |
| Console | UI 200; whoami 401 without a token and 200 `owner` with the approver token; approve with a wrong token 401; approve authorized on an unknown request 404; the executive panel 200 (Postgres over the private VPC) |
| Pub/Sub publish | 932 events published twice: the same synthetic 20 customers x 5 days, seed 7, a 100 customer-day cap of 2,000 |
| Delivery, acks and idempotency | **warehouse:** 1,864 deliveries, 1,864 acks, 0 dead-lettered, **932 distinct rows** (each event's 2 copies stored once). **monitoring:** 932 unique events, 932 duplicate deliveries detected. **operational:** 86 deliveries: 43 stateful events x 2 copies (its subscription filter is `attributes.stateful = "true"`), all `duplicate`, because an earlier run had already applied them to Postgres; idempotent across runs. A second drain of each subscription found 0 deliveries, so the acks held. The DLQ (read by the ops identity) is empty |
| Ops monitor | One tick: status `ok`, 0 alerts, 0 incidents, collectors dead_letters / revenue / warehouse `ok`. Every process snapshot is listed as stale, which is expected: services scale to zero and jobs exit, so no process keeps publishing |
| Spend guard | A synthetic notification (cost GBP 0.01) went through the real budget topic and the OIDC push subscription. The guard logged `hold` (budget `praxis-dev-monthly`, GBP 10); billing is still enabled afterwards. No notification at or above GBP 8 was ever sent |
| Secrets | Readers measured on each secret's IAM policy: database-url {agent, api, console, ops, streaming}; console-approvers {console}; postgres-password {db}; the Stripe secrets and task-token {api}; llm-api-key {agent}. All match the declaration |

## 3. Earlier smoke runs (failures kept, not hidden)

| Run | Outcome | Cause | Change |
| --- | --- | --- | --- |
| 1 | FAIL: `api /healthz` 404; then a crash parsing the produce report | The Cloud Run frontend reserves paths ending in `z` (a Google 404 page; the container never sees the request). The script also read the wrong log field | Probe `/v1/ops/health`. `/healthz` is recorded as expected-404 and documented in ADR 0026 and the runbook. Read the stdout log only |
| 2 | Stuck, then stopped | Cloud Run appends "Container called exit(0)." after the pretty JSON, so decoding failed and the script kept retrying | Decode the first JSON object from stdout entries only |
| 3 | 29 PASS, **1 FAIL**, then a crash | **FAIL:** the expectation "the owner cannot invoke the spend guard" (403) was wrong; the owner got 200 because project Owners inherit `run.invoker`. **Crash:** `gcloud run jobs execute --args` rejects a repeated value (`1`, `1`) | The check was redefined to what IAM actually guarantees (the service policy names only the guard account), and the Owner call is recorded as an observation. The ops tick uses the job's declared args. ADR 0026 is corrected. Disclosed here because the check changed after a failure: the original claim was a factual error about IAM inheritance, not a loosened threshold |
| 4 (final) | **46 / 46 PASS** | | |

Runs 1-3 published 932 events each time before stopping, so run 3's drains saw up to 5
copies per event: warehouse 4,660 deliveries, 932 rows; monitoring 3,728 duplicates. The
idempotency result was the same. Total cloud publishing across all runs was about 7,500
small messages, far inside the Pub/Sub free tier.

## 4. Findings and defects fixed during deployment

1. **Latent bug: the DLQ publisher grant was missing.** `DeadLetterRouter` publishes to
   the DLQ itself; streaming had no publisher grant on that topic. Added.
2. **The first real plan failed:** the Cloud Tasks enqueuer grant used `count` on an
   apply-time email. Now a statically keyed `for_each`.
3. **Mock test failure:** the guard invoker used `for_each` over an apply-time email. Now a
   static-keyed map. A mocked `terraform test` cannot catch this class of defect.
4. **Image defects found by the local image smoke before any push:**
   * `libgomp1` was missing (LightGBM import);
   * the wrong warehouse was baked in (it had no complete feature day);
   * the approver token in the local test was too short (correctly rejected).
5. **Console sidecar** bound to 127.0.0.1, so its startup probe failed. It now binds
   0.0.0.0, which is not externally routable.
6. **Perpetual plan drift** on service-level scaling. Fixed by declaring
   `scaling { min_instance_count = 0 }`.
7. **Owner inherits guard invoke:** documented (section 3). This is not an escalation:
   Owners can unlink billing directly.
8. **Not verified:** the console through `gcloud run services proxy`. The `cloud-run-proxy`
   component is not installed here (the component manager is disabled). The UI and API
   paths were verified with direct authenticated calls instead.
9. **Known gaps, not defects:**
   * there is no cloud pipeline producing fresh forecast features, so forecasts answer 503;
   * the dunning subscription has no consumer role in the CLI;
   * the VM's boot log is only in the serial console (no project-level log-writer role).

## 5. Local gates

* `make -k check`: run 1 exited 2. `.env.example` lacked the two new spend-guard settings,
  and one test had an untyped call. Both were fixed; run 2 **exit 0**: 2,073 passed,
  2 skipped (the opt-in live BigQuery checks), 7 contracts kept, coverage floors OK
  including the new `spend_guard.py` >= 95 (measured 96 %), `terraform test` 9 / 9.
* `make nightly-security`: run 1 exited 2 (GCP-0076, subnet flow logs, MEDIUM). Fixed by
  enabling flow logs (not ignored); run 2 **exit 0**.
* After the flow-log change, which only touched Terraform: `tf-check` (9 / 9, fmt,
  validate) and `tests/security` (21 passed) were re-run.
* Terraform tests run on a copy without the gitignored `deploy.auto.tfvars`, because
  `terraform test` auto-loads it and CI does not have it.

## 6. Cost: estimate vs measured

* **Measured:** nothing. There is no billing export, so the CLI cannot read spend. The
  budget alerts (GBP 2 / 5 / 8 / 10) and the Billing console are the measured signal from
  now on.
* **Measured usage inputs:**
  * registry 943 MB after the rollout (two app image versions);
  * VM e2-micro with no external IP;
  * 2 x 10 GB pd-standard disks;
  * Cloud Run: about 15 job executions of 2-4 minutes and a few hundred requests;
  * Pub/Sub: about 7,500 messages;
  * BigQuery: 0 queries run by the deployment.
* **Estimate:** GBP 0.10 to 0.25 / month, revised for the measured registry size. The
  keep-3 policy can hold about 1.5 GB, roughly GBP 0.15; snapshots about GBP 0.05;
  everything else is in the free tiers (preflight section 4). The plausible bad case is
  about GBP 8, and the spend guard acts at GBP 8.
