"""Who may complete a guarded action from the console (ADR 0021).

``PRAXIS_CONSOLE_APPROVERS`` lists named approvers and their bearer tokens:
``"alice=<token>@2026-12-31,bob=<token>@2026-12-15"``. The token decides WHO is approving; the
request body never names the approver, so a person cannot approve as someone else. Tokens
are compared in constant time and never logged. With no approvers configured every admin
route answers 503: the console is then read-only.

Expiry and rotation (ADR 0021 amendment 1): ``@YYYY-MM-DD`` ends a token at 00:00 UTC of that
date; an expired token identifies nobody. Outside the local environment every token must
expire within ``MAX_LIFETIME_DAYS``. A name may hold two tokens at once, so a rotation adds the
new token, moves the clients over, and lets the old one expire (``scripts/console_token.py``).
A deployed console would put the same check behind an identity provider; the boundary stays
in FastAPI either way.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,39}$")
MIN_TOKEN_LENGTH = 24
MAX_LIFETIME_DAYS = 90  # outside local: no token may be valid longer than this
MAX_TOKENS_PER_NAME = 2  # current + next during a rotation
EXPIRY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Generic system actor names; the AI operator's own identity (policy.toml `actor`) is refused
# by `console.app.build_approvers`. A human approver may never reuse them, or the
# "decided by is not requested by" rule could be satisfied by impersonation.
RESERVED = frozenset({"agent", "system", "ops-monitor", "mlops-automation"})


class ApproversConfigError(ValueError):
    """``PRAXIS_CONSOLE_APPROVERS`` is malformed (the message never contains a token)."""


@dataclass(frozen=True)
class Entry:
    name: str
    secret: bytes
    expires: datetime | None  # None = no expiry (accepted in the local environment only)

    def valid_at(self, now: datetime) -> bool:
        return self.expires is None or now < self.expires


@dataclass(frozen=True)
class Approvers:
    entries: tuple[Entry, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.entries)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(e.name for e in self.entries)

    def identify(self, token: str, now: datetime | None = None) -> str | None:
        """The approver this token belongs to, or ``None`` (unknown or expired). Every entry
        is compared, so the time taken does not depend on which (if any) entry matches."""
        at = now or datetime.now(UTC)
        supplied = token.encode()
        found = None
        for entry in self.entries:
            if hmac.compare_digest(supplied, entry.secret) and entry.valid_at(at):
                found = entry.name
        return found

    def expiring(self, now: datetime, within: timedelta) -> list[str]:
        """Names whose LAST valid token ends within ``within`` (rotation due), sorted."""
        latest: dict[str, datetime | None] = {}
        for e in self.entries:
            if not e.valid_at(now):
                continue
            current = latest.get(e.name, now)
            latest[e.name] = (
                None if e.expires is None or current is None else max(current, e.expires)
            )
        return sorted(n for n, end in latest.items() if end is not None and end - now <= within)


def _expiry(text: str, i: int) -> datetime:
    if not EXPIRY.fullmatch(text):
        raise ApproversConfigError(f"approver entry {i}: expiry must be @YYYY-MM-DD")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ApproversConfigError(f"approver entry {i}: invalid expiry date") from exc
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _entry(item: str, i: int) -> Entry:
    name, sep, rest = item.strip().partition("=")
    name = name.strip()
    token, at, expiry = rest.strip().rpartition("@") if "@" in rest else (rest.strip(), "", "")
    if not sep or not NAME.fullmatch(name):
        raise ApproversConfigError(f"approver entry {i}: expected name=token, lower-case name")
    if name in RESERVED:
        raise ApproversConfigError(f"approver entry {i}: {name!r} is a reserved actor name")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ApproversConfigError(
            f"approver entry {i}: token shorter than {MIN_TOKEN_LENGTH} characters"
        )
    return Entry(name, token.encode(), _expiry(expiry, i) if at else None)


def parse_approvers(
    raw: str | None, *, require_expiry: bool = False, now: datetime | None = None
) -> Approvers:
    """``require_expiry`` (every non-local environment): each token needs ``@YYYY-MM-DD`` no
    later than ``MAX_LIFETIME_DAYS`` from ``now``. Errors never contain a token."""
    if raw is None or not raw.strip():
        return Approvers()
    at = now or datetime.now(UTC)
    entries: list[Entry] = []
    for i, item in enumerate(raw.split(","), start=1):
        entry = _entry(item, i)
        if require_expiry and entry.expires is None:
            raise ApproversConfigError(f"approver entry {i}: an expiry (@YYYY-MM-DD) is required")
        if (
            require_expiry
            and entry.expires is not None
            and (entry.expires - at > timedelta(days=MAX_LIFETIME_DAYS))
        ):
            raise ApproversConfigError(
                f"approver entry {i}: expiry is more than {MAX_LIFETIME_DAYS} days away"
            )
        if sum(e.name == entry.name for e in entries) >= MAX_TOKENS_PER_NAME:
            raise ApproversConfigError(
                f"approver entry {i}: {entry.name!r} has more than {MAX_TOKENS_PER_NAME} tokens"
            )
        if any(hmac.compare_digest(e.secret, entry.secret) for e in entries):
            raise ApproversConfigError(f"approver entry {i}: token reused")
        entries.append(entry)
    return Approvers(tuple(entries))


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None
