"""Stripe REST client: only the HTTP boundary is replaced (``httpx.MockTransport``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qsl

import httpx
import pytest

from praxis.errors import TransientError
from praxis.payments.gateway import GatewayError, IdempotencyConflict
from praxis.payments.stripe_client import (
    StripeApiError,
    StripeCardError,
    StripeClient,
    StripeTransientError,
    classify,
    encode_form,
)
from tests.payments.helpers import sandbox_key

KEY = sandbox_key()
VERSION = "2026-09-30.endive"
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, sleeps: list[float] | None = None, **kwargs: object) -> StripeClient:
    record = sleeps if sleeps is not None else []
    return StripeClient(
        KEY,
        api_version=VERSION,
        transport=httpx.MockTransport(handler),
        sleep=record.append,
        **kwargs,  # type: ignore[arg-type]
    )


def error(status: int, headers: dict[str, str] | None = None, **err: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": err}, headers={"Request-Id": "req_1", **(headers or {})}
    )


def test_headers_auth_version_and_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "cus_1"})

    c = client(handler)
    assert c.post("/v1/customers", {"metadata": {"a": "b"}}, idempotency_key="praxis-k1") == {
        "id": "cus_1"
    }
    c.get("/v1/customers/cus_1")
    post, get = seen
    assert post.headers["Authorization"] == f"Bearer {KEY}"
    assert post.headers["Stripe-Version"] == VERSION
    assert post.headers["Idempotency-Key"] == "praxis-k1"
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(post.content.decode())) == {"metadata[a]": "b"}
    assert "Idempotency-Key" not in get.headers
    c.close()


def test_form_encoding_matches_stripe_conventions() -> None:
    assert encode_form(
        {
            "customer": "cus_1",
            "items": [{"price": "price_1"}],
            "expand": ["payments"],
            "metadata": {"k": "v"},
            "flag": True,
            "off": False,
            "amount": 4900,
            "skip": None,
        }
    ) == [
        ("customer", "cus_1"),
        ("items[0][price]", "price_1"),
        ("expand[0]", "payments"),
        ("metadata[k]", "v"),
        ("flag", "true"),
        ("off", "false"),
        ("amount", "4900"),
    ]
    with pytest.raises(TypeError):
        encode_form({"bad": 1.5})


def test_get_sends_query_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params.get_list("expand[0]") == ["payments"]
        return httpx.Response(200, json={"id": "in_1"})

    client(handler).get("/v1/invoices/in_1", {"expand": ["payments"]})


@pytest.mark.parametrize(
    "response",
    [
        error(429, type="invalid_request_error", code="rate_limit"),
        error(500, type="api_error"),
        error(409, type="invalid_request_error", code="lock_timeout"),
        error(400, {"Stripe-Should-Retry": "true"}, type="invalid_request_error"),
    ],
)
def test_transient_failures_are_retried_with_the_same_key(response: httpx.Response) -> None:
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return response if len(keys) < 3 else httpx.Response(200, json={"id": "sub_1"})

    sleeps: list[float] = []
    assert (
        client(handler, sleeps).post("/v1/subscriptions", idempotency_key="praxis-sub")["id"]
        == "sub_1"
    )
    assert keys == ["praxis-sub"] * 3
    assert sleeps == [0.5, 1.0]


def test_retries_are_bounded() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(503, type="api_error")

    with pytest.raises(StripeTransientError) as info:
        client(handler, max_retries=2).post("/v1/x", idempotency_key="k")
    assert len(calls) == 3 and info.value.status == 503 and info.value.request_id == "req_1"
    assert isinstance(info.value, TransientError)


def test_should_retry_false_overrides_a_server_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(500, {"Stripe-Should-Retry": "false"}, type="api_error")

    with pytest.raises(StripeApiError):
        client(handler).post("/v1/x", idempotency_key="k")
    assert len(calls) == 1


def test_transport_errors_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    sleeps: list[float] = []
    with pytest.raises(StripeTransientError, match="ConnectTimeout"):
        client(handler, sleeps, max_retries=1).get("/v1/customers/cus_1")
    assert sleeps == [0.5]


def test_card_error_is_a_402_with_decline_code() -> None:
    response = error(
        402, type="card_error", code="card_declined", decline_code="insufficient_funds"
    )
    exc = classify(response)
    assert isinstance(exc, StripeCardError)
    assert (exc.status, exc.code, exc.decline_code, exc.request_id) == (
        402,
        "card_declined",
        "insufficient_funds",
        "req_1",
    )


def test_idempotency_misuse_is_a_conflict() -> None:
    assert isinstance(classify(error(400, type="idempotency_error")), IdempotencyConflict)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_permanent(status: int) -> None:
    exc = classify(error(status, type="invalid_request_error", code="resource_missing"))
    assert isinstance(exc, StripeApiError) and not isinstance(exc, StripeCardError)
    assert exc.reason == "resource_missing" and exc.status == status


def test_unparseable_error_body_still_classifies() -> None:
    exc = classify(httpx.Response(418, text="<html>teapot</html>"))
    assert isinstance(exc, StripeApiError) and exc.reason == "http_418"
    assert classify(httpx.Response(200, json={})) is None


def test_errors_never_contain_the_key_or_the_request_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error(
            400,
            type="invalid_request_error",
            code="parameter_invalid",
            message=request.content.decode(),
        )

    with pytest.raises(StripeApiError) as info:
        client(handler).post(
            "/v1/payment_methods/pm/attach", {"card": "4242424242424242"}, idempotency_key="k"
        )
    text = str(info.value)
    assert KEY not in text and "4242" not in text and "req_1" in text


def test_list_all_paginates_and_is_bounded() -> None:
    pages = {
        None: {"data": [{"id": "ch_1"}, {"id": "ch_2"}], "has_more": True},
        "ch_2": {"data": [{"id": "ch_3"}], "has_more": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "100"
        return httpx.Response(200, json=pages[request.url.params.get("starting_after")])

    assert [c["id"] for c in client(handler).list_all("/v1/charges")] == ["ch_1", "ch_2", "ch_3"]

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "ch_x"}], "has_more": True})

    with pytest.raises(GatewayError, match="pagination_limit"):
        list(client(endless).list_all("/v1/charges", max_pages=3))

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": True})

    assert list(client(empty).list_all("/v1/charges")) == []


def test_delete_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(200, content=json.dumps({"id": "clock_1", "deleted": True}))

    assert client(handler).delete("/v1/test_helpers/test_clocks/clock_1")["deleted"] is True


@pytest.mark.parametrize("key", ["sk" + "_live_abc", "pk" + "_test_abc", ""])
def test_only_test_mode_secret_keys_are_accepted(key: str) -> None:
    with pytest.raises(ValueError, match="test-mode"):
        StripeClient(key, api_version=VERSION)


def test_api_version_must_be_pinned() -> None:
    with pytest.raises(ValueError, match="API version"):
        StripeClient(KEY, api_version="")
