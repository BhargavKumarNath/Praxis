# ADR 0014: Stripe integration: signed webhook inbox, state re-fetch, shared event contracts

Status: Accepted (Phase 7)

## Context
ADR 0003 puts every payment operation behind `PaymentGateway` (Stripe Sandbox and a
synthetic provider) and requires both to emit the same internal payment events. ADR 0006
keeps Stripe for small integration tests only. Stripe's documentation (checked 2026-10-07,
API version `2026-09-30.endive`) says:

* webhooks must be verified against the raw body (`Stripe-Signature`, HMAC-SHA256 over
  `"{t}." + body`, `v1` only, timestamp tolerance), acknowledged quickly, and processed
  asynchronously;
* delivery is at least once and **not ordered**; `created` has one-second resolution and
  must not be used to order or deduplicate; the recommended pattern is to treat an event
  as a signal and retrieve the object;
* an invoice's `attempt_count` counts only scheduled attempts ("manual payment attempts
  after the first attempt do not affect the retry schedule");
* since `2025-03-31.basil` an invoice's payments are `invoice.payments` (InvoicePayment ->
  PaymentIntent), and subscription metadata sits under `parent.subscription_details`;
* idempotency keys make POST retries safe for 24 hours.

The Phase 3 control plane already applies internal events idempotently and
order-independently (ADR 0009): event ids are deduplicated in `processed_events`, and each
aggregate is a fold over its event set.

## Decision
* **Durable inbox, immediate acknowledgement.** `POST /v1/webhooks/stripe` reads the raw
  bytes, verifies the signature (several secrets and several `v1` values for rotation,
  constant-time comparison, +-300 s tolerance, never 0), parses only routing fields and
  inserts one row into `payment_webhook_inbox` (migration 0003), keyed by the Stripe event
  id. A duplicate delivery hits the primary key and is acknowledged as `duplicate` (the
  `deliveries` counter records it). No provider call and no business logic run in the
  request. Invalid signatures and payloads get 400 and are never stored. An inbox outage
  returns 503 so Stripe retries. Live-mode events are refused.
* **Webhooks are notifications; state is re-fetched.** A worker
  (`NotificationProcessor`, `python -m praxis.payments process`) claims due rows with
  `FOR UPDATE SKIP LOCKED` as a lease, groups them per object, fetches the object's current
  state once (invoice + its charges, or subscription + its paid invoices) and derives the
  internal events from that snapshot. Out-of-order and duplicate webhooks therefore cannot
  change the outcome: the snapshot is the same whichever event triggered the fetch.
* **One derivation for both providers.** Both gateways return provider-neutral snapshots
  (`praxis.payments.model`); `praxis.payments.normalise` turns a snapshot into the existing
  v1 contracts (`invoice.created`, `payment.attempted`, `payment.failed`,
  `payment.succeeded`, `subscription.started`, `churn.observed`). The synthetic provider
  emits the same kind of notifications into the same inbox, so the same code path is tested
  at scale without Stripe.
* **Deterministic event ids.** Each derived event's id is
  `uuid5(namespace, provider:kind:object:role)`, e.g. `stripe:invoice:in_..:attempt-2`.
  Re-processing after a crash, a replay or a duplicate re-derives the same ids, which the
  control plane deduplicates: exactly-once effect without distributed transactions.
* **Attempts are charges.** Attempt *n* is the *n*-th charge of the invoice's
  PaymentIntents ordered by (created, id), so manual recovery attempts count.
  `payment.failed.final` is always `false`: deciding when collection is over is dunning
  policy (Phase 8), not a provider fact.
* **Praxis owns the customer lifecycle; Stripe owns billing facts.** Enrolment publishes
  `customer.created` (and `conversion.observed` for new customers) from Praxis, timed at the
  provider customer's creation so they sort first. Activation (`subscription.started`) is
  the first paid invoice of the subscription; cancellation (`churn.observed`) uses
  `cancellation_details.reason` (`payment_failed` / `payment_disputed` -> involuntary).
  Objects without Praxis metadata are `ForeignObject` and recorded as ignored.
* **Idempotent outbound writes.** Every POST carries an idempotency key derived from the
  business intent (`praxis-<operation>-sha256(parts)`), so a retried intent reuses its key.
  `ensure_customer` / `ensure_plan` also persist the created object id in
  `payment_provider_refs` (append-only by trigger), which keeps them idempotent beyond the
  24-hour key window. Prices are identified by `lookup_key`; a new fee is a new price.
* **A thin client instead of the SDK.** `StripeClient` (httpx) pins `Stripe-Version`,
  refuses any non-test key, bounds timeouts, retries only transient failures (connection,
  timeout, 409 `lock_timeout`, 429, 5xx, honouring `Stripe-Should-Retry`) at most twice with
  the same key, and never puts the key, request body or card data into errors or logs.
* **Bounded failure handling.** Transient fetch or publish failures back off exponentially
  (5 s doubling, capped at 15 min) and become `failed` after 8 attempts; permanent errors
  fail at once; `requeue` re-opens failed rows after a fix.
* **Sandbox verification.** `make stripe-verify` drives four customers, each on its own
  Test Clock (deleted afterwards), through the shared contract scenarios with real signed
  webhooks forwarded by the Stripe CLI (Docker) to a real uvicorn server, then replays the
  real events reversed and duplicated into an empty control plane and requires the same
  state. It is opt-in and never part of CI.

## Consequences
* Every processed notification costs Stripe reads (invoice + one charge list per
  PaymentIntent; subscription + paid-invoice list). Coalescing per object per batch keeps
  this small; at Stripe scale a short delay before processing would coalesce more.
* Usage-based billing is not wired to Stripe: the internal invoice is the subscription fee
  (as in the simulator), and pushing synthetic usage to Stripe meters would be load testing.
* Subscription tier changes (`subscription.changed`) are not derived yet: the snapshot does
  not carry the previous tier, and nothing executes a price or plan change in Phase 7.
* Stripe-side terminal invoice states (`uncollectible`, `void`) do not close the internal
  invoice; Phase 8 owns the dunning state machine and its ADR.
* The synthetic provider models Stripe's billing timing (calendar months, collection one
  hour after the period end, `incomplete` on a declined first payment, `past_due` on a
  declined renewal) but not Smart Retries or latency.
