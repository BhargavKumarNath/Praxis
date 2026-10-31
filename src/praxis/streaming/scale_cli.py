"""``scale-archive`` / ``scale-run`` subcommands of ``python -m praxis.streaming`` (ADR 0022).

Workloads and SLOs come only from the pre-registered config (``configs/scale/slo.toml``),
never from ad-hoc flags, so a run cannot be re-parameterised after seeing a result.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from praxis.streaming.scale import (
    build_archive,
    evaluate_http,
    evaluate_slo,
    load_scale_config,
    run_scale,
)

DEFAULT_CONFIG = Path("configs/scale/slo.toml")


def _write(report: dict[str, Any], out: Path | None) -> None:
    body = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
    sys.stdout.write(body)


def cmd_scale_archive(args: argparse.Namespace) -> int:
    run, _ = load_scale_config(args.config, args.scale)
    _write(build_archive(run, args.out), args.report)
    return 0


def cmd_scale_run(args: argparse.Namespace) -> int:
    run, slo = load_scale_config(args.config, args.scale)
    db_url = args.database_url
    if db_url is None:
        raise SystemExit("scale-run needs --database-url (a dedicated, freshly migrated DB)")
    report = run_scale(run, args.archive, db_url, args.warehouse_dir)
    report["scale"] = args.scale
    report["slo"] = evaluate_slo(report, slo) if slo else None
    _write(report, args.report)
    ok = report["matches_oracle"] and (report["slo"] is None or report["slo"]["passed"])
    return 0 if ok else 1


def cmd_scale_http_check(args: argparse.Namespace) -> int:
    """W3 verdict: the k6 summary against the pre-registered ``[http.<scale>]`` SLO."""
    slo = tomllib.loads(args.config.read_text())["http"][args.scale]
    summary = json.loads(args.summary.read_text())
    result = {"scale": args.scale, "slo": slo, **evaluate_http(summary, slo)}
    _write(result, args.summary.with_name("http-slo.json"))
    return 0 if result["passed"] else 1


def add_scale_commands(sub: Any) -> None:
    archive = sub.add_parser("scale-archive", help="W1: simulate -> sharded archive + oracle")
    archive.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    archive.add_argument("--scale", required=True, help="ladder step in the config, e.g. 100k")
    archive.add_argument("--out", type=Path, required=True)
    archive.add_argument("--report", type=Path, default=None)
    archive.set_defaults(fn=cmd_scale_archive)

    run = sub.add_parser("scale-run", help="W2: sharded replay through the consumers + SLOs")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    run.add_argument("--scale", required=True)
    run.add_argument("--archive", type=Path, required=True)
    run.add_argument("--warehouse-dir", type=Path, required=True)
    run.add_argument("--report", type=Path, default=None)
    run.set_defaults(fn=cmd_scale_run)

    http = sub.add_parser("scale-http-check", help="W3: k6 summary vs the HTTP SLO")
    http.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    http.add_argument("--scale", required=True)
    http.add_argument("--summary", type=Path, required=True)
    http.set_defaults(fn=cmd_scale_http_check)
