# Phase 7 Evidence Report

```text
Phase: 7 - Stripe Sandbox Integration
Date: 2026-10-07
Code revision: 9790abd + working tree (uncommitted; no commits made by Claude)
Environment: Linux, Python 3.12, Postgres 17 (local Docker), Pub/Sub emulator (local Docker),
             httpx 0.28, FastAPI/uvicorn, Stripe API version pinned to 2026-09-30.endive,
             Stripe CLI image stripe/stripe-cli:v1.53.0 (for make stripe-verify)
All customers are SYNTHETIC. Stripe: SANDBOX only (test-mode key), 4 customers, 126 API
requests in one verification run. No cloud resource was created or used.
```

Design: `docs/payments.md`, ADR 0014 (with ADR 0003 and 0006). Stripe facts were checked
against the current official docs on 2026-10-07: the API version, webhook signature scheme,
event names and ordering, the `invoice.payments` shape, `attempt_count` semantics, test cards
and Test Clock limits.

## Gate

**PASS** (2026-10-07). Offline items passed first. The live Stripe Sandbox items then passed
with `make stripe-verify` (5/5, 72 s) once a sandbox key was configured. Raw live report:
`phase-7-stripe-sandbox.json`.

| project_plan Phase 7 gate | Result |
| --- | --- |
| real Stripe sandbox happy path passes | PASS: real signed webhooks -> endpoint -> inbox -> re-fetch -> control plane: customer active, subscription active, invoice paid on attempt 1, ledger GBP 49 (9.0 s) |
| decline / failure fixtures pass | PASS: Stripe-shaped charge / invoice / 402 fixtures (generic, insufficient funds, expired, processing error, authentication) map to the contract reasons; decline -> recovery passes end to end on synthetic AND live Stripe (`pm_card_chargeCustomerFail`: invoice open, attempt 1 `card_declined`, customer converted; new card + pay -> paid on attempt 2, active) |
| duplicate webhook test passes | PASS: same event id 3x -> `accepted, duplicate, duplicate`, one inbox row (`deliveries = 3`); duplicated notifications converge to the same control-plane checksum. Real payloads: the 33 real Stripe events, re-delivered twice each -> 33 accepted + 33 duplicate, same state |
| out-of-order webhook test passes | PASS: every notification of a 3-customer world delivered 3x newest-first converges to the same checksum as clean delivery; Hypothesis: derived events in any order with duplicates fold to the same invoice / customer state (150 cases). Real payloads: the 33 real events replayed newest-first into an empty control plane reach exactly the live state for all 4 customers |
| invalid signature rejected | PASS: wrong secret, tampered body, missing / malformed header, v0-only (downgrade), stale / future timestamp -> 400 and nothing stored; 200-case Hypothesis single-byte mutation property |
| raw body signature verification confirmed | PASS: independent HMAC re-implementation matches; the same JSON re-serialised (different bytes) is rejected at the endpoint and in the verifier |
| handler returns quickly before heavy processing | PASS: p95 6.6 ms over 500 signed deliveries (gate test: p95 < 100 ms over 200); the receiver holds no gateway, and every row stays `pending` after the response |
| test clock scenario passes where used | PASS on the synthetic clock AND a real Stripe Test Clock: advanced 1 month + 2 h, renewal invoice paid, then cancellation -> churned (voluntary), subscription cancelled, ledger 2 x GBP 49 (23.9 s); all 4 clocks deleted afterwards |

## What was built

* `praxis.payments` (new package, layer `praxis.pricing | praxis.payments`; new import
  contract "Payments are independent of the simulator, science and pricing"; coverage floor 95%):
  `PaymentGateway` protocol, `StripeGateway` (thin httpx client), `SyntheticPaymentGateway`,
  provider-neutral snapshots, the pure `normalise` derivation, signature verification,
  webhook routing and receiver, Postgres inbox and provider-ref store, `NotificationProcessor`,
  `BillingService`, CLI `python -m praxis.payments {process|inbox|requeue}`.
* API: `POST /v1/webhooks/stripe` (`praxis.api.webhooks`).
* Migration `0003`: `payment_webhook_inbox` (PK = provider event id, lease-based claiming),
  `payment_provider_refs` (append-only by trigger).
* Settings: `stripe_api_version`, `stripe_api_base_url`, `stripe_timeout_s`,
  `stripe_webhook_tolerance_s` (names in `.env.example`). `httpx` became a runtime
  dependency (it was already locked as a dev dependency).
* `make stripe-verify` (opt-in, live, never CI).

## Tests executed

* `make check` (== CI): lint, mypy (246 files), import contracts (5 kept), **1198 passed,
  2 skipped** (live BigQuery and live Stripe, both opt-in), coverage **98.91%**, 118 per-file
  floors met, event chaos smoke vs oracle, schemas, secret scan, frontend, Terraform,
  actionlint + zizmor. The first `audit` attempt hit a pypi.org read timeout (network). On
  re-run it reported no known Python vulnerabilities and 0 npm vulnerabilities.
