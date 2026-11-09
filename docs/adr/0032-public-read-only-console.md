# ADR 0032: A public, read-only copy of the decision console

Status: Accepted (2026-11-09, owner decision: "we need to make it public", option "public
read-only copy"). Applied 2026-11-09 with owner approval: plan 5 added, 0 changed, 0 destroyed.
Verified anonymously the same day: the console page and every view's data answered 200, and
`whoami` and an approve POST answered 503 `admin_not_configured`.

## Context

ADR 0021 and ADR 0024 keep the decision console private: it is the only place where a person
approves or rejects a guarded request or lifts a price freeze. The owner wants the console to be
viewable by anyone (a portfolio link), which those decisions forbid for the approving console.

## Decision

* Add a second Cloud Run service, `console-public`, behind the opt-in variable
  `console_public` (default `false`). It runs the same two images as the private console.
* It never receives the `console-approvers` secret. With no approvers configured, the console
  API answers 503 `admin_not_configured` on every admin route (existing behaviour, ADR 0021),
  so nobody can approve, reject or lift a freeze through it. It has no route that carries a
  price (the route inventory is unchanged).
* It has its own service account (`console-public`). That identity reads only the database URL
  secret and the live volume (read-only). It cannot read the approver secret.
* `roles/run.invoker` for `allUsers` on that service only. The Cloud Run module's validation now
  allows `public = true` for `api` and `console-public` and still refuses it for `console`.
* One instance at most, scale to zero, the module's 30 s request timeout.

The private console is unchanged and remains the only way to approve.

## Consequences

* Anyone can read the synthetic operating state: decisions, incidents, model registry, the
  agent audit trail and the cost estimate. All of it is synthetic. It contains no secret and no
  personal data.
* Public traffic is billed by Cloud Run and reaches the e2-micro Postgres VM. One instance caps
  concurrency, and the GBP 10 budget with the GBP 8 spend guard caps spend. Unauthenticated
  requests are no longer refused before billing on this one service.
* Read-only is enforced by configuration and code (no approver secret, so no admin path), not by
  the database. The service's database credential can write, because the console publishes its
  own metric snapshots. A read-only database role that allows only that is a follow-up.
* There is no WAF or rate limiting. A sustained flood would raise cost (bounded as above) and
  could slow the private console and API, which share the database VM.
* Tests: `tests/security/test_terraform_posture.py` and the mocked Terraform test
  `the_read_only_console_copy_can_be_public` pin that the copy is opt-in, off by default,
  capped at one instance and never granted the approver secret.
  `console_can_never_be_public` still refuses a public approving console.
