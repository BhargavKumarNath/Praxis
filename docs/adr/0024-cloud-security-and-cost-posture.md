# ADR 0024: Cloud security and cost posture (identities, secrets, Cloud Run, spend guards)

Status: Accepted (2026-11-01, Phase 15). Extends ADR 0021 (console never public). Declared
in Terraform only: nothing was applied; any apply is an owner decision (docs/cost-guard.md).

> **Amended by ADR 0026 (deployment, 2026-11-01):** applied to `praxis-dev-510522`. The budget
> is GBP 10 (the owner's ceiling, validated <= 10) with alerts at GBP 2 / 5 / 8 / 10 on gross
> cost, plus a spend guard that unlinks billing at GBP 8. The API is private by default
> (`api_public`). The guard holds the one classified project-level grant. Two first-apply
> defects of the `for_each`-on-apply-time-values class were fixed.

## Decision
* **Identity.** One service account per process (api, console, streaming, ops, agent) and
  **no project-level role at all**. Every grant is on one resource: secret accessor per
  secret, publisher on the events topic, subscriber per subscription, Cloud Tasks enqueuer
  on the retry queue. No service-account keys. Checked by
  `tests/security/test_terraform_posture.py`.
* **Secrets.** Secret Manager containers only; versions are added out of band, so no secret
  value ever enters Terraform state. Each secret has an explicit reader list (e.g. only the
  API reads the Stripe webhook secret; only the console reads the approver tokens).
* **Exposure.** Only the API may grant `run.invoker` to `allUsers` (Stripe must reach the
  signed webhook). The console module refuses `public = true` by validation and takes named
  invokers only. FastAPI `/docs`, `/redoc` and `/openapi.json` are served only when
  `environment = local`. `tests/security/test_route_inventory.py` inventories every route
  with its protection and proves each write refuses an unauthenticated caller.
* **Cost.** Cloud Run: min instances 0, request-based billing (`cpu_idle = true`), max
  instances validated 1..10 (dev: API 2, console 1), request timeout <= 60 s. A GBP budget
  (default 5, validated <= 5) with 50/90/100 % and forecast alerts; a budget is a
  notification, never a cap. A BigQuery daily query quota (default 10 GiB/day, validated so
  31 days stay inside the 1 TiB monthly free tier) - the one enforced spend limit, and
  approximate per Google. Services are declared only when `deploy_services = true`.
* **Destroy / recreate.** `make tf-test` runs `terraform test` (Terraform 1.16.5, pinned in
  the Makefile and CI) with mocked `google` / `google-beta` providers: it applies the whole
  dev graph, re-applies a reduced graph and destroys both, and checks the validations.
  Dev resources are destroyable (`force_destroy`, `delete_contents_on_destroy`,
  `deletion_protection = false`). `make cloud-destroy` (real) requires
  `CONFIRM=destroy-<project>` and is never called by CI.

## Consequences
* The mocked test proves the configuration plans, creates and destroys cleanly; it does not
  prove GCP API behaviour. It already caught one real defect (a `for_each` keyed by
  apply-time service-account emails, which would have failed the first real apply).
* The BigQuery quota unit (MiB/day) was verified on 2026-11-01 by a read-only Service Usage
  listing (metric unit `MiBy`, default 209,715,200 = the documented 200 TiB/day) and
  Google's unit grammar; see `docs/cost-guard.md`. Open: GCS access logging stays accepted in
  `.trivyignore` (GCP-0077, exp 2027-03-31); a cloud Postgres is not declared (the cheapest
  managed option exceeds the GBP 5 target; local / free-tier Postgres remains the plan).
