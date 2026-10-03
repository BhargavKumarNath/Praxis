"""Trace / correlation ID conventions.

* ``trace_id``: 32 lowercase hex chars, non-zero. Compatible with W3C ``traceparent`` and
  OpenTelemetry, so Phase 11 can adopt OTel without changing event contracts.
* ``correlation_id``: UUID string identifying one business flow; it survives across
  services and is carried in events and the ``X-Correlation-ID`` HTTP header.

Both live in ``contextvars`` so structured logs pick them up without parameter passing.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"
TRACE_ID_PATTERN = r"^(?!0{32}$)[0-9a-f]{32}$"
_TRACE_ID_RE = re.compile(TRACE_ID_PATTERN)

_trace_id: ContextVar[str | None] = ContextVar("praxis_trace_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("praxis_correlation_id", default=None)


def new_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if _TRACE_ID_RE.match(value):
            return value


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def is_valid_trace_id(value: str) -> bool:
    return bool(_TRACE_ID_RE.match(value))


def is_valid_correlation_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def trace_context(
    trace_id: str | None = None, correlation_id: str | None = None
) -> Iterator[tuple[str, str]]:
    """Bind trace and correlation IDs for the duration of the block.

    Invalid inbound IDs are rejected rather than silently replaced, so a malformed
    upstream value is visible instead of breaking the trace chain unnoticed.
    """
    if trace_id is not None and not is_valid_trace_id(trace_id):
        raise ValueError("invalid trace_id")
    if correlation_id is not None and not is_valid_correlation_id(correlation_id):
        raise ValueError("invalid correlation_id")
    tid = trace_id or new_trace_id()
    cid = correlation_id or new_correlation_id()
    t_token = _trace_id.set(tid)
    c_token = _correlation_id.set(cid)
    try:
        yield tid, cid
    finally:
        _trace_id.reset(t_token)
        _correlation_id.reset(c_token)
