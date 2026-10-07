"""Deterministic identifiers, idempotency keys and model validation."""

from __future__ import annotations

import uuid

import pytest

from praxis.payments.ids import derived_event_id, flow_correlation_id, idempotency_key
from praxis.payments.model import PaymentBehaviour, Plan
from praxis.payments.service import BillingService
from praxis.payments.synthetic import SyntheticPaymentGateway
from tests.payments.helpers import PLAN, T0, profile


def test_derived_ids_are_stable_uuids_and_collision_safe() -> None:
    a = derived_event_id("stripe", "invoice", "in_1", "created")
    assert a == derived_event_id("stripe", "invoice", "in_1", "created")
    assert uuid.UUID(a).version == 5
    assert a != derived_event_id("synthetic", "invoice", "in_1", "created")
    for bad in (("stripe", "invoice", "in:1", "created"), ("stripe", "", "in_1", "created")):
        with pytest.raises(ValueError, match="invalid id component"):
            derived_event_id(*bad)


def test_correlation_ids_are_valid_and_per_flow() -> None:
    one = flow_correlation_id("stripe", "invoice", "in_1")
    assert str(uuid.UUID(one)) == one and one != flow_correlation_id("stripe", "invoice", "in_2")


def test_idempotency_keys_are_intent_hashes() -> None:
    key = idempotency_key("subscribe", "cust_a", "praxis_growth_gbp_4900_month")
    assert key == idempotency_key("subscribe", "cust_a", "praxis_growth_gbp_4900_month")
    assert key.startswith("praxis-subscribe-") and len(key) <= 255
    assert key != idempotency_key("subscribe", "cust_a", "praxis_growth_gbp_5900_month")
    for op, parts in (("not an op", ("a",)), ("ok", ()), ("ok", ("",)), ("ok", ("a\0b",))):
        with pytest.raises(ValueError, match="idempotency key"):
            idempotency_key(op, *parts)
    with pytest.raises(ValueError, match="too long"):
        idempotency_key("x" * 300, "a")


def test_plan_validation_and_keys() -> None:
    assert PLAN.lookup_key == "praxis_growth_gbp_4900_month" and PLAN.product_key == "praxis_growth"
    with pytest.raises(ValueError, match="base_fee_minor"):
        Plan(tier="growth", products=("a",), base_fee_minor=0)
    with pytest.raises(ValueError, match="product"):
        Plan(tier="growth", products=(), base_fee_minor=100)


def test_enrolment_refuses_a_plan_for_another_tier() -> None:
    service = BillingService(SyntheticPaymentGateway(start=T0), publisher=_Discard())
    with pytest.raises(ValueError, match="tier"):
        service.enrol(profile("cust_a", tier="starter"), PLAN, PaymentBehaviour.SUCCEEDS)


class _Discard:
    def publish(self, events: object, /) -> object:
        return None
