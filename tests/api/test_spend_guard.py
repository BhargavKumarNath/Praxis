"""Spend guard (ADR 0026): a budget notification unlinks billing at the GBP ceiling, and only then.

The guard is the automatic shutdown behind the GBP 10 project budget. These tests pin:
the parse of Google's budget notification (schema 1.0, Pub/Sub push envelope), the decision
(only the named GBP budget, only at actual cost >= the guard), the Pub/Sub ack semantics
(unusable messages are acked, a failed billing call is retried), and the Cloud Billing calls.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from praxis.api.spend_guard import (
    BILLING_API,
    BudgetState,
    CloudBilling,
    create_guard_app,
    decide,
    parse_push,
)
from praxis.config import Environment, Settings
from praxis.errors import TransientError

BUDGET = "praxis-dev-monthly"


def push(**fields: Any) -> dict[str, Any]:
    note = {
        "budgetDisplayName": BUDGET,
        "costAmount": 1.25,
        "costIntervalStart": "2026-11-01T07:00:00Z",
        "budgetAmount": 10.0,
        "budgetAmountType": "SPECIFIED_AMOUNT",
        "currencyCode": "GBP",
    } | fields
    data = base64.b64encode(json.dumps(note).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "s"}


class FakeBilling:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.disabled: list[str] = []

    def billing_enabled(self, project_id: str) -> bool:
        if self.fail:
            raise TransientError("billing API answered 503")
        return self.enabled

    def disable_billing(self, project_id: str) -> None:
        self.disabled.append(project_id)
        self.enabled = False


def client(billing: FakeBilling, **settings: Any) -> TestClient:
    base: dict[str, Any] = {
        "environment": Environment.DEV,
        "gcp_project_id": "praxis-p",
        "spend_guard_kill_at_gbp": Decimal(8),
        "spend_guard_budget_name": BUDGET,
    }
    return TestClient(create_guard_app(Settings(**(base | settings)), billing))


# --- parse --------------------------------------------------------------------------------
def test_parse_reads_amounts_as_decimals() -> None:
    state = parse_push(push(costAmount=7.99))
    assert state == BudgetState(BUDGET, Decimal("7.99"), Decimal("10.0"), "GBP")


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"message": "x"},
        {"message": {"data": "not base64!"}},
        {"message": {"data": base64.b64encode(b"[1]").decode()}},
        push(costAmount="NaN"),
        push(costAmount=-1),
        push(costAmount="eight"),
        push(budgetDisplayName=None),
    ],
)
def test_parse_rejects_anything_that_is_not_a_budget_notification(body: Any) -> None:
    with pytest.raises(ValueError):
        parse_push(body)


# --- decide -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("cost", "action"),
    [("0", "hold"), ("7.99", "hold"), ("8", "disable"), ("8.00", "disable"), ("42", "disable")],
)
def test_decide_acts_exactly_at_the_guard(cost: str, action: str) -> None:
    state = BudgetState(BUDGET, Decimal(cost), Decimal(10), "GBP")
    assert decide(state, budget_name=BUDGET, kill_at=Decimal(8)).action == action


@pytest.mark.parametrize(
    ("state", "name", "kill_at", "reason"),
    [
        (BudgetState("other", Decimal(99), Decimal(10), "GBP"), BUDGET, Decimal(8), "budget"),
        (BudgetState(BUDGET, Decimal(99), Decimal(10), "USD"), BUDGET, Decimal(8), "currency"),
        (BudgetState(BUDGET, Decimal(99), Decimal(10), "GBP"), None, Decimal(8), "configured"),
        (BudgetState(BUDGET, Decimal(99), Decimal(10), "GBP"), BUDGET, None, "configured"),
    ],
)
def test_decide_ignores_other_budgets_currencies_and_an_unconfigured_guard(
    state: BudgetState, name: str | None, kill_at: Decimal | None, reason: str
) -> None:
    verdict = decide(state, budget_name=name, kill_at=kill_at)
    assert verdict.action == "ignore" and reason in verdict.reason


# --- the HTTP endpoint (Pub/Sub push) -----------------------------------------------------
def test_below_the_guard_nothing_happens() -> None:
    billing = FakeBilling()
    response = client(billing).post("/v1/budget", json=push(costAmount=7.5))
    assert response.status_code == 200 and response.json()["action"] == "hold"
    assert billing.disabled == []


def test_at_the_guard_billing_is_unlinked_once() -> None:
    billing = FakeBilling()
    c = client(billing)
    first = c.post("/v1/budget", json=push(costAmount=8.01))
    assert first.status_code == 200 and first.json()["action"] == "disable"
    assert billing.disabled == ["praxis-p"]
    again = c.post("/v1/budget", json=push(costAmount=8.5))  # later notifications: no-op
    assert again.status_code == 200 and billing.disabled == ["praxis-p"]


def test_a_failed_billing_call_is_retried_by_pubsub() -> None:
    response = client(FakeBilling(fail=True)).post("/v1/budget", json=push(costAmount=9))
    assert response.status_code == 503 and response.json()["action"] == "retry"


def test_unusable_messages_are_acked_not_retried_forever() -> None:
    billing = FakeBilling()
    c = client(billing)
    assert c.post("/v1/budget", json={"message": "x"}).json()["action"] == "ignore"
    assert c.post("/v1/budget", content=b"not json").status_code == 200
    assert c.post("/v1/budget", json=push(budgetDisplayName="x", costAmount=99)).status_code == 200
    assert billing.disabled == []


def test_without_a_project_the_guard_never_calls_billing() -> None:
    billing = FakeBilling()
    settings = Settings(spend_guard_kill_at_gbp=Decimal(8), spend_guard_budget_name=BUDGET)
    c = TestClient(create_guard_app(settings, billing))
    assert c.post("/v1/budget", json=push(costAmount=99)).status_code == 200
    assert billing.disabled == []


def test_health_and_no_public_schema_outside_local() -> None:
    c = client(FakeBilling())
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/openapi.json").status_code == 404


def test_the_guard_ceiling_is_validated_in_settings() -> None:
    with pytest.raises(ValueError):
        Settings(spend_guard_kill_at_gbp=Decimal(11))


# --- Cloud Billing calls ------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, *, json: Any, timeout: float) -> FakeResponse:
        self.calls.append((method, url, json))
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_cloud_billing_reads_and_unlinks_the_project() -> None:
    session = FakeSession(FakeResponse(200, {"billingEnabled": True}), FakeResponse(200, {}))
    billing = CloudBilling(session)
    assert billing.billing_enabled("p") is True
    billing.disable_billing("p")
    url = f"{BILLING_API}/projects/p/billingInfo"
    assert session.calls == [("GET", url, None), ("PUT", url, {"billingAccountName": ""})]


@pytest.mark.parametrize(
    "failure",
    [
        FakeResponse(403, {"error": "denied"}),
        requests.ConnectionError("down"),
        RefreshError("no token"),  # type: ignore[no-untyped-call]  # google-auth is untyped
    ],
)
def test_cloud_billing_failures_are_transient(failure: FakeResponse | Exception) -> None:
    with pytest.raises(TransientError):
        CloudBilling(FakeSession(failure)).billing_enabled("p")