* `make nightly-security` (gitleaks full history + trivy Terraform): clean.
* Payments suite: 225 tests in `tests/payments/`:
  * `test_signature.py` (27): documented construction, raw body, tampering, Hypothesis
    mutation, rotation, downgrade, malformed headers, tolerance window.
  * `test_webhook.py` (42): every routed event type, unknown types ignored, malformed
    payloads, livemode refused, size limit, inbox outage -> transient.
  * `test_normalise.py` (20): contract validity of every derived event, attempt ordering by
    charge time, skips, determinism / monotonicity, order-independence property at the fold.
  * `test_stripe_client.py` (24): headers (auth, pinned version, idempotency key), form
    encoding, retry classification (429, 5xx, 409 lock_timeout, `Stripe-Should-Retry`),
    bounded retries reusing the key, 402 card errors, idempotency conflicts, no key or body
    in errors, pagination bound, live keys refused.
  * `test_stripe_gateway.py` (48): request shapes (allow_incomplete, metadata, no personal
    data), ensure-once semantics, decline fixtures, payment-method types, cancellation
    reasons, foreign objects, invoice payments -> charges, Test Clock polling / failure /
    timeout.
  * `test_synthetic.py` (16), `test_store.py` (9: dedupe, lease, SKIP LOCKED, transitions,
    constraints, append-only refs), `test_processor.py` (10: coalescing, failure policy,
    backoff, crash after publish -> no double effect), `test_api_webhooks.py` (12),
    `test_cli.py` (5), `test_ids_and_model.py` (5).
  * `test_contract.py` (7): the shared contract scenarios (happy path, decline then
    recovery, renewal then cancellation, idempotent re-enrolment) on the synthetic gateway
    through inbox -> processor -> producer -> broker -> operational consumer -> Postgres,
    plus duplicate / reordered delivery convergence.
  * `test_stripe_sandbox.py` (5, `make stripe-verify`, live): the same four scenarios against
    the Stripe Sandbox (Stripe CLI v1.53.0 forwarding real signed webhooks to uvicorn), plus
    the real-event replay. **5 passed in 72 s.** Stripe requests: 94 GET, 27 POST, 5 DELETE;
    33 webhook events received and processed; 0 natural duplicate deliveries observed (the
    duplicate guarantee is shown by the replay).
* Coverage (branch) for `praxis.payments`: 99.3-100% per file; `api/webhooks.py` 100%.

## Performance metrics (local, synthetic; not a scale claim)

* Webhook acknowledgement, FastAPI TestClient + Postgres inbox, 500 signed deliveries:
  p50 4.4 ms, p95 6.6 ms, p99 8.1 ms, max 9.8 ms.
* Synthetic provider, 1,000 customers (10% declined first payment), one Test Clock month:
  6,700 notifications -> 4,700 object fetches -> 10,200 internal events; gateway ops 5.1 s,
  inbox insert 7.6 s, processor 8.5 s, pipeline drain 25.9 s, total 47.1 s. Result: 900
  active, 100 converted (declined, unrecovered), 1,900 invoices, 1,800 ledger entries
  (GBP 88,200), 0 pending, 0 orphans. Arithmetic check: 1,000 + 900 renewals; 900 x 2 paid
  x GBP 49.
* Live sandbox scenario wall time (incl. webhook delivery via the CLI and 2 s polling):
  happy 9.0 s, decline + recovery 12.3 s, renewal + cancellation 23.9 s, re-enrolment 6.5 s.
  126 Stripe requests per run, bounded by four customers.

## Statistical metrics

Not applicable (no model in this phase).

## Failures found

* `idempotency_key` joined intent parts with NUL, so a part containing NUL could collide with
  two parts. Found while writing the key tests; parts containing NUL are now rejected.
* The CLI report and structured logs share stdout (project convention); the CLI tests parse
  the trailing report document.
* Live run: 2 inbox rows were still `pending` at teardown: events delivered after the final
  drain (from the Test Clock deletions / cancellations at cleanup). They affect no assertion.
  A deployed worker drains continuously; noted, not a defect.

## Known limitations

* `subscription.changed` (tier change) is not derived from Stripe yet; nothing executes plan
  changes in Phase 7.
* Stripe `uncollectible` / `void` invoices do not close the internal invoice, and
  `payment.failed.final` is always false. Phase 8 owns dunning states (ADR to come).
* Usage is not billed through Stripe (the internal invoice is the subscription fee).
* The synthetic provider does not model Smart Retries or latency (Phase 14).
* `process` exits after draining; a long-running worker (Cloud Run job / scheduler) is not
  deployed. No Terraform change was made.
* Starlette warns that its TestClient's use of `httpx` is deprecated (pre-existing, all API
  tests).

## Cloud cost incurred

GBP 0. Stripe Sandbox (test mode, no money moves); no GCP calls. Sandbox leftovers by
design: product `praxis_growth` and one GBP 49/month price (reused by later runs);
customers and subscriptions were deleted with their Test Clocks.

## Reason

Every project_plan Phase 7 gate item has evidence. Offline items pass under `make check`;
the real sandbox happy path, decline and recovery, Test Clock renewal and cancellation, and
real-payload duplicate / out-of-order replay pass under `make stripe-verify`. The same contract
scenarios pass on both gateways, so the synthetic provider is a faithful stand-in for scale
work. No load was sent to Stripe.
