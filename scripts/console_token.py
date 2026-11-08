"""Rotate a console approver token (ADR 0021 amendment 1). Secrets flow stdin -> stdout only.

    gcloud secrets versions access latest --secret=praxis-dev-console-approvers --project=$P \\
      | .venv/bin/python scripts/console_token.py rotate --name owner --days 30 \\
      | gcloud secrets versions add praxis-dev-console-approvers --data-file=- --project=$P

Reads the current ``PRAXIS_CONSOLE_APPROVERS`` value, drops expired tokens, adds a new random
token for ``--name`` that expires in ``--days`` (<= 90), and writes the new value. The name's
older token keeps working until its own expiry (the overlap that lets clients move over),
unless ``--revoke-others``. A token without an expiry is given one on the way through
(``--legacy-days``), so the output always satisfies the non-local rule. Nothing is logged.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import UTC, datetime, timedelta

from praxis.console.auth import MAX_LIFETIME_DAYS, NAME, Entry, _entry


def _fmt(e: Entry) -> str:
    assert e.expires is not None  # noqa: S101 - every output entry expires
    return f"{e.name}={e.secret.decode()}@{e.expires.date().isoformat()}"


def rotate(raw: str, name: str, days: int, *, revoke_others: bool, legacy_days: int,
           now: datetime, token: str | None = None) -> str:  # fmt: skip
    if not NAME.fullmatch(name):
        raise SystemExit("invalid approver name")
    if not 1 <= days <= MAX_LIFETIME_DAYS or not 1 <= legacy_days <= MAX_LIFETIME_DAYS:
        raise SystemExit(f"days must be within 1..{MAX_LIFETIME_DAYS}")
    today = datetime(now.year, now.month, now.day, tzinfo=UTC)
    kept: list[Entry] = []
    items = [i for i in raw.strip().split(",") if i.strip()]
    for i, item in enumerate(items, start=1):
        e = _entry(item, i)
        if e.expires is None:
            e = Entry(e.name, e.secret, today + timedelta(days=legacy_days))
        if not e.valid_at(now) or (revoke_others and e.name == name):
            continue
        kept.append(e)
    # keep at most the newest other token of this name (the rotation overlap)
    mine = sorted((e for e in kept if e.name == name), key=lambda e: e.expires or today)
    kept = [e for e in kept if e.name != name] + mine[-1:]
    new = Entry(name, (token or secrets.token_hex(24)).encode(), today + timedelta(days=days))
    return ",".join(_fmt(e) for e in [*kept, new])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rotate")
    r.add_argument("--name", required=True)
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--legacy-days", type=int, default=14)
    r.add_argument("--revoke-others", action="store_true")
    args = parser.parse_args(argv)
    value = rotate(sys.stdin.read(), args.name, args.days, revoke_others=args.revoke_others,
                   legacy_days=args.legacy_days, now=datetime.now(UTC))  # fmt: skip
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
