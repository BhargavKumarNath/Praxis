from __future__ import annotations

import logging

import httpx
import pytest

from praxis.data.errors import SourceRejectedError, SourceUnavailableError
from praxis.data.fetch import HttpFetcher, RetryPolicy, build_endpoint, redact_params
from tests.data.helpers import FAKE_FRED_KEY, make_fetcher

URL = "https://api.example.test/v1/x"


def test_success_returns_body_and_redacted_endpoint() -> None:
    f = make_fetcher(lambda r: httpx.Response(200, content=b"ok"))
    out = f.get(URL, {"series_id": "A", "api_key": FAKE_FRED_KEY})
    assert out.body == b"ok"
    assert FAKE_FRED_KEY not in out.endpoint
    assert "api_key=%5BREDACTED%5D" in out.endpoint or "api_key=[REDACTED]" in out.endpoint


def test_retry_after_honoured_and_capped_then_succeeds() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "9999"})
        return httpx.Response(200, content=b"ok")

    f = make_fetcher(handler, sleeps)
    assert f.get(URL, {}).body == b"ok"
    assert len(calls) == 2
    assert sleeps == [RetryPolicy().retry_after_cap_s]  # 9999 capped to 30


def test_5xx_retries_are_bounded_with_capped_exponential_backoff() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpFetcher(client, RetryPolicy(max_attempts=5, backoff_cap_s=3.0), sleeps.append)
    with pytest.raises(SourceUnavailableError):
        fetcher.get(URL, {})
    assert len(calls) == 5
    assert sleeps == [1.0, 2.0, 3.0, 3.0]  # no sleep after the final attempt


def test_timeout_is_retried_boundedly_and_reported_without_leaking_url_params() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    f = make_fetcher(handler)
    with pytest.raises(SourceUnavailableError) as err:
        f.get(URL, {"api_key": FAKE_FRED_KEY})
    assert len(calls) == RetryPolicy().max_attempts
    assert FAKE_FRED_KEY not in str(err.value)
    assert "ReadTimeout" in str(err.value)


def test_non_retryable_4xx_fails_immediately() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": True, "reason": "bad"})

    with pytest.raises(SourceRejectedError):
        make_fetcher(handler).get(URL, {})
    assert len(calls) == 1


def test_http_date_retry_after_falls_back_to_backoff() -> None:
    assert RetryPolicy().delay(2, "Wed, 21 Oct 2026 07:28:00 GMT") == 2.0
    assert RetryPolicy().delay(1, "-5") == 0.0


def test_policy_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


def test_credentials_never_logged_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    f = make_fetcher(lambda r: httpx.Response(503))
    with pytest.raises(SourceUnavailableError):
        f.get(URL, {"api_key": FAKE_FRED_KEY})
    assert FAKE_FRED_KEY not in caplog.text
    assert caplog.records  # failures were logged


def test_redact_and_build_endpoint_handle_list_params() -> None:
    params: dict[str, str | list[str]] = {"facets[x][]": ["NY"], "API_KEY": "secret", "a": "1"}
    assert redact_params(params)["API_KEY"] == "[REDACTED]"
    assert "secret" not in build_endpoint(URL, params)
    assert build_endpoint(URL, {}) == URL
