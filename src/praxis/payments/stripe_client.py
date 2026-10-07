"""Minimal Stripe REST client (httpx), pinned API version, test-mode keys only.

Why not the official SDK: Praxis needs about ten endpoints, explicit idempotency keys,
explicit timeouts, explicit retry classification and an HTTP boundary that tests can
replace (``httpx.MockTransport``). A thin client keeps all of that visible (ADR 0014).

* ``Stripe-Version`` is pinned (``Settings.stripe_api_version``), so responses do not change
  shape when the account default moves.
* Only ``sk_test_`` / ``rk_test_`` keys are accepted: Praxis never touches live money, and a
  misconfigured live key fails at construction, before any request.
* POSTs carry an ``Idempotency-Key``; retries of a request reuse it, so a retried write can
  never execute twice (docs.stripe.com/api/idempotent_requests).
* Retries are bounded (``max_retries``) and only for transient failures: connection errors,
  timeouts, 409 ``lock_timeout``, 429 and 5xx, unless Stripe says ``Stripe-Should-Retry:
  false``. Everything else is permanent. A 402 is a declined payment (``StripeCardError``).
* Error messages carry status, type, code and request id: never the key, the request body or
  card details.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from praxis.errors import TransientError
from praxis.payments.gateway import GatewayError, IdempotencyConflict

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.stripe.com"
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
_STATUS_PAYMENT_REQUIRED = 402
_STATUS_CONFLICT = 409
_STATUS_RATE_LIMITED = 429
_STATUS_SERVER_ERROR = 500


class StripeTransientError(TransientError):
    def __init__(
        self, detail: str, *, status: int | None = None, request_id: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.request_id = request_id


class StripeApiError(GatewayError):
    def __init__(
        self,
        status: int,
        *,
        error_type: str | None,
        code: str | None,
        decline_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        detail = f"status={status} type={error_type} code={code} request_id={request_id}"
        super().__init__(code or error_type or f"http_{status}", detail)
        self.status = status
        self.error_type = error_type
        self.code = code
        self.decline_code = decline_code
        self.request_id = request_id


class StripeCardError(StripeApiError):
    """402: the payment was declined. A business outcome, not an integration failure."""


def encode_form(params: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: ``a[b]=1``, ``items[0][price]=p``, booleans as true/false."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, Mapping):
            pairs.extend(encode_form(value, name))
        elif isinstance(value, list | tuple):
            for i, item in enumerate(value):
                if isinstance(item, Mapping):
                    pairs.extend(encode_form(item, f"{name}[{i}]"))
                else:
                    pairs.append((f"{name}[{i}]", _scalar(item)))
        else:
            pairs.append((name, _scalar(value)))
    return pairs


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | str):
        return str(value)
    raise TypeError(f"unsupported form value type {type(value).__name__}")


def classify(response: httpx.Response) -> Exception | None:
    """``None`` for 2xx; otherwise the error to raise (transient or permanent)."""
    status = response.status_code
    if status < 300:
        return None
    request_id = response.headers.get("Request-Id")
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    error_type, code = error.get("type"), error.get("code")
    should_retry = response.headers.get("Stripe-Should-Retry")
    retryable = (
        status in (_STATUS_RATE_LIMITED,)
        or status >= _STATUS_SERVER_ERROR
        or (status == _STATUS_CONFLICT and code == "lock_timeout")
    )
    if should_retry is not None:
        retryable = should_retry.lower() == "true"
    if retryable:
        return StripeTransientError(
            f"stripe status={status} code={code} request_id={request_id}",
            status=status,
            request_id=request_id,
        )
    if error_type == "idempotency_error":
        return IdempotencyConflict(f"status={status} request_id={request_id}")
    cls = StripeCardError if status == _STATUS_PAYMENT_REQUIRED else StripeApiError
    return cls(
        status,
        error_type=error_type,
        code=code,
        decline_code=error.get("decline_code"),
        request_id=request_id,
    )


class StripeClient:
    def __init__(  # noqa: PLR0913 - explicit configuration, no hidden globals
        self,
        api_key: SecretStr | str,
        *,
        api_version: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key.startswith(TEST_KEY_PREFIXES):
            raise ValueError("only Stripe test-mode keys (sk_test_ / rk_test_) are accepted")
        if not api_version:
            raise ValueError("a pinned Stripe API version is required")
        self.api_version = api_version
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Authorization": f"Bearer {key}",
                "Stripe-Version": api_version,
                "User-Agent": "praxis-payments/1",
            },
        )

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params or {}, None)

    def post(
        self, path: str, params: Mapping[str, Any] | None = None, *, idempotency_key: str | None
    ) -> dict[str, Any]:
        """``idempotency_key`` is mandatory in spirit: pass ``None`` only for safe actions."""
        return self._request("POST", path, params or {}, idempotency_key)

    def delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path, {}, None)

    def list_all(
        self, path: str, params: Mapping[str, Any] | None = None, *, max_pages: int = 20
    ) -> Iterator[dict[str, Any]]:
        """Auto-paginate a list endpoint (``starting_after``), bounded by ``max_pages``."""
        query = {**(params or {}), "limit": 100}
        for _ in range(max_pages):
            page = self.get(path, query)
            data = page.get("data") or []
            yield from data
            if not page.get("has_more") or not data:
                return
            query["starting_after"] = data[-1]["id"]
        raise GatewayError("pagination_limit", f"{path} has more than {max_pages} pages")

    def _request(
        self, method: str, path: str, params: Mapping[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        form = encode_form(params)
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(
                    method,
                    path,
                    params=tuple(form) if method == "GET" else None,
                    data=dict(form) if method == "POST" else None,
                    headers=headers,
                )
                error = classify(response)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = StripeTransientError(f"stripe transport: {type(exc).__name__}")
            if error is None:
                body: dict[str, Any] = response.json()
                return body
            if not isinstance(error, TransientError) or attempt == self._max_retries:
                raise error
            logger.warning(
                "stripe request retry",
                extra={"method": method, "path": path, "attempt": attempt + 1, "error": str(error)},
            )
            self._sleep(self._backoff_s * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover
