# Cost guard

The owner's ceiling is GBP 10 a month for the whole project, and the target is well under it.
A budget alert on its own only sends email, so Praxis also has a spend guard: a small Cloud Run
service that unlinks billing from the project when reported spend reaches GBP 8 (ADR 0026).

## Rules

1. Nothing is applied to GCP without the owner's approval. CI only validates Terraform and
   tests it against mocked providers.
2. Develop against local emulation: Docker Postgres, the Pub/Sub emulator, DuckDB.
3. Services scale to zero and every one has a max-instance cap.
4. Bulk history lives in GCS and BigQuery, never Postgres. Pub/Sub keeps 24 hours.
5. Never load-test Stripe Sandbox or a public API. Scale tests run locally.
6. Check current list prices before provisioning; free tiers change.
7. Never add an external IP, NAT, load balancer, Cloud SQL, scheduler trigger or always-on
   consumer without a cost line and owner approval.

## Safeguards in place

| Guard | Enforced? |
| --- | --- |
| Budget of GBP 10 (gross cost), email at GBP 2, 5, 8 and 10 actual and 100 % forecast | Notification only |
| Spend guard unlinks billing at GBP 8 actual | Yes, on reported cost, which lags by hours |
| Cloud Run min 0, request-based CPU, max instances 2 / 1 / 1 / 1 (API, console, public console, spend guard), 30 s timeout; jobs 1 task, no retries | Yes |
| Database is the Free Tier e2-micro VM in us-central1 with 20 GB of disk | Yes (module validation) |
| BigQuery query quota of 10 GiB per day | Yes, approximately (Google allows overshoot) |
| Cloud event production capped at 2,000 customer-days per run, never scheduled | Yes |
| Scheduler runs only the daily live tick and the daily ops tick, no retries; the live job is capped at 4 hours | Yes |
| Registry keeps 3 image versions, snapshots kept 7 days | Yes |
| Nothing public by default, so unauthenticated requests are refused before they are billed. The opt-in read-only console copy (ADR 0032) is the exception, capped at one instance | Yes |

## What it costs

The deployment is estimated at GBP 0.10 to 0.20 a month from list prices checked on
2026-11-01. Almost everything sits inside free tiers. The exception is Artifact Registry
storage, because another project on the same billing account already uses its free 0.5 GB. The
live loop adds a few USD a month of Cloud Run job time, mostly on weekly retrain days.

The only measured figure is Google's own: the budget notification reported GBP 0.65 for
November to date on 2026-11-05, and the guard held. There is no per-service billing export, so
the split between services is an estimate.
[final-cost-verification.md](evidence/final-cost-verification.md),
[deployment-preflight.md](evidence/deployment-preflight.md)

Free tiers are per billing account, so other projects on the same account use them up too.

## If the guard fires

See [deployment.md](deployment.md#if-the-spend-guard-has-fired). In short: read the guard's
log entry, find and remove the cause, then re-link billing. Until the budget resets on the 1st
of the month, any notification at or above GBP 8 unlinks billing again.
