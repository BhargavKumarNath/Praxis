# ADR 0026: GCP deployment topology, free-tier control plane and spend guard

Status: Accepted (2026-11-01, production deployment; owner-authorised, GBP 10/month ceiling,
GBP 0-5 target). Extends ADR 0024 (identities, caps, budget) and ADR 0021 (private console).
Evidence: `docs/evidence/deployment-preflight.md`, `docs/evidence/deployment.md`.

## Context
ADR 0024 declared identities, secrets, Pub/Sub, BigQuery, Cloud Tasks and two Cloud Run
services, but never deployed them. Four things were missing for a working deployment:
1. **A Postgres control plane.** The cheapest managed option (Cloud SQL shared-core)
   costs about the whole GBP 10 ceiling.
2. **A runtime for the consumers and the ops monitor.** These are pull loops, which a
   scale-to-zero service cannot host.
3. **Container images.**
4. **Any automatic response to spend.** Budgets only notify.

The Free Tier differs by region. The e2-micro VM and Cloud Storage's free 5 GB exist only
in us-central1 / us-west1 / us-east1. An external IPv4 address costs USD 0.005/hour (about
USD 3.65/month), and Cloud NAT carries the same IP charge plus a per-VM fee.

## Decision
* **Region.** Compute, registry, secrets, storage and tasks run in **us-central1**. The
  Phase 2 BigQuery datasets stay in **europe-west2**, imported into Terraform unchanged,
  because moving a dataset destroys it. The data is synthetic, so UK residency is not a
  constraint; the trade-off is recorded here.
* **Control plane: one Free Tier e2-micro VM.**
  * The OS is Container-Optimized OS on a Shielded VM, with OS Login and **no external
    IP**.
  * The official `postgres:17.11` image is mirrored into Artifact Registry by digest and
    pulled through Private Google Access.
  * Data lives on a separate 10 GB pd-standard disk with daily snapshots, kept 7 days. The
    boot and data disks together total 20 GB, under the free 30 GB.
  * The password is read from Secret Manager into tmpfs at boot, so it never enters state,
    metadata or the image.
  * Postgres uses scram-sha-256 and is reachable only from the private subnet (VPC
    firewall plus host iptables). SSH goes only through IAP.
  * The address is fixed at `cidrhost(subnet, 10)`, so the `database-url` secret survives a
    VM replacement.
* **Network.** A custom VPC with one /24 subnet (Private Google Access). Cloud Run reaches
  it through **Direct VPC egress** (`PRIVATE_RANGES_ONLY`; no connector, no NAT, no load
  balancer, no public address anywhere). `tests/security` pins all of this.
* **Services (scale to zero, request-based CPU):**
  * `api`: max 2 instances, **private by default**. `api_public` opens it only when the
    Stripe webhook integration is switched on. Requests that IAM denies are not billed,
    so a private API has no anonymous cost exposure.
  * `console`: max 1 instance; the Next UI as the ingress container, with the console
    API as a sidecar. The UI's rewrite target is fixed at build time to the sidecar.
  * `spend-guard`: max 1 instance, concurrency 1.
* **Jobs, not daemons.** `praxis-dev-ops` (ops identity) and `praxis-dev-streaming`
  (streaming identity) are Cloud Run jobs: 1 task, 0 retries, timeouts of 5 and 10 min.
  * Arguments are overridden per execution: migrations, `consume --cloud` drains a
    subscription and exits, and one ops-monitor tick.
  * Nothing is scheduled; every execution is an owner action. A continuously running
    consumer fleet is a later decision with its own cost line.
* **Cloud Pub/Sub from the CLI.**
  * `produce` and `consume` accept `--cloud` only when `--project` equals
    `PRAXIS_GCP_PROJECT_ID` and the emulator is not set.
  * A cloud `produce` is capped at 2,000 customer-days.
  * `emulator-setup`, `replay`, `emulator-bench` and `dlq` stay emulator-only, so topology
    is never created by code.
* **Spend guard.**
  * The GBP 10 budget measures **gross** cost (credits excluded). It alerts at GBP 2 / 5 /
    8 / 10 actual and at 100 % of forecast, and publishes its state to a topic.
  * A push subscription (OIDC token of the guard's own service account) delivers it to
    `praxis.api.spend_guard`. The guard's service policy names only that account as
    invoker. Project Owners still inherit `run.invoker` through `roles/owner`; the smoke
    measured this (the owner gets 200). It is not an escalation, because an Owner can
    unlink billing directly.
  * At **GBP 8 actual** the guard **unlinks billing from the project**, which stops every
    billable resource.
  * This needs the one project-level grant in Praxis: `roles/billing.projectManager` for
    the guard's account. It is the narrowest role that can do this, it is classified in
    `tests/security/test_terraform_posture.py`, and it is the same pattern the owner
    already uses elsewhere.
* **Images.**
  * One Python image (API, console API, jobs, guard) and one UI image.
  * Built locally, never with Cloud Build. Non-root, allowlist `.dockerignore`, deployed by
    digest.
  * The registry keeps the 3 most recent versions per image and deletes anything older
    than 7 days.
  * The Python image carries SYNTHETIC development artifacts only: the seed-1 demand model
    and its feature warehouse.
* **Terraform.**
  * State lives in a versioned, private GCS bucket (bootstrap: `gs://<project>-tfstate`).
  * Applies run only from saved, reviewed plan files (`make cloud-plan` / `cloud-apply`).
  * The runtime is declared only with `deploy_services = true`.
  * The mocked `terraform test` covers the whole graph, plus validations: e2-micro regions,
    the guard below the ceiling, a budget of at most 10.

## Alternatives rejected
* **Cloud SQL (shared-core):** about the whole ceiling for one instance; no headroom.
* **External free Postgres (Neon, Supabase):** a third-party account and a data path
  outside GCP, with free tiers that pause or expire; not reproducible from this repo.
* **e2-micro with an external IP or NAT:** about USD 3.65/month for the IP alone, plus an
  exposure.
* **europe-west2 compute:** no Free Tier VM (an e2-micro costs about GBP 6/month there) and
  no free Cloud Storage.
* **Always-on consumers or a scheduler:** recurring cost and unbounded work for a demo with
  no live event source (Stripe is off). Jobs can be scheduled later.
* **Graduated throttling instead of unlinking billing:** the services are already private
  and capped; the remaining spend sources (registry, snapshots, BigQuery) cannot be
  throttled by the application. Unlinking is the only action that stops everything.

## Consequences
* Expected cost is GBP 0.10 to 0.20/month (registry storage and snapshots); the plausible
  bad case is about GBP 8. These are estimates, not bills; see the preflight report.
* The database is a single VM: no high availability. The RPO is up to 24 h (daily snapshot)
  on synthetic data. It is the right size for a demonstration, not for production traffic.
* The guard acts on reported cost, which lags by hours. Google warns that unlinking may
  delete resources. Both are accepted for a reproducible, synthetic environment.
* The first real plan found two defects the mocked test could not: `count` / `for_each`
  keyed by apply-time service-account emails (Cloud Tasks enqueuer, guard invoker). A mock
  makes every value known at plan time, so `terraform test` cannot catch this class of
  defect; both now use statically keyed maps.
* Cloud Run's frontend reserves URL paths ending in `z`, so `/healthz` is unreachable on
  Cloud Run. External health checks use `/v1/ops/health`.
* `gcloud run jobs execute --args` rejects a value that appears twice in the list (for
  example `--max-ticks,1,--interval,1`), so pass distinct values or use the job's declared
  args.
* The VM cannot write Cloud Logging (that would need a project-level log-writer role); its
  boot log is the serial console, readable by the owner.
