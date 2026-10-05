"""CLI: ``python -m praxis.data {migrate,ingest,replay,load-sim,freshness}``.

Exit codes: 0 success or degraded-but-safe (outage, missing key), 2 contract problem
(quarantine / rejected request), 1 with ``--strict`` on any non-success.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from praxis.config import get_settings
from praxis.data.config import load_sources_config
from praxis.data.fetch import HttpFetcher
from praxis.data.freshness import FreshnessState, check_freshness
from praxis.data.ingest import IngestReport, IngestService, Outcome
from praxis.data.models import SourceId, TimeWindow
from praxis.data.raw_store import LocalRawStore
from praxis.data.sources import build_registry
from praxis.data.warehouse import Warehouse
from praxis.logging import configure_logging

DEFAULT_DB = Path("data/warehouse/praxis.duckdb")
DEFAULT_RAW = Path("data/raw")


def _service(args: argparse.Namespace, wh: Warehouse, client: httpx.Client) -> IngestService:
    settings = get_settings()
    registry = build_registry(
        load_sources_config(),
        fred_api_key=settings.fred_api_key,
        eia_api_key=settings.eia_api_key,
    )
    return IngestService(registry, HttpFetcher(client), LocalRawStore(args.raw), wh)


def _exit_code(report: IngestReport, strict: bool) -> int:
    if strict and not report.all_succeeded:
        return 1
    if report.count(Outcome.QUARANTINED) or report.count(Outcome.REJECTED):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - complexity-debt
    ap = argparse.ArgumentParser(prog="praxis.data")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    ing = sub.add_parser("ingest")
    ing.add_argument("--source", choices=[s.value for s in SourceId] + ["all"], default="all")
    ing.add_argument("--start", type=date.fromisoformat, default=None)
    ing.add_argument("--end", type=date.fromisoformat, default=None)
    ing.add_argument("--strict", action="store_true")
    rep = sub.add_parser("replay", help="rebuild normalised rows from the raw archive (offline)")
    rep.add_argument("--source", choices=[s.value for s in SourceId], default=None)
    sim = sub.add_parser("load-sim")
    sim.add_argument("--dir", type=Path, required=True)
    sub.add_parser("freshness")
    bq = sub.add_parser("bq-load", help="mirror the raw layer into BigQuery (load jobs only)")
    bq.add_argument("--project", required=True)
    bq.add_argument("--prefix", default="praxis_dev_")
    bq.add_argument("--location", default="europe-west2")
    bq.add_argument("--workdir", type=Path, default=Path("data/bq_export"))
    args = ap.parse_args(argv)

    configure_logging("praxis-data", get_settings().log_level.value)
    with Warehouse(args.db) as wh:
        wh.migrate()
        config = load_sources_config()
        wh.load_region_locations(config.locations)
        if args.cmd == "migrate":
            return 0
        if args.cmd == "load-sim":
            manifest = json.loads((args.dir / "manifest.json").read_text())
            new = wh.load_sim_events(args.dir / "events.ndjson", manifest)
            sys.stdout.write(
                json.dumps({"new_events": new, "batch_id": manifest["batch_id"]}) + "\n"
            )
            return 0
        if args.cmd == "bq-load":
            from google.cloud import bigquery

            from praxis.data.bigquery import BigQueryLoader

            loader = BigQueryLoader(
                bigquery.Client(project=args.project, location=args.location),
                project=args.project,
                prefix=args.prefix,
                location=args.location,
            )
            for result in loader.load_raw(wh, args.workdir):
                sys.stdout.write(json.dumps(result.__dict__) + "\n")
            return 0
        if args.cmd == "freshness":
            now = datetime.now(UTC)
            rows = check_freshness(wh, config, now)
            for r in rows:
                latest = r.latest_observation.isoformat() if r.latest_observation else None
                sys.stdout.write(
                    json.dumps({"source": r.source.value, "state": r.state.value, "latest": latest})
                    + "\n"
                )
            return 0 if all(r.state is FreshnessState.FRESH for r in rows) else 1
        with httpx.Client(follow_redirects=False) as client:
            service = _service(args, wh, client)
            if args.cmd == "replay":
                report = service.replay(SourceId(args.source) if args.source else None)
            else:
                end = args.end or datetime.now(UTC).date() - timedelta(days=1)
                window = TimeWindow(start=args.start or end - timedelta(days=6), end=end)
                chosen = list(SourceId) if args.source == "all" else [SourceId(args.source)]
                report = IngestReport()
                for sid in chosen:
                    report.results.extend(service.ingest(sid, window).results)
        sys.stdout.write(json.dumps(report.summary(), sort_keys=True) + "\n")
        return _exit_code(report, getattr(args, "strict", False))


if __name__ == "__main__":
    sys.exit(main())
