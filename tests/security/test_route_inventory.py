"""Phase 15 security review: every HTTP route is inventoried with its protection.

A new route fails this test until it is classified here (and, if it writes, proven to refuse
an unauthenticated caller). Classes:

* ``public-read``: no state change; safe to expose (health, incidents list, forecasts).
* ``stripe-signature``: Stripe webhook; HMAC signature over the raw body (ADR 0014).
* ``task-token``: Cloud Tasks callback; shared token in ``X-Praxis-Task-Token`` (ADR 0015).
* ``private-service``: the decision console's read model. The console service is never
  public: Terraform grants ``run.invoker`` to named principals only (``infra/terraform``,
  checked by ``terraform test``), never ``allUsers``.
* ``approver-token``: console admin writes; bearer token of a named approver (ADR 0021).
* ``guard-push``: the spend guard's budget push (ADR 0026). Its Cloud Run service is private:
  only the guard's own service account (the push subscription's OIDC identity) may invoke
  it. It acts only on the named GBP budget at actual cost >= the configured guard.

No route may delete data, set a price or run a payment (the console contract, CLAUDE.md s24).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.api.spend_guard import create_guard_app
from praxis.config import Environment, Settings
from praxis.console.app import create_console_app

API = {
    ("POST", "/v1/forecasts/demand"): "public-read",
    ("GET", "/v1/forecasts/demand/model"): "public-read",
    ("GET", "/v1/forecasts/demand/metrics"): "public-read",
    ("POST", "/v1/webhooks/stripe"): "stripe-signature",
    ("GET", "/v1/ops/health"): "public-read",
    ("GET", "/ops/dashboard"): "public-read",
    ("GET", "/v1/ops/incidents"): "public-read",
    ("POST", "/v1/tasks/payment-retry"): "task-token",
    ("GET", "/healthz"): "public-read",
}
CONSOLE_READS = (
    "executive", "pricing", "payments", "mlops", "system", "operator",
    "incidents/{incident_id}", "sessions/{session_id}", "pricing/decisions/{decision_id}",
    "whoami",
)  # fmt: skip
CONSOLE = {("GET", f"/v1/console/{p}"): "private-service" for p in CONSOLE_READS} | {
    ("POST", "/v1/console/requests/{request_id}/approve"): "approver-token",
    ("POST", "/v1/console/requests/{request_id}/reject"): "approver-token",
    ("POST", "/v1/console/freezes/{freeze_id}/lift"): "approver-token",
}
GUARD = {
    ("POST", "/v1/budget"): "guard-push",
    ("GET", "/healthz"): "public-read",
}
FORBIDDEN_WORDS = ("delete", "drop", "price", "charge", "refund", "execute", "shell", "sql")


def routes(app: FastAPI) -> set[tuple[str, str]]:
    return {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}


def test_api_routes_are_exactly_the_inventory() -> None:
    assert routes(create_app(Settings())) == set(API)


def test_console_routes_are_exactly_the_inventory() -> None:
    assert routes(create_console_app(Settings())) == set(CONSOLE)


def test_guard_routes_are_exactly_the_inventory() -> None:
    assert routes(create_guard_app(Settings(), billing=_NoBilling())) == set(GUARD)


class _NoBilling:
    def billing_enabled(self, project_id: str) -> bool:
        raise AssertionError("the inventory never reaches billing")

    def disable_billing(self, project_id: str) -> None:
        raise AssertionError("the inventory never reaches billing")


def test_no_route_is_destructive_or_carries_a_price() -> None:
    for method, path in set(API) | set(CONSOLE) | set(GUARD):
        assert method in {"GET", "POST"}, (method, path)
        assert not any(w in path.lower() for w in FORBIDDEN_WORDS), path


@pytest.mark.parametrize(
    ("app", "path", "headers", "body"),
    [
        ("api", "/v1/webhooks/stripe", {}, b"{}"),  # no signature
        ("api", "/v1/tasks/payment-retry", {}, {"job_id": "job_1"}),  # no task token
        ("console", "/v1/console/requests/r1/approve", {}, None),  # no approver token
        ("console", "/v1/console/requests/r1/reject", {"Authorization": "x"}, {"reason": "x"}),
        ("console", "/v1/console/freezes/f1/lift", {}, None),
    ],
)  # fmt: skip
def test_every_write_refuses_an_unauthenticated_caller(
    app: str, path: str, headers: dict[str, str], body: Any
) -> None:
    client = TestClient(create_app(Settings()) if app == "api" else create_console_app(Settings()))
    if isinstance(body, bytes):
        response = client.post(path, content=body, headers=headers)
    else:
        response = client.post(path, json=body, headers=headers)
    assert response.status_code in {400, 401, 403, 422, 503}, (path, response.status_code)
    assert "Traceback" not in response.text


@pytest.mark.parametrize("environment", [Environment.DEV, Environment.PROD])
@pytest.mark.parametrize("factory", [create_app, create_console_app])
def test_schema_and_docs_are_not_served_outside_local(
    environment: Environment, factory: Any
) -> None:
    settings = Settings(environment=environment, gcp_project_id="praxis-test")
    client = TestClient(factory(settings))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    local = TestClient(factory(Settings()))
    assert local.get("/openapi.json").status_code == 200
