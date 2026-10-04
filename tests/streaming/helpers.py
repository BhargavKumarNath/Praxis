"""Deterministic event builders and cached simulator streams for Phase 3 tests."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from praxis.domain.projections import LoggedEvent
from praxis.simulator.config import SimulationConfig, load_config
from praxis.simulator.engine import Engine
from praxis.simulator.events import Draft, EventFactory
from praxis.simulator.population import generate_population

T0 = 1_767_571_200  # 2026-01-05T00:00:00Z
DAY = 86_400
FACTORY = EventFactory("phase3-tests")
Event = dict[str, Any]


def ev(
    uid: str,
    event_type: str,
    entity: str,
    payload: dict[str, Any],
    offset_s: int,
    *,
    flow: str | None = None,
    cause: str | None = None,
) -> Event:
    return FACTORY.build(
        Draft(T0 + offset_s, entity, uid, event_type, payload, flow or f"life:{entity}", cause)
    )


def customer_lifecycle(
    cid: str = "cust_a", *, new: bool = True, changes: int = 1, churn: bool = True
) -> list[Event]:
    tiers = ["starter", "growth", "enterprise"]
    out = [
        ev(
            f"cust:{cid}",
            "customer.created",
            cid,
            {
                "region_id": "uk-south",
                "industry": "saas",
                "tier": "starter",
                "preferred_payment_method": "card",
                "is_existing": not new,
                "tenure_days": 0 if new else 400,
            },
            60,
        )
    ]
    if new:
        out.append(
            ev(
                f"conv:{cid}",
                "conversion.observed",
                cid,
                {"converted": True, "price_index_milli": 1000},
                180,
                cause=f"cust:{cid}",
            )
        )
    out.append(
        ev(
            f"sub:{cid}",
            "subscription.started",
            cid,
            {
                "tier": "starter",
                "products": ["gpu-inference"],
                "origin": "new" if new else "existing",
                "base_fee_minor": 4_900,
                "billing_period_days": 30,
            },
            240,
        )
    )
    for k in range(changes):
        out.append(
            ev(
                f"chg:{cid}:{k}",
                "subscription.changed",
                cid,
                {
                    "from_tier": tiers[k % 3],
                    "to_tier": tiers[(k + 1) % 3],
                    "base_fee_minor": 9_900 + k,
                },
                (k + 1) * DAY + 12 * 3600,
            )
        )
    if churn:
        out.append(
            ev(
                f"churn:{cid}",
                "churn.observed",
                cid,
                {"reason": "voluntary", "tenure_days": 30},
                (changes + 2) * DAY,
            )
        )
    return out


def invoice_lifecycle(
    cid: str = "cust_a",
    inv: str = "inv_1",
    *,
    fail_first: int = 0,
    max_attempts: int = 3,
    amount: int = 12_345,
) -> list[Event]:
    """``fail_first`` failed attempts, then success unless that reaches ``max_attempts``."""
    flow = f"inv:{inv}"
    out = [
        ev(
            f"inv:{inv}",
            "invoice.created",
            cid,
            {
                "invoice_id": inv,
                "amount_minor": amount,
                "currency": "GBP",
                "period_start": "2026-01-05",
                "period_end": "2026-02-03",
                "tier": "starter",
            },
            3600,
            flow=flow,
        )
    ]
    prev = f"inv:{inv}"
    base = {"invoice_id": inv, "amount_minor": amount, "currency": "GBP"}
    for n in range(1, max_attempts + 1):
        day = (n - 1) * 3 * DAY
        out.append(
            ev(
                f"patt:{inv}:{n}",
                "payment.attempted",
                cid,
                {**base, "attempt_number": n, "payment_method": "card"},
                day + 3660,
                flow=flow,
                cause=prev,
            )
        )
        if n > fail_first:
            out.append(
                ev(
                    f"pok:{inv}:{n}",
                    "payment.succeeded",
                    cid,
                    {**base, "attempt_number": n},
                    day + 3720,
                    flow=flow,
                    cause=f"patt:{inv}:{n}",
                )
            )
            return out
        prev = f"pfail:{inv}:{n}"
        out.append(
            ev(
                prev,
                "payment.failed",
                cid,
                {
                    **base,
                    "attempt_number": n,
                    "reason": "card_declined",
                    "final": n == max_attempts,
                },
                day + 3720,
                flow=flow,
                cause=f"patt:{inv}:{n}",
            )
        )
    return out


def logged(event: Event) -> LoggedEvent:
    return LoggedEvent(
        event_id=event["event_id"],
        event_type=event["event_type"],
        entity_id=event["entity_id"],
        occurred_at=datetime.fromisoformat(event["occurred_at"]),
        payload=event["payload"],
    )


def sim_config(n_customers: int, days: int) -> SimulationConfig:
    return load_config().with_overrides(n_customers=n_customers, days=days)


@lru_cache(maxsize=4)
def sim_events(n_customers: int = 200, days: int = 56, seed: int = 42) -> tuple[Event, ...]:
    cfg = sim_config(n_customers, days)
    return tuple(Engine(cfg, seed, generate_population(cfg, seed)).run())


def command_output(stdout: str) -> Any:
    """Parse a CLI's JSON output, ignoring structured log lines (also JSON, on stdout)."""
    import json

    kept = []
    for line in stdout.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if not (isinstance(parsed, dict) and {"level", "logger", "message"} <= parsed.keys()):
            kept.append(line)
    return json.loads("\n".join(kept))
