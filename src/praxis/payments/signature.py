"""Stripe webhook signature verification over the raw request body.

Implements Stripe's documented scheme (docs.stripe.com/webhooks, "Verify webhook signatures
manually", checked 2026-10-07):

* header ``Stripe-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...][,v0=...]``;
* ``signed_payload = f"{t}." + raw_body`` (the exact bytes received, never re-serialised);
* expected = HMAC-SHA256(endpoint secret, signed_payload), hex;
* only ``v1`` counts (ignoring other schemes prevents downgrade attacks); several ``v1``
  values (secret rotation) and several local secrets are all tried, in constant time;
* the timestamp must be within ``tolerance_s`` of now (default 300 s, never 0) in either
  direction, which bounds replay of a captured request.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence

SIGNATURE_HEADER = "Stripe-Signature"
DEFAULT_TOLERANCE_S = 300


class SignatureError(ValueError):
    REASONS = frozenset(
        {
            "missing_header",
            "malformed_header",
            "no_v1_signature",
            "signature_mismatch",
            "timestamp_outside_tolerance",
            "no_secret_configured",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self.REASONS:
            raise ValueError(f"unknown signature failure {reason!r}")
        super().__init__(reason)
        self.reason = reason


def compute_signature(secret: str, timestamp: int, payload: bytes) -> str:
    message = str(timestamp).encode() + b"." + payload
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, payload: bytes, timestamp: int) -> str:
    """A ``Stripe-Signature`` header value (tests, local tooling, replay of real events)."""
    return f"t={timestamp},v1={compute_signature(secret, timestamp, payload)}"


def _parse(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    v1: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep or not value:
            raise SignatureError("malformed_header")
        if key == "t":
            if not value.isdigit() or timestamp is not None:
                raise SignatureError("malformed_header")
            timestamp = int(value)
        elif key == "v1":
            v1.append(value)
    if timestamp is None:
        raise SignatureError("malformed_header")
    if not v1:
        raise SignatureError("no_v1_signature")
    return timestamp, v1


def verify(
    payload: bytes,
    header: str | None,
    secrets: Sequence[str],
    *,
    now: float,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> int:
    """Return the signed timestamp, or raise ``SignatureError``."""
    if tolerance_s <= 0:
        raise ValueError("tolerance must be positive: 0 would disable replay protection")
    if not secrets:
        raise SignatureError("no_secret_configured")
    if not header:
        raise SignatureError("missing_header")
    timestamp, candidates = _parse(header)
    matched = False
    for secret in secrets:
        expected = compute_signature(secret, timestamp, payload).encode()
        for candidate in candidates:
            # Bytes (non-ASCII input cannot raise); accumulate instead of returning early.
            matched |= hmac.compare_digest(expected, candidate.encode("utf-8", "replace"))
    if not matched:
        raise SignatureError("signature_mismatch")
    if abs(now - timestamp) > tolerance_s:
        raise SignatureError("timestamp_outside_tolerance")
    return timestamp
