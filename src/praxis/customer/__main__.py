"""CLI: ``python -m praxis.customer --db WAREHOUSE {train|monitor}``.

* ``train --start DATE --cutoff DATE --out ROOT [--report FILE]``: snapshot panel over
  [start, cutoff) -> rolling-origin backtest -> pre-registered champion rule -> artifact
  (``ROOT/<model_version>``). Reads the marts only, never the simulator.
* ``monitor --model DIR --as-of DATE [--out FILE]``: segment monitoring report (retention,
  churn vs expectation, CLV and its sensitivity, price-change exposure, alerts). Exit 1 when
  an alert fires.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from praxis.config import get_settings
from praxis.customer.artifact import load_artifact, save_artifact
from praxis.customer.config import load_config
from praxis.customer.dataset import build_panel, snapshot_dates
from praxis.customer.monitor import segment_report
from praxis.customer.train import train
from praxis.customer.warehouse import connect
from praxis.logging import configure_logging


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def cmd_train(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    con = connect(args.db)
    try:
        panel = build_panel(con, snapshot_dates(args.start, args.cutoff, cfg.label), cfg)
    finally:
        con.close()
    manifest, files, report = train(panel, cfg, cutoff=args.cutoff)
    path = save_artifact(manifest, files, args.out)
    report["artifact"] = str(path)
    _write(args.report, report)
    sys.stdout.write(json.dumps({"artifact": str(path), "champion": manifest["champion"]}) + "\n")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    con = connect(args.db)
    try:
        report = segment_report(con, load_artifact(args.model), args.as_of, cfg)
    finally:
        con.close()
    _write(args.out, report)
    return 1 if report["alerts"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.customer")
    p.add_argument("--db", type=Path, required=True, help="DuckDB warehouse with dbt marts")
    p.add_argument("--config", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("train", help="backtest + champion rule + artifact")
    t.add_argument("--start", type=date.fromisoformat, required=True, help="first day of data")
    t.add_argument("--cutoff", type=date.fromisoformat, required=True, help="first day excluded")
    t.add_argument("--out", type=Path, required=True, help="artifact root")
    t.add_argument("--report", type=Path, default=None)
    t.set_defaults(func=cmd_train)
    m = sub.add_parser("monitor", help="segment monitoring report")
    m.add_argument("--model", type=Path, required=True, help="customer artifact dir")
    m.add_argument("--as-of", type=date.fromisoformat, required=True)
    m.add_argument("--out", type=Path, default=None)
    m.set_defaults(func=cmd_monitor)
    return p


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging("praxis-customer", settings.log_level.value)
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
