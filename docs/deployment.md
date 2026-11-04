# Deployment runbook (GCP, ADR 0026)

Environment: project `praxis-dev-510522`, compute in `us-central1`, BigQuery in
`europe-west2`. All data is SYNTHETIC. Every command below is an owner action; CI never
deploys. Costs and safeguards: `docs/cost-guard.md`; evidence:
`docs/evidence/deployment-preflight.md` and `docs/evidence/deployment.md`.

```text
owner (gcloud identity) ──IAM──> Cloud Run: praxis-dev-api        (private, max 2)
                        ──IAM──> Cloud Run: praxis-dev-console    (UI + console-API sidecar, private, max 1)
                        ──exec─> Cloud Run jobs: praxis-dev-ops / praxis-dev-streaming
Cloud Scheduler 03:10 / 03:50 UTC ──> jobs praxis-dev-live (daily tick) / praxis-dev-ops
praxis-dev-live ──rw──> gs://praxis-dev-live-<project> (/mnt/live) <──ro── api, console, ops
Cloud Run ──Direct VPC egress (10.10.0.0/24)──> praxis-dev-db  e2-micro, COS, Postgres 17.11 @ 10.10.0.10
                                              (no external IP; Private Google Access)
Pub/Sub praxis-dev-events (+ DLQ) ──pull──> jobs (consume --cloud)
Budget GBP 10 ──> praxis-dev-budget-alerts ──OIDC push──> praxis-dev-spend-guard (unlinks billing at GBP 8)
BigQuery praxis_dev_{raw,staging,marts} (europe-west2, 10 GiB/day quota)
```

## One-time bootstrap (done 2026-11-01)
1. `gcloud billing projects link praxis-dev-510522 --billing-account=<id>`
2. `gcloud services enable cloudresourcemanager.googleapis.com --project=praxis-dev-510522`
3. State bucket: `gs://praxis-dev-510522-tfstate` (us-central1, versioned, uniform access,
   public access prevention, keeps 10 old versions).
4. Import the Phase 2 datasets (`terraform import 'module.bigquery.google_bigquery_dataset.layer["raw"]' projects/<p>/datasets/praxis_dev_raw`, and the same for `staging` and `marts`).
5. Write the gitignored `infra/terraform/envs/dev/deploy.auto.tfvars`: `project_id`,
   `billing_account`, `console_invokers = ["user:<owner>"]`, `deploy_services`, and the
   three image digests.

## Deploy or update
```bash
export CLOUD_PROJECT=praxis-dev-510522
make cloud-init                  # once per checkout (GCS backend)
make cloud-images                # build locally, push, print the 3 digests -> deploy.auto.tfvars
make cloud-plan                  # saves deploy.tfplan (+ deploy.tfplan.txt to review)
make cloud-apply                 # applies exactly that plan, then deletes it
gcloud run jobs execute praxis-dev-ops --region us-central1 --args=praxis.streaming,migrate --wait
make cloud-smoke                 # bounded verification -> docs/evidence/deployment-smoke.json
```
A first-time environment applies in two stages: `deploy_services = false` (the registry
must exist before images can be pushed), add the secret versions, then
`deploy_services = true`.

Secret values are added out of band and never enter Terraform:
```bash
printf '%s' "$VALUE" | gcloud secrets versions add praxis-dev-<name> --data-file=- --project=$CLOUD_PROJECT
```
Active versions: `database-url`, `postgres-password` and `console-approvers`
(`owner=<48-hex token>`). The Stripe, task-token and LLM secrets have no version, so no
Stripe or LLM call is possible. After rotating `postgres-password` you must also change the
role's password in Postgres; `initdb` reads the password only on first boot.

## Use it
* **Console UI (private):**
  `gcloud run services proxy praxis-dev-console --region us-central1 --project $CLOUD_PROJECT --port 3000`,
  then open http://localhost:3000. The approver token comes from
  `gcloud secrets versions access latest --secret=praxis-dev-console-approvers` (the part
  after `owner=`). Do not paste it anywhere else.
  * The proxy needs the `cloud-run-proxy` gcloud component (on apt installs:
    `sudo apt-get install google-cloud-cli-cloud-run-proxy`).
  * **Unverified (2026-11-01):** the component was not installed here, so the browser path
    is untested. In particular it is unknown whether the proxy replaces the UI's
    `Authorization: Bearer <approver>` header with its identity token. If approve / reject
    answer 401 through the proxy, that is the cause: read-only panels still work, and admin
    writes go through a direct call with `X-Serverless-Authorization` (as in
    `scripts/cloud_smoke.py`, verified).
* **API (private):** `curl -H "X-Serverless-Authorization: Bearer $(gcloud auth print-identity-token)" <url>/v1/ops/health`.
  `/healthz` cannot be reached on Cloud Run: the Google frontend reserves paths ending in
  `z`.
* **Events (bounded):**
  ```bash
  gcloud run jobs execute praxis-dev-streaming --region us-central1 --wait \
    --args=praxis.streaming,--environment,dev,produce,--cloud,--project,$CLOUD_PROJECT,--customers,20,--days,5
  gcloud run jobs execute praxis-dev-streaming --region us-central1 --wait \
    --args=praxis.streaming,--environment,dev,consume,--cloud,--project,$CLOUD_PROJECT,--role,operational
  ```
  `--role warehouse` writes to the job's ephemeral DuckDB (it reports `warehouse_rows`).
  The analytical plane is BigQuery, loaded in batch (`make bq-verify BQ_PROJECT=...`).
  Dead letters: run `consume --role dlq-inspect` on the **ops** job (only ops reads the DLQ
  subscription).
