from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from praxis.control.store import ControlPlaneStore, EventRecord
from praxis.dunning.service import DunningEvent, DunningService
from praxis.dunning.store import DunningRepo
from praxis.dunning.tasks import LocalTaskQueue
from praxis.recovery.artifact import RecoveryArtifact
from praxis.recovery.config import load_policy
from praxis.recovery.policy import RecoveryDecider

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)
POLICY = load_policy()
NS = uuid.UUID("2b7e1f0a-1111-4c2d-8e9f-0a1b2c3d4e5f")
CUST = "cust_d8"


def eid(*parts: object) -> str:
    return str(uuid.uuid5(NS, ":".join(map(str, parts))))


def failed(
    invoice: str,
    n: int,
    at: datetime,
    *,
    reason: str = "insufficient_funds",
    amount: int = 19_900,
    provider: str | None = "synthetic",
    customer: str = CUST,
) -> DunningEvent:
    return DunningEvent(
        eid("fail", invoice, n, at.isoformat()),
        "payment.failed",
        customer,
        at,
        {
            "invoice_id": invoice,
            "attempt_number": n,
            "amount_minor": amount,
            "currency": "GBP",
            "reason": reason,
            "final": False,
        },
        "a" * 32,
        eid("corr", invoice),
        provider,
    )


def succeeded(invoice: str, n: int, at: datetime, *, customer: str = CUST) -> DunningEvent:
    return DunningEvent(
        eid("ok", invoice, n),
        "payment.succeeded",
        customer,
        at,
        {"invoice_id": invoice, "attempt_number": n, "amount_minor": 19_900, "currency": "GBP"},
        "a" * 32,
        eid("corr", invoice),
        "synthetic",
    )


def churned(at: datetime, customer: str = CUST) -> DunningEvent:
    return DunningEvent(
        eid("churn", customer),
        "churn.observed",
        customer,
        at,
        {"reason": "voluntary", "tenure_days": 10},
        "a" * 32,
        eid("c"),
        None,
    )


def record(event_type: str, entity: str, at: datetime, payload: dict[str, Any]) -> EventRecord:
    return EventRecord(
        eid(event_type, entity, at.isoformat(), str(payload)),
        event_type,
        entity,
        at,
        payload,
        "a" * 32,
        eid("corr", entity),
        None,
    )


def seed_history(
    store: ControlPlaneStore, invoice: str, *, customer: str = CUST, at: datetime = T0
) -> None:
    """customer.created + invoice.created in the control plane (feature inputs)."""
    store.apply_event(
        "operational",
        record(
            "customer.created",
            customer,
            at - timedelta(days=40),
            {
                "region_id": "eu_west",
                "industry": "saas",
                "tier": "growth",
                "preferred_payment_method": "card",
                "is_existing": True,
                "tenure_days": 300,
            },
        ),
    )
    store.apply_event(
        "operational",
        record(
            "invoice.created",
            customer,
            at - timedelta(hours=1),
            {
                "invoice_id": invoice,
                "amount_minor": 19_900,
                "currency": "GBP",
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "tier": "growth",
            },
        ),
    )


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_service(
    engine: Engine,
    *,
    artifact: RecoveryArtifact | None = None,
    queue: LocalTaskQueue | None = None,
    clock: Clock | None = None,
) -> DunningService:
    return DunningService(
        engine,
        RecoveryDecider(POLICY, artifact),
        queue or LocalTaskQueue(),
        clock=clock or Clock(),
    )


def rows(engine: Engine, sql: str, **params: object) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).all()]


def case_state(engine: Engine, invoice: str) -> str | None:
    with engine.connect() as conn:
        c = DunningRepo(conn).case(invoice)
    return c.state.value if c else None


Check = Callable[[], bool]
