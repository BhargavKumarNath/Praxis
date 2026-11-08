"""Approver configuration and token identification (unit)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from praxis.config import Environment, Settings
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
        ("alice={t},alice={u},alice={t}x", "more than 2 tokens"),
        ("alice={t},bob={t}", "token reused"),
        ("alice={t}@2026-13-01", "invalid expiry"),
        ("alice={t}@tomorrow", "expiry must be"),
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


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def test_an_expired_token_identifies_nobody() -> None:
    t = tok()
    approvers = parse_approvers(f"alice={t}@2026-11-06")
    assert approvers.identify(t, NOW) == "alice"
    assert approvers.identify(t, datetime(2026, 11, 6, tzinfo=UTC)) is None  # 00:00 UTC ends it


def test_rotation_overlap_keeps_both_tokens_until_the_old_one_expires() -> None:
    old, new = tok(), tok()
    approvers = parse_approvers(f"alice={old}@2026-11-10,alice={new}@2026-12-05")
    assert approvers.identify(old, NOW) == approvers.identify(new, NOW) == "alice"
    later = datetime(2026, 11, 11, tzinfo=UTC)
    assert approvers.identify(old, later) is None and approvers.identify(new, later) == "alice"
    assert approvers.expiring(NOW, timedelta(days=14)) == []  # the newest token is not due
    assert approvers.expiring(later, timedelta(days=30)) == ["alice"]


def test_non_local_environments_require_bounded_expiry() -> None:
    t = tok()
    with pytest.raises(ApproversConfigError, match=r"expiry .* is required") as err:
        parse_approvers(f"alice={t}", require_expiry=True, now=NOW)
    assert t not in str(err.value)
    with pytest.raises(ApproversConfigError, match="more than 90 days"):
        parse_approvers(f"alice={t}@2027-06-01", require_expiry=True, now=NOW)
    assert parse_approvers(f"alice={t}@2026-12-01", require_expiry=True, now=NOW).configured
    assert parse_approvers(f"alice={t}").configured  # local: allowed without expiry
    dev = Settings(
        environment=Environment.DEV, gcp_project_id="p", console_approvers=SecretStr(f"alice={t}")
    )
    with pytest.raises(ApproversConfigError, match="required"):
        build_approvers(dev, NOW)
    ok = Settings(environment=Environment.DEV, gcp_project_id="p",
                  console_approvers=SecretStr(f"alice={t}@2026-11-20"))  # fmt: skip
    assert build_approvers(ok, NOW).names == frozenset({"alice"})


def test_rotation_script_adds_expires_and_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.console_token import rotate

    old, other, dead = tok(), tok(), tok()
    raw = f"owner={old},bob={other}@2026-11-30,carol={dead}@2026-11-01"
    out = rotate(raw, "owner", 30, revoke_others=False, legacy_days=14, now=NOW, token="n" * 48)
    approvers = parse_approvers(out, require_expiry=True, now=NOW)
    assert approvers.identify(old, NOW) == "owner"  # legacy token kept, now with an expiry
    assert approvers.identify("n" * 48, NOW) == "owner"
    assert approvers.identify(other, NOW) == "bob" and "carol" not in approvers.names
    assert approvers.identify(old, datetime(2026, 11, 19, tzinfo=UTC)) is None  # 14 days
    revoked = rotate(out, "owner", 7, revoke_others=True, legacy_days=14, now=NOW, token="m" * 48)
    after = parse_approvers(revoked, require_expiry=True, now=NOW)
    assert after.identify("n" * 48, NOW) is None and after.identify("m" * 48, NOW) == "owner"
    with pytest.raises(SystemExit):
        rotate(raw, "owner", 120, revoke_others=False, legacy_days=14, now=NOW)
