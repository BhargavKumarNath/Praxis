# Cost report

> **Every figure on this page is an ESTIMATE from official list prices.** The repository holds
> no measured bill: there is no billing export, and the CLI cannot read spend (preflight risk
> R6). The only measured signal is the Billing console, which the owner reads. **Actual
> spend to date: not recorded here. The owner should fill it in from the Billing console.**

## 1. Guardrails (configured and tested)

| Guard | Value | Enforced? |
| --- | --- | --- |
| Owner ceiling | GBP 10 / month for the whole project | Policy |
| Budget `praxis-dev-monthly` | GBP 10 gross cost (credits excluded). Alerts at GBP 2 / 5 / 8 / 10 actual and 100 % forecast | Notification only |
| Spend guard | **Unlinks billing at GBP 8 actual** (budget topic, then an OIDC push, then `praxis.api.spend_guard`) | Yes. It acts on reported cost, which lags by hours |
| Cloud Run | min 0, max instances 2 / 1 / 1, request-based CPU, timeout 30 s. Jobs: 1 task, 0 retries, <= 10 min; the live job is 4 vCPU / 8 GiB, <= 4 h | Yes (Terraform; `tests/security`) |
| BigQuery | 10 GiB / day query quota. The unit was verified on the project (`MiBy`) | Yes (approximate) |
| Forbidden without a cost line and owner approval | external IP, NAT, load balancer, Cloud SQL, any new scheduler, any always-on consumer | Yes (`tests/security/test_terraform_posture.py`) |

The spend guard's path was exercised with a GBP 0.01 synthetic notification. It logged
`hold`, and billing stayed enabled. **No notification at or above GBP 8 was ever sent**, so
the unlink action itself has not been triggered for real
([deployment.md](../evidence/deployment.md)).

## 2. Estimated monthly cost

| Component | Estimate | Basis |
| --- | --- | --- |
| Deployment (API, console, spend guard, jobs, e2-micro Postgres, Pub/Sub, GCS, Secret Manager, Tasks) | **GBP 0.10-0.20** | Free tiers. The main paid items are Artifact Registry storage (the account's free 0.5 GB is already used by another project) and disk snapshots ([deployment-preflight.md](../evidence/deployment-preflight.md)) |
| Plausible bad case for the deployment | about GBP 8 | Every listed risk at once: the VM falls outside the Free Tier (GBP 5.87), the BigQuery free TiB is used up elsewhere, and so on. Still under the ceiling, and the guard acts at GBP 8 |
| Live loop (ADR 0027): one daily job, 4 vCPU / 8 GiB | about **USD 0.5-3 / month**, growing with the history | Ordinary days take a few minutes. A retrain day was about 1,600-2,000 s on 2 vCPU. **The 4 vCPU timing has not been measured yet** ([ADR 0027](../adr/0027-live-loop.md) amendment 2) |
| Cloud Scheduler | USD 0 (2 jobs; 3 are free per billing account) | ADR 0027 |
| Local development, CI, every phase gate | GBP 0 | Docker, DuckDB, GitHub-hosted runners (public repository) |
| Stripe Sandbox | GBP 0 | Test mode |

**Exchange rate used:** GBP 0.80 per USD plus 20 % VAT, i.e. GBP = USD x 0.96. This is
conservative, and the rate is not current (risk R7).

## 3. Cost by phase (as recorded in each evidence file)

Phases 0-15 each incurred **GBP 0**. All runs were local, the BigQuery checks used the
sandbox with billing disabled, and Stripe ran in test mode. Cloud spend began with the
deployment (2026-11-01) and the live loop (2026-11-04). **Measured (2026-11-05):** Google's
budget notification reports **GBP 0.65 for November to date**. The spend guard held
(`cost GBP 0.65 < guard GBP 8`). Source:
[final-cost-verification.md](../evidence/final-cost-verification.md).

## 4. Cost risk that grows: the live retrain

Every retrain must pass a rolling-origin backtest over the whole history. Both the number of
origins and the size of each training set grow, so the cost grows roughly with the **square**
of the world's age. After a year of live operation, ADR 0027 estimates USD 1-3 a month. That
is inside the ceiling, but it does not stop growing.

**Decided (ADR 0030):** a sliding 364-day training and backtest window. Once the live world
is a year old (about 2027-04-05), a retrain costs about what a one-year retrain costs. Every
evaluated world is shorter than the window, so no gate evidence changes.

## 5. What would change the picture

| Action | Effect | Status |
| --- | --- | --- |
| A 1M scale run on BigQuery or a larger host | Paid compute or query bytes | Not approved, not run |
| Enabling Stripe (making the API public) | The API becomes reachable. IAM-denied requests are not billed | Not done |
| A live LLM provider for the agent | Per-token cost. The benchmark is about USD 0.05 a run at gemini-3.5-flash-lite list prices; an investigation is at most 120K tokens (about USD 0.05) | Built (ADR 0029). Needs an API key in `praxis-dev-llm-api-key`; not configured |
| Another e2-micro on the same billing account | About GBP 5.87 / month for the Praxis VM | A risk to watch |
