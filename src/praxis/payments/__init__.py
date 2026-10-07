"""Payment provider integration (Phase 7): ``PaymentGateway`` with Stripe and synthetic backends.

Business logic never sees provider types: both gateways produce provider-neutral snapshots
(``model``), and one pure function (``normalise``) turns a snapshot into the internal event
contracts that the Phase 3 pipeline already applies idempotently and order-independently.
"""
