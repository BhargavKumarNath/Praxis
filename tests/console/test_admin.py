"""The admin boundary (required_test.md s19): an impactful action needs a server-side approver
token, the token (not the request) names the approver, the action runs only through the
existing approval paths, and no console route can set a price."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from praxis.pricing.freeze import PostgresFreezeStore
from tests.console.conftest import Console, make_console
from tests.console.journey import run_journey

pytestmark = pytest.mark.integration


def approve(console: Console, request_id: str, headers: dict[str, str] | None = None) -> object:
    return console.client.post(f"/v1/console/requests/{request_id}/approve", headers=headers or {})


@pytest.mark.parametrize("header", [None, "Bearer ", "Basic abc", "Bearer not-a-real-token"])
def test_approval_without_a_valid_token_is_refused(console: Console, header: str | None) -> None:
    journey = run_journey(console.engine)
    headers = {"Authorization": header} if header is not None else {}
    response = console.client.post(
        f"/v1/console/requests/{journey.request_id}/approve", headers=headers
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"] == "unauthorized" and response.json()["correlation_id"]
    assert PostgresFreezeStore(console.engine).active() == []  # nothing happened


def test_authorized_approval_runs_the_existing_freeze_path(console: Console) -> None:
    journey = run_journey(console.engine)
    response = console.client.post(
        f"/v1/console/requests/{journey.request_id}/approve", headers=console.auth()
    )
    assert response.status_code == 200, response.text
    done = response.json()["request"]
    assert done["status"] == "completed" and done["decided_by"] == "alice"
    (freeze,) = PostgresFreezeStore(console.engine).active()
    assert freeze.approved_by == "alice" and freeze.requested_by == "praxis-agent"
    assert done["outcome"]["freeze"]["freeze_id"] == freeze.freeze_id

    again = console.client.post(
        f"/v1/console/requests/{journey.request_id}/approve", headers=console.auth()
    )
    assert again.status_code == 409 and again.json()["error"] == "refused"


def test_the_body_cannot_choose_the_approver(console: Console) -> None:
    journey = run_journey(console.engine)
    response = console.client.post(
        f"/v1/console/requests/{journey.request_id}/approve?approver=mallory",
        headers=console.auth(console.other_token),
        json={"approver": "mallory"},
    )
    assert response.status_code == 200
    assert response.json()["request"]["decided_by"] == "bob"


def test_reject_needs_a_reason_and_freezes_nothing(console: Console) -> None:
    journey = run_journey(console.engine)
    path = f"/v1/console/requests/{journey.request_id}/reject"
    missing = console.client.post(path, headers=console.auth(), json={})
    assert missing.status_code == 422 and missing.json()["error"] == "invalid_request"
    done = console.client.post(path, headers=console.auth(), json={"reason": "false alarm"})
    assert done.status_code == 200
    assert done.json()["request"]["status"] == "rejected"
    assert PostgresFreezeStore(console.engine).active() == []


def test_lift_freeze_is_guarded_and_named(console: Console) -> None:
    journey = run_journey(console.engine)
    approve(console, journey.request_id, console.auth())
    (freeze,) = PostgresFreezeStore(console.engine).active()
    path = f"/v1/console/freezes/{freeze.freeze_id}/lift"
    assert console.client.post(path).status_code == 401
    lifted = console.client.post(path, headers=console.auth(console.other_token))
    assert lifted.status_code == 200 and lifted.json()["freeze"]["lifted_by"] == "bob"
    assert console.client.post(path, headers=console.auth()).status_code == 404
    with console.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pricing_freezes")).scalar() == 1


def test_unknown_request_is_404(console: Console) -> None:
    response = console.client.post("/v1/console/requests/req_nope/approve", headers=console.auth())
    assert response.status_code == 404


def test_admin_is_off_without_configured_approvers(console: Console, tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = make_console(console.engine, tmp_path / "absent.duckdb")
    with client:
        response = client.post("/v1/console/requests/r/approve", headers=console.auth())
        whoami = client.get("/v1/console/whoami", headers=console.auth())
    assert response.status_code == 503 and response.json()["error"] == "admin_not_configured"
    assert whoami.status_code == 503


def test_whoami_names_the_token_holder(console: Console) -> None:
    assert console.client.get("/v1/console/whoami", headers=console.auth()).json() == {
        "approver": "alice"
    }
    assert console.client.get("/v1/console/whoami").status_code == 401


def test_no_console_route_can_set_a_price_or_execute(console: Console) -> None:
    schema = console.client.get("/openapi.json").json()
    writes = sorted(path for path, ops in schema["paths"].items() if set(ops) - {"get", "head"})
    assert writes == [
        "/v1/console/freezes/{freeze_id}/lift",
        "/v1/console/requests/{request_id}/approve",
        "/v1/console/requests/{request_id}/reject",
    ]
    text_ = console.client.get("/openapi.json").text
    assert "price_micros" not in text_ and "execute" not in text_.lower()
