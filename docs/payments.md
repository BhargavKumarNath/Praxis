# Payments: Stripe Sandbox and the synthetic provider (Phase 7)

**All customers are SYNTHETIC.** Stripe runs in a **sandbox** only (the client refuses live
keys). Decisions: ADR 0003 (gateway), ADR 0006 (scale vs integration), ADR 0014 (webhook
inbox, state re-fetch, shared contracts). Evidence: `docs/evidence/phase-7.md`.

```text
BillingService.enrol / update_payment_method / retry_invoice / cancel
        |  idempotency key = hash(business intent)       customer.created, conversion.observed
        v                                                  (Praxis-side facts) ---------------+
PaymentGateway  ---- StripeGateway (httpx, pinned API version, sandbox)                       |
        |       \--- SyntheticPaymentGateway (deterministic, in process)                      |
        |                                                                                     |
  provider changes an object                                                                  |
        |  Stripe: signed webhook -> POST /v1/webhooks/stripe                                 |
        |  synthetic: Notification                                                            |
        v                                                                                     |
payment_webhook_inbox (Postgres, PK = provider event id)  <- duplicate = no new row           |
        |  NotificationProcessor: claim (SKIP LOCKED lease), group per object                 |
        v                                                                                     |
gateway.fetch_invoice / fetch_subscription   (current state, not the webhook payload)        |
        v                                                                                     |
normalise: snapshot -> v1 events with deterministic ids                                       |
        v                                                                                     v
EventProducer (validate, archive) -> Pub/Sub -> operational consumer -> control plane (ADR 0009)
```

## Modules (`src/praxis/payments/`)

| Module | Role |
| --- | --- |
| `model.py` | Provider-neutral types: `CustomerProfile`, `Plan`, `PaymentBehaviour`, snapshots, `Notification` |
| `gateway.py` | `PaymentGateway` / `SnapshotSource` / `ClockControl` protocols; `GatewayError`, `IdempotencyConflict`, `UnsupportedOperation`, `ForeignObject` |
| `ids.py` | Deterministic event ids (UUIDv5), flow correlation ids, intent-derived idempotency keys |
| `normalise.py` | Snapshot -> internal events (pure; both providers) |
| `signature.py` | `Stripe-Signature` verification over the raw body |
| `webhook.py` | Event routing (current Stripe event names), `WebhookReceiver` (verify -> parse -> inbox) |
| `store.py` | `PostgresInbox` (record, claim, complete / ignore / retry / fail / requeue), provider refs |
| `processor.py` | `NotificationProcessor`: inbox -> re-fetch -> derive -> publish -> mark |
| `stripe_client.py` | Thin Stripe REST client: auth, version pin, form encoding, retry classification |
| `stripe_gateway.py` | `StripeGateway` + pure Stripe JSON -> snapshot mappings; Test Clocks |
| `synthetic.py` | `SyntheticPaymentGateway`: Stripe-like billing semantics, deterministic outcomes |
| `service.py` | `BillingService` workflows; `deliver_notifications` (synthetic -> inbox) |
| `__main__.py` | CLI `python -m praxis.payments {process|inbox|requeue}` |

The HTTP route is `praxis.api.webhooks` (`POST /v1/webhooks/stripe`); the app builds the
receiver when both `PRAXIS_STRIPE_WEBHOOK_SECRET` and `PRAXIS_DATABASE_URL` are set and
answers 503 otherwise.

## Event mapping

| Provider fact | Internal event (id role) | `occurred_at` |
| --- | --- | --- |
| invoice finalised (not draft, amount > 0, GBP, Praxis tier) | `invoice.created` (`created`) | `status_transitions.finalized_at` |
| *n*-th charge of the invoice, by (created, id) | `payment.attempted` (`attempt-n`) | charge `created` |
| charge succeeded / failed | `payment.succeeded` / `payment.failed` (`result-n`, `final=false`) | charge `created` |
| first paid invoice of a subscription | `subscription.started` (`started`) | earliest `paid_at` |
| subscription `canceled` after activation | `churn.observed` (`ended`) | `ended_at` |
| enrolment (Praxis) | `customer.created`, `conversion.observed` (new only) | provider customer `created` |

Routed Stripe event types (`webhook.ROUTES`): `invoice.{created, finalized, updated, paid,
payment_succeeded, payment_failed, payment_action_required, marked_uncollectible, voided}`,
`invoice_payment.paid` and `customer.subscription.{created, updated, deleted, paused,
resumed}`. Others are acknowledged as `ignored` and not stored.

Decline mapping (Stripe `failure_code` / decline code or `outcome.reason`):
`insufficient_funds` -> `insufficient_funds`; `expired_card` -> `expired_card`;
`processing_error` -> `processor_error`; `authentication_required` ->
`authentication_required`; anything else -> `card_declined`.

## Payment behaviours

| `PaymentBehaviour` | Stripe test PaymentMethod | Synthetic outcome |
| --- | --- | --- |
| `SUCCEEDS` | `pm_card_visa` | succeeded |
| `CHARGE_FAILS` | `pm_card_chargeCustomerFail` (attaches, then declines) | `card_declined` |
| `INSUFFICIENT_FUNDS`, `EXPIRED_CARD`, `PROCESSING_ERROR` | not attachable: `UnsupportedOperation` | that reason |

## Running it

```bash
make test                                   # everything offline: synthetic contract suite + webhooks
make stripe-verify                          # LIVE sandbox (opt-in): needs sk_test_ key in .env + Docker
.venv/bin/python -m praxis.payments --database-url URL inbox     # inbox counts by status
.venv/bin/python -m praxis.payments --database-url URL process --project <pubsub project>
.venv/bin/python -m praxis.payments --database-url URL requeue   # failed -> pending after a fix
```

`make stripe-verify` starts the Stripe CLI in Docker (`stripe/stripe-cli:v1.53.0`,
`stripe listen --forward-to`), a uvicorn server and a fresh Postgres database, runs the four
contract scenarios (happy path, decline then recovery, Test Clock renewal then cancellation,
idempotent re-enrolment), replays the real events reversed and duplicated into an empty
control plane, deletes every Test Clock it created, and writes
`data/stripe/verify-report.json` (scenario states, Stripe request counts, inbox counts).

## Safety rules

* Sandbox only; never load-test Stripe (four customers per verification run). Scale and
  chaos use the synthetic provider.
* Only synthetic identifiers are sent to Stripe (metadata `praxis_*`); no names, emails or
  addresses. The inbox stores routing fields and a body checksum, never the payload.
* Keys and webhook secrets are `SecretStr` settings; they never appear in logs, errors,
  reports or command lines (the Stripe CLI receives the key through an environment
  variable).
