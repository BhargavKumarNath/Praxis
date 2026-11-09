# Payments

All payment operations go through one interface, `PaymentGateway`, with two implementations:
`StripeGateway` (Stripe Sandbox only; the client refuses live keys) and
`SyntheticPaymentGateway`, which has Stripe-like billing semantics and deterministic outcomes
and is what every large test uses. Decisions: ADR 0003, 0006 and 0014.

```text
BillingService (enrol, update payment method, retry invoice, cancel)
        | idempotency key = hash of the business intent
        v
PaymentGateway ── StripeGateway (pinned API version, sandbox)
               └─ SyntheticPaymentGateway
        |
  provider changes an object
        |  Stripe: signed webhook -> POST /v1/webhooks/stripe
        v
payment_webhook_inbox (Postgres, primary key = provider event id; a duplicate adds nothing)
        |  NotificationProcessor: claim with SKIP LOCKED, group per object
        v
gateway.fetch_invoice / fetch_subscription   <- current state, not the webhook payload
        v
normalise to v1 events with deterministic ids -> EventProducer -> Pub/Sub -> control plane
```

The webhook route only verifies the signature on the raw body, stores the event and returns.
The heavy work happens asynchronously, and it always re-fetches the object, so out-of-order
and duplicated webhooks converge on the provider's current state. Both providers produce the
same internal events.

## Event mapping

| Provider fact | Internal event |
| --- | --- |
| Invoice finalised (GBP, amount > 0, a Praxis tier) | `invoice.created` |
| The n-th charge of the invoice | `payment.attempted` |
| Charge succeeded / failed | `payment.succeeded` / `payment.failed` |
| First paid invoice of a subscription | `subscription.started` |
| Subscription cancelled after activation | `churn.observed` |

Stripe event types outside the routed set (`webhook.ROUTES`) are acknowledged and not stored.
Declines map to `insufficient_funds`, `expired_card`, `processor_error`,
`authentication_required` or `card_declined`.

## Running it

```bash
make test                     # the synthetic contract suite and webhook tests, offline
make stripe-verify            # opt-in: a real Stripe Sandbox run (sk_test_ key in .env, Docker)
python -m praxis.payments --database-url URL inbox     # inbox counts by status
python -m praxis.payments --database-url URL process --project <pubsub project>
python -m praxis.payments --database-url URL requeue   # failed -> pending after a fix
```

`stripe-verify` starts the Stripe CLI in Docker to forward webhooks to a local server, runs
four scenarios (happy path, decline then recovery, Test Clock renewal then cancellation,
idempotent re-enrolment), replays the real events reversed and duplicated into an empty
control plane, and deletes every Test Clock it created.

The API answers 503 on the webhook route until both `PRAXIS_STRIPE_WEBHOOK_SECRET` and
`PRAXIS_DATABASE_URL` are set.

## Safety rules

- Sandbox only, four customers per verification run, never a load test.
- Only synthetic identifiers are sent to Stripe: no names, emails or addresses. The inbox
  stores routing fields and a body checksum, not the payload.
- Keys and webhook secrets are `SecretStr` settings and never appear in logs, errors, reports
  or command lines.
- Signature verification rejects a wrong secret, a tampered body, a missing header, a
  v0-only downgrade and stale or future timestamps (a single-byte mutation property test).