* **Ops monitor tick:** `gcloud run jobs execute praxis-dev-ops --region us-central1 --wait`
  (default args: one tick; it also runs daily at 03:50 UTC).

## The live loop (ADR 0027)
Once a day (03:10 UTC) Cloud Scheduler starts `praxis-dev-live`:
1. it rebuilds the SYNTHETIC world through yesterday;
2. it publishes the warehouse to `gs://praxis-dev-live-<project>` (mounted at `/mnt/live`);
3. it runs the model lifecycle for each new day: probation and automatic rollback, shadow
   evaluation, drift monitoring, retraining, validation, then into shadow;
4. if a shadow version has passed, the operator files a **pending promotion request**.

At 03:50 UTC one ops-monitor tick follows. The API serves the registry's champion and
follows a promotion or rollback within 30 s.

**Promote a model (the only human step):** open the console's MLOps page (or `gcloud run
services proxy`, above), find the pending `promotion` request, and approve it with your
approver token. The API switches to the new champion on its next registry refresh.

One-time bootstrap, done 2026-11-04. It trains, validates and promotes the first champion
(through 2026-08-30), and the following ticks catch up 7 days per execution:
```bash
gcloud run jobs execute praxis-dev-live --region us-central1 --wait \
  --args=praxis.live,bootstrap,--approver,<your name>
gcloud run jobs execute praxis-dev-live --region us-central1 --wait   # repeat until behind_days = 0
```
Status and the newest tick report:
```bash
gcloud run jobs execute praxis-dev-live --region us-central1 --wait --args=praxis.live,status
gcloud storage cat gs://praxis-dev-live-$CLOUD_PROJECT/state.json
gcloud storage ls gs://praxis-dev-live-$CLOUD_PROJECT/reports/
gcloud scheduler jobs list --location us-central1 --project $CLOUD_PROJECT
```
* A tick is idempotent: a second execution on the same day answers `up_to_date`.
* Repair: `--args=praxis.live,republish` rebuilds and publishes the warehouse the last tick
  saw, without operating anything (ADR 0027 amendment 3).
* A failed step leaves the state where it was, so the next tick retries the same days.
* An execution may take up to 4 hours (4 vCPU / 8 GiB). Ordinary days take a few minutes; a
  retrain day (about weekly) takes much longer, and the backtest it requires grows with the
  history (ADR 0027 amendments 1-2). A timed-out step is a failed step.
* To pause the loop: `gcloud scheduler jobs pause praxis-dev-live-tick --location
  us-central1` (and `resume`).
* Never edit `state.json` by hand to skip days; the lifecycle needs every day in order.

## Inspect
```bash
gcloud run services list --region us-central1 --project $CLOUD_PROJECT
gcloud run jobs executions list --region us-central1 --project $CLOUD_PROJECT --limit 10
gcloud compute instances describe praxis-dev-db --zone us-central1-a --project $CLOUD_PROJECT --format="value(status,machineType)"
gcloud compute instances get-serial-port-output praxis-dev-db --zone us-central1-a --project $CLOUD_PROJECT | tail -50
gcloud compute ssh praxis-dev-db --zone us-central1-a --tunnel-through-iap --project $CLOUD_PROJECT -- docker logs --tail 50 praxis-postgres
gcloud artifacts repositories describe praxis --location us-central1 --project $CLOUD_PROJECT   # size
gcloud billing projects describe $CLOUD_PROJECT                                               # billing linked?
make cloud-plan   # "No changes" = state matches the deployed infrastructure
```
Spend: Billing console, Reports, filter project `praxis-dev-510522`. The budget emails at
GBP 2 / 5 / 8 / 10. Guard decisions are in the `praxis-dev-spend-guard` logs
(`jsonPayload.message="budget notification"` or `"spend guard unlinked billing"`).

## Stop (keep everything, stop spend-generating parts)
* The services already scale to zero and cost nothing idle. Jobs run only when executed.
* Pause the daily triggers: `gcloud scheduler jobs pause praxis-dev-live-tick --location us-central1`
  and the same for `praxis-dev-ops-tick`. With the VM stopped, a tick fails without
  advancing its state.
* Stop the database VM (disks are kept; the Free Tier VM costs nothing anyway):
  `gcloud compute instances stop praxis-dev-db --zone us-central1-a --project $CLOUD_PROJECT`
  (start it again with `start`; Postgres comes back from the data disk).
* Emergency stop of everything: `gcloud billing projects unlink $CLOUD_PROJECT`. The spend
  guard does this itself at GBP 8. Google warns that resources may be deleted while billing
  is off. Re-link with `gcloud billing projects link`.

## If the spend guard has fired
1. Read the guard log entry (cost and reason). Check the Billing report for the cause.
2. Fix or remove the cause, then re-link billing (above). The VM restarts on `start`.
3. A budget's actual cost resets on the 1st of the month; until then every notification at
   or above GBP 8 unlinks billing again. Re-link only once the cause is understood.

## Destroy (irreversible)
```bash
make cloud-destroy CLOUD_PROJECT=praxis-dev-510522 CONFIRM=destroy-praxis-dev-510522
```
This deletes **everything Terraform manages**, including:
* the Postgres data disk (snapshots taken by the schedule are kept);
* the three BigQuery datasets **with their Phase 2 tables** (`delete_contents_on_destroy`);
* the raw bucket and the registry.

APIs stay enabled. Billing, the state bucket and the snapshots are outside Terraform:
* delete the state bucket with `gcloud storage rm -r gs://praxis-dev-510522-tfstate`;
* delete snapshots with `gcloud compute snapshots list` / `delete`;
* unlink billing with `gcloud billing projects unlink`.

A recreate is `make cloud-plan cloud-apply`, then the secrets, migrations and smoke.
