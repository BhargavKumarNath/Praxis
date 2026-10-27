"""Seed a control-plane database for the console E2E test and the local demo.

Migrates the database, then drives the key journey through the real code paths
(``tests/console/journey.py``): a pricing snapshot over the volatility limit, the ops monitor
opening the incident, and the deterministic triage requesting a freeze. Prints the journey ids
as JSON. The alert is STAGED: the snapshot is published by this seed, not by a pricing run.

    python -m tests.e2e.seed_journey --database-url postgresql+psycopg://...
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from sqlalchemy import create_engine

from praxis.control.db import migrate
from tests.console.journey import run_journey


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--no-migrate", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_migrate:
        migrate(args.database_url)
    journey = run_journey(create_engine(args.database_url))
    sys.stdout.write(json.dumps({"staged": True, **asdict(journey)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
