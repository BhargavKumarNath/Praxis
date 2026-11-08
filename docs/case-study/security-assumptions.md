# Security assumptions

The data is synthetic, but the security practices are built to production standard
(CLAUDE.md s17). This page lists what is **enforced and tested**, what is **assumed**, and
what is **known to be weak**.

## 1. Enforced and tested

| Control | How it is enforced | Test / evidence |
| --- | --- | --- |
| Route inventory | Every API and console route is pinned with its protection class. A new route fails CI until it is classified. No route deletes data or carries a price. `/docs`, `/redoc` and `/openapi.json` are served only in `local` | `tests/security/test_route_inventory.py` |
| Least-privilege IAM | One service account per process. No primitive or project-level roles, except the one classified grant (the spend guard's `roles/billing.projectManager`). No service-account keys | `tests/security/test_terraform_posture.py`; [deployment.md](../evidence/deployment.md) measured each secret's readers against the declaration |
| Nothing public | No `allUsers` / `allAuthenticatedUsers` binding. Anonymous requests got 403 on all 3 services. No external IP, NAT or load balancer. Postgres has a private IP only | Cloud smoke 46 / 46 |
| Secrets | Secret Manager only. Versions are added out of band and never appear in Terraform or its state. Each secret has its own reader list. `.env` is gitignored. Secret-looking test fixtures are built at runtime | `tests/security`; `make secrets`; gitleaks over the full history |
| Webhook integrity | The Stripe signature is checked against the **raw body**. Rejected: wrong secret, tampered body, missing header, v0-only downgrade, stale or future timestamp. Duplicates are removed by provider event id | [phase-7.md](../evidence/phase-7.md): a 200-case single-byte mutation property test |
| Idempotent money movement | One idempotency key per (invoice, attempt). The DB refuses a second live retry job. Money is stored as integer minor units | [phase-8.md](../evidence/phase-8.md) |
| Decision integrity | No price executes without a persisted, checksummed record, enforced by a Postgres trigger. Decision tables are append-only. A frozen product cannot execute | [phase-6.md](../evidence/phase-6.md), [phase-12.md](../evidence/phase-12.md) |
| AI operator containment | An import contract stops it reaching the optimiser, payments, training, streaming, the API or `subprocess`. Tools have typed schemas, permissions are enforced outside the prompt, and every call is audited (audit fails closed). Guarded actions become PENDING requests for a named human. The DB refuses self-approval | [phase-12.md](../evidence/phase-12.md): 28 prohibited attempts refused, 0 executed; prompt-injection fixtures in 4 channels |
| Console boundary | The console's only writes are approve / reject and lifting a freeze. Each needs a server-side token that names the approver. It cannot import pricing, payments, streaming, the simulator or science | `src/praxis/console`, import contract, [phase-13.md](../evidence/phase-13.md) |
| Supply chain | Actions pinned by SHA. Tool images pinned (Terraform 1.16.5, trivy 0.75.0, gitleaks v8.30.1, k6 by digest). Dependency audits run nightly: `pip-audit`, `npm audit`, trivy config. Dependabot runs weekly | `make nightly-security` (Phase 16 result in [phase-16.md](../evidence/phase-16.md)) |
| Logs | No secret keys, webhook secrets or full payment details in logs or traces. A span records only the exception type | [phase-11.md](../evidence/phase-11.md) trace test |

## 2. Assumptions (believed, not proven here)

- **Google IAM and Secret Manager behave as documented.** The smoke tests measured the
  bindings, but nothing penetration-tested them.
- **Project Owners can invoke every service.** `roles/owner` includes `run.invoker`, so the
  owner can call the spend guard directly. This was found in deployment smoke run 3: the
  expectation was wrong and was corrected, and the correction is disclosed.
- **Unlinking billing stops all resources.** Google warns that resources may be deleted. This
  is accepted for a synthetic environment that Terraform can rebuild.
- **The Stripe Sandbox matches live mode** in signature scheme and event shapes, at the
  pinned API version `2026-09-30.endive` (checked against the official docs on 2026-10-07).
  Live mode was never used.
- **The single Postgres VM is trusted.** It has no HA, it is reachable only on its private
  IP inside the VPC, and its recovery point is the daily snapshot (RPO up to 24 h of synthetic data).

## 3. Known weaknesses (open)

| Weakness | Risk | Owner action |
| --- | --- | --- |
| Approver tokens are static (no expiry or rotation) | A leaked token approves requests until it is rotated by hand | Add rotation and expiry before any shared use (Phase 13 carry-over) |
| `.gitleaksignore` holds 3 fingerprints for one reviewed false positive (a public Stripe price lookup key, commit `105c930`) | A real finding at the same fingerprint would be hidden. The ignore is fingerprint-scoped, never by path or rule | Rewriting history would remove it; that needs an explicit instruction |
| Console access through `gcloud run services proxy` is unverified | The private console may be unreachable to the owner without extra steps | Verify once |
| The dunning Pub/Sub subscription has no consumer role in the cloud | Cloud retries are not wired | Needed only when Stripe is switched on |
| No WAF or rate limiting on the API | Abuse once the API is made public for Stripe | Decide before switching `api_public` on |
| No live LLM provider (when one is added: prompt-injection exposure, key handling) | The tests use scripted providers only | Owner decision; the `llm-api-key` secret is readable only by the agent identity |
