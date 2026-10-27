"""Approver configuration and token identification (unit)."""

from __future__ import annotations

import secrets

import pytest
from pydantic import SecretStr

from praxis.config import Settings
from praxis.console.app import build_approvers
from praxis.console.auth import ApproversConfigError, bearer, parse_approvers


def tok() -> str:
    return secrets.token_urlsafe(24)


def test_tokens_identify_their_named_approver() -> None:
    a, b = tok(), tok()
    approvers = parse_approvers(f" alice={a} , bob={b}")
    assert approvers.configured
    assert approvers.identify(a) == "alice" and approvers.identify(b) == "bob"
    assert approvers.identify(a + "x") is None and approvers.identify("") is None


def test_unset_means_read_only() -> None:
    assert not parse_approvers(None).configured
    assert not parse_approvers("  ").configured


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("alice", "expected name=token"),
        ("Alice=" + "x" * 30, "expected name=token"),
        ("alice=short", "shorter than 24"),
        ("system=" + "x" * 30, "reserved"),
        ("alice={t},alice={u}", "duplicate name"),
        ("alice={t},bob={t}", "token reused"),
    ],
)
def test_malformed_config_is_refused_without_echoing_tokens(raw: str, message: str) -> None:
    t, u = tok(), tok()
    with pytest.raises(ApproversConfigError, match=message) as error:
        parse_approvers(raw.format(t=t, u=u))
    assert t not in str(error.value) and u not in str(error.value)


def test_settings_approvers_cannot_reuse_the_agent_identity() -> None:
    ok = build_approvers(Settings(console_approvers=SecretStr(f"alice={tok()}")))
    assert ok.configured
    with pytest.raises(ApproversConfigError, match="AI operator's identity"):
        build_approvers(Settings(console_approvers=SecretStr(f"praxis-agent={tok()}")))


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("", None), ("Bearer abc", "abc"), ("bearer  abc ", "abc"),
     ("Basic abc", None), ("Bearer", None)],
)  # fmt: skip
def test_bearer_parsing(header: str | None, expected: str | None) -> None:
    assert bearer(header) == expected
