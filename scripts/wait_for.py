"""Wait (bounded) until a local dependency is ready: Postgres accepts a query, or an HTTP
endpoint answers. Used by the Makefile and CI instead of fixed sleeps.

    python scripts/wait_for.py --postgres postgresql+psycopg://praxis@127.0.0.1:55432/postgres
    python scripts/wait_for.py --http http://127.0.0.1:8085 --timeout 120
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from collections.abc import Callable

from sqlalchemy import create_engine, text


def _postgres(url: str) -> Callable[[], bool]:
    def probe() -> bool:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        try:
            with engine.connect() as conn:
                return bool(conn.execute(text("SELECT 1")).scalar_one() == 1)
        except Exception:
            return False
        finally:
            engine.dispose()

    return probe


def _http(url: str) -> Callable[[], bool]:
    if not url.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("only local endpoints may be probed")

    def probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - local only
                return bool(200 <= resp.status < 300)
        except OSError:
            return False

    return probe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--postgres")
    group.add_argument("--http")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)
    probe = _postgres(args.postgres) if args.postgres else _http(args.http)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if probe():
            return 0
        time.sleep(0.5)  # readiness polling with a hard deadline, not synchronisation
    sys.stderr.write(f"not ready after {args.timeout:.0f}s\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
