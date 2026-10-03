# ADR 0003: Stripe behind a `PaymentGateway` abstraction

Status: Accepted (Phase 0)

## Context
Stripe Sandbox has lower rate limits than live mode and must not be load tested. Praxis
still needs a real integration and million-customer scale tests.

## Decision
All payment operations go through a `PaymentGateway` interface with two implementations:
`StripeGateway` (small representative cohort, real Sandbox) and `SyntheticPaymentGateway`
(deterministic failures and latency, used for scale and chaos). Both emit identical
internal payment event contracts, and a shared contract test suite runs against both.

## Consequences
* Business logic never imports Stripe types.
* Large-scale tests cannot touch Stripe by construction.
* Behaviours Stripe cannot express are scoped out of the shared contract suite explicitly.
