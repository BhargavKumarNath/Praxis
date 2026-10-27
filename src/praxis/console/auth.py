"""Who may complete a guarded action from the console (ADR 0021).

``PRAXIS_CONSOLE_APPROVERS`` lists named approvers and their bearer tokens:
``"alice=<token>,bob=<token>"``. The token decides WHO is approving; the request body never
names the approver, so a person cannot approve as someone else. Tokens are compared in
constant time and never logged. With no approvers configured every admin route answers 503:
the console is then read-only.

This is a local, single-operator mechanism (no SSO, no rotation). A deployed console would
put the same check behind an identity provider; the boundary stays in FastAPI either way.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,39}$")
MIN_TOKEN_LENGTH = 24
# Generic system actor names; the AI operator's own identity (policy.toml `actor`) is refused
# by `console.app.build_approvers`. A human approver may never reuse them, or the
# "decided by is not requested by" rule could be satisfied by impersonation.
RESERVED = frozenset({"agent", "system", "ops-monitor", "mlops-automation"})


class ApproversConfigError(ValueError):
    """``PRAXIS_CONSOLE_APPROVERS`` is malformed (the message never contains a token)."""


@dataclass(frozen=True)
class Approvers:
    tokens: tuple[tuple[str, bytes], ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.tokens)

    def identify(self, token: str) -> str | None:
        """The approver this token belongs to, or ``None``. Every entry is compared, so the
        time taken does not depend on which (if any) entry matches."""
        supplied = token.encode()
        found = None
        for name, secret in self.tokens:
            if hmac.compare_digest(supplied, secret):
                found = name
        return found


def parse_approvers(raw: str | None) -> Approvers:
    if raw is None or not raw.strip():
        return Approvers()
    entries: list[tuple[str, bytes]] = []
    for i, item in enumerate(raw.split(","), start=1):
        name, sep, token = item.strip().partition("=")
        name, token = name.strip(), token.strip()
        if not sep or not NAME.fullmatch(name):
            raise ApproversConfigError(f"approver entry {i}: expected name=token, lower-case name")
        if name in RESERVED:
            raise ApproversConfigError(f"approver entry {i}: {name!r} is a reserved actor name")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ApproversConfigError(
                f"approver entry {i}: token shorter than {MIN_TOKEN_LENGTH} characters"
            )
        if any(n == name for n, _ in entries):
            raise ApproversConfigError(f"approver entry {i}: duplicate name {name!r}")
        if any(hmac.compare_digest(t, token.encode()) for _, t in entries):
            raise ApproversConfigError(f"approver entry {i}: token reused by another approver")
        entries.append((name, token.encode()))
    return Approvers(tuple(entries))


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None
