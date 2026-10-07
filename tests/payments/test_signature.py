"""Webhook signature verification (required_test.md s13 "Webhook": raw body, tampering)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.payments.signature import SignatureError, compute_signature, sign, verify
from tests.payments.helpers import body_of, stripe_event, stripe_invoice, webhook_secret

SECRET = webhook_secret()
NOW = 1_790_000_000
BODY = body_of(stripe_event("invoice.paid", stripe_invoice()))


def _reason(
    body: bytes, header: str | None, secrets: list[str] | None = None, now: int = NOW
) -> str:
    with pytest.raises(SignatureError) as info:
        verify(body, header, secrets or [SECRET], now=now)
    return info.value.reason


def test_signature_matches_stripes_documented_construction() -> None:
    # Independent re-implementation of docs.stripe.com/webhooks "verify manually", step 2-3.
    expected = hmac.new(SECRET.encode(), f"{NOW}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, NOW, BODY) == expected
    assert sign(SECRET, BODY, NOW) == f"t={NOW},v1={expected}"
    assert verify(BODY, sign(SECRET, BODY, NOW), [SECRET], now=NOW) == NOW


def test_verification_uses_the_raw_body_not_a_reserialisation() -> None:
    header = sign(SECRET, BODY, NOW)
    reserialised = json.dumps(json.loads(BODY), separators=(",", ":")).encode()
    assert json.loads(reserialised) == json.loads(BODY)  # same JSON, different bytes
    assert _reason(reserialised, header) == "signature_mismatch"


def test_tampered_body_is_rejected() -> None:
    header = sign(SECRET, BODY, NOW)
    tampered = BODY.replace(b'"amount_due": 4900', b'"amount_due": 1')
    assert tampered != BODY
    assert _reason(tampered, header) == "signature_mismatch"


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=0, max_value=len(BODY) - 1), st.integers(min_value=1, max_value=255))
def test_any_single_byte_change_is_rejected(position: int, delta: int) -> None:
    header = sign(SECRET, BODY, NOW)
    mutated = bytearray(BODY)
    mutated[position] = (mutated[position] + delta) % 256
    assert _reason(bytes(mutated), header) == "signature_mismatch"


def test_wrong_secret_is_rejected() -> None:
    header = sign(webhook_secret("other"), BODY, NOW)
    assert _reason(BODY, header) == "signature_mismatch"


def test_rotation_multiple_signatures_and_secrets() -> None:
    old, new = webhook_secret("old"), webhook_secret("new")
    both = f"t={NOW},v1={compute_signature(old, NOW, BODY)},v1={compute_signature(new, NOW, BODY)}"
    assert verify(BODY, both, [new], now=NOW) == NOW
    assert verify(BODY, sign(old, BODY, NOW), [new, old], now=NOW) == NOW


def test_v0_only_header_is_a_downgrade_and_rejected() -> None:
    v0 = f"t={NOW},v0={compute_signature(SECRET, NOW, BODY)}"
    assert _reason(BODY, v0) == "no_v1_signature"


def test_signature_on_another_timestamp_is_rejected() -> None:
    sig = compute_signature(SECRET, NOW, BODY)
    assert _reason(BODY, f"t={NOW + 1},v1={sig}", now=NOW + 1) == "signature_mismatch"


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing_header"),
        ("", "missing_header"),
        ("garbage", "malformed_header"),
        ("v1=abc", "malformed_header"),
        ("t=abc,v1=abc", "malformed_header"),
        ("t=-5,v1=abc", "malformed_header"),
        ("t=1,t=2,v1=abc", "malformed_header"),
        ("t=1,v1=", "malformed_header"),
        (f"t={NOW}", "no_v1_signature"),
        (f"t={NOW},v1=é", "signature_mismatch"),  # non-ASCII must not crash the comparison
    ],
)
def test_malformed_headers(header: str | None, reason: str) -> None:
    assert _reason(BODY, header) == reason


@pytest.mark.parametrize("offset", [-301, 301, -10_000])
def test_timestamp_outside_tolerance_is_rejected(offset: int) -> None:
    signed_at = NOW + offset
    assert _reason(BODY, sign(SECRET, BODY, signed_at)) == "timestamp_outside_tolerance"


@pytest.mark.parametrize("offset", [-300, 0, 300])
def test_timestamp_within_tolerance_is_accepted(offset: int) -> None:
    assert verify(BODY, sign(SECRET, BODY, NOW + offset), [SECRET], now=NOW) == NOW + offset


def test_zero_tolerance_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="tolerance"):
        verify(BODY, sign(SECRET, BODY, NOW), [SECRET], now=NOW, tolerance_s=0)


def test_empty_secret_list_reports_no_secret() -> None:
    with pytest.raises(SignatureError) as info:
        verify(BODY, sign(SECRET, BODY, NOW), [], now=NOW)
    assert info.value.reason == "no_secret_configured"


def test_unknown_reason_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="unknown"):
        SignatureError("made_up")
