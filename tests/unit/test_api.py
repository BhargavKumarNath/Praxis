from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.tracing import CORRELATION_HEADER, new_correlation_id


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()))


def test_backend_boots_and_reports_health(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["environment"] == "local"


def test_correlation_id_generated_when_absent(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert len(resp.headers[CORRELATION_HEADER]) == 36


def test_valid_inbound_correlation_id_is_echoed(client: TestClient) -> None:
    cid = new_correlation_id()
    assert (
        client.get("/healthz", headers={CORRELATION_HEADER: cid}).headers[CORRELATION_HEADER] == cid
    )


def test_malformed_inbound_correlation_id_is_replaced(client: TestClient) -> None:
    resp = client.get("/healthz", headers={CORRELATION_HEADER: "evil\nvalue"})
    assert resp.headers[CORRELATION_HEADER] != "evil\nvalue"
    assert len(resp.headers[CORRELATION_HEADER]) == 36


def test_unhandled_error_hides_trace_and_carries_correlation_id() -> None:
    app = create_app(Settings())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals")

    resp = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"] == "internal_error"
    assert "secret internals" not in resp.text
    assert resp.json()["correlation_id"] == resp.headers[CORRELATION_HEADER]
    logging.getLogger().handlers.clear()
