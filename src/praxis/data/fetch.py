"""HTTP fetching with bounded retries. No import-time I/O; the client and sleeper are injected.

Retry policy is explicit and finite: connection errors, timeouts, HTTP 429 and 5xx are
retried up to ``max_attempts`` with capped exponential backoff (honouring a capped
``Retry-After``). Any other 4xx is a request error and is never retried.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from praxis.data.errors import SourceRejectedError, SourceUnavailableError

logger = logging.getLogger(__name__)

SECRET_PARAMS = frozenset({"api_key", "apikey", "key", "token"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Params = Mapping[str, str | list[str]]

_SECRET_QUERY = re.compile(r"(?i)\b(api_?key|token|key)=[^&\s\"']+")


class _RedactSecretsFilter(logging.Filter):
    """httpx logs full request URLs at INFO, which would include API keys. Scrub them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed = _SECRET_QUERY.sub(r"\1=[REDACTED]", message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


def _install_url_redaction() -> None:
    target = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactSecretsFilter) for f in target.filters):
        target.addFilter(_RedactSecretsFilter())


def redact_params(params: Params) -> dict[str, str | list[str]]:
    return {k: ("[REDACTED]" if k.lower() in SECRET_PARAMS else v) for k, v in params.items()}


def build_endpoint(url: str, params: Params) -> str:
    """Credential-free URL (query string included) safe to store and log."""
    safe = redact_params(params)
    pairs = [(k, x) for k, v in sorted(safe.items()) for x in (v if isinstance(v, list) else [v])]
    return f"{url}?{urlencode(pairs, safe='[]')}" if pairs else url


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    timeout_s: float = 10.0
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 8.0
    retry_after_cap_s: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self.retry_after_cap_s)
            except ValueError:
                pass  # HTTP-date form: fall back to backoff
        return float(min(self.backoff_base_s * 2 ** (attempt - 1), self.backoff_cap_s))


@dataclass(frozen=True)
class FetchedBody:
    body: bytes
    status: int
    endpoint: str  # redacted


class HttpFetcher:
    def __init__(
        self,
        client: httpx.Client,
        policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _install_url_redaction()
        self._client = client
        self._policy = policy or RetryPolicy()
        self._sleep = sleeper

    def get(self, url: str, params: Params) -> FetchedBody:
        endpoint = build_endpoint(url, params)
        last = "no attempt made"
        for attempt in range(1, self._policy.max_attempts + 1):
            retry_after: str | None = None
            try:
                response = self._client.get(
                    url, params=dict(params), timeout=self._policy.timeout_s
                )
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}"
            else:
                status = response.status_code
                if status == 200:
                    return FetchedBody(body=response.content, status=status, endpoint=endpoint)
                if status not in _RETRYABLE_STATUS:
                    raise SourceRejectedError(f"HTTP {status} from {endpoint}")
                retry_after = response.headers.get("retry-after")
                last = f"HTTP {status}"
            logger.warning(
                "source request failed",
                extra={"endpoint": endpoint, "attempt": attempt, "reason": last},
            )
            if attempt < self._policy.max_attempts:
                self._sleep(self._policy.delay(attempt, retry_after))
        raise SourceUnavailableError(
            f"{endpoint} unavailable after {self._policy.max_attempts} attempts ({last})"
        )
