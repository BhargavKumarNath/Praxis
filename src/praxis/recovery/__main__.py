"""CLI: ``python -m praxis.recovery --db WAREHOUSE train ...``.

``train --selection-cutoff DATE --train-cutoff DATE --gap-choices 1,2,... --out ROOT
--report FILE`` runs the pre-registered training protocol on the warehouse (never the
simulator), saves the checksummed artifact under ROOT and writes the training report. Exit 1
when the survival fit does not converge or there are too few rows (no artifact then).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path

from praxis.config import get_settings
from praxis.logging import configure_logging
from praxis.provenance import code_revision
from praxis.recovery.artifact import save_artifact
from praxis.recovery.classifier import TrainingError
from praxis.recovery.config import load_model_config, load_policy
from praxis.recovery.survival import FitError
from praxis.recovery.train import train
from praxis.recovery.warehouse import WarehouseUnavailable


def _midnight(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time(0), tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.recovery")
    p.add_argument("--db", type=Path, required=True, help="DuckDB warehouse with dbt marts")
    p.add_argument("--policy", type=Path, default=None)
    p.add_argument("--model-config", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("train", help="selection + final fit + artifact")
    t.add_argument("--selection-cutoff", type=_midnight, required=True, help="YYYY-MM-DD (UTC)")
    t.add_argument("--train-cutoff", type=_midnight, required=True, help="YYYY-MM-DD (UTC)")
    t.add_argument(
        "--gap-choices",
        type=lambda v: tuple(int(x) for x in v.split(",")),
        required=True,
        help="retry-timing design of the logging policy, e.g. 1,2,3,4,5,7,10",
    )
    t.add_argument("--out", type=Path, required=True, help="artifact root")
    t.add_argument("--report", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("praxis-recovery", get_settings().log_level)
    log = logging.getLogger("praxis.recovery")
    try:
        manifest, files, report = train(
            args.db,
            selection_cutoff=args.selection_cutoff,
            train_cutoff=args.train_cutoff,
            cfg=load_model_config(args.model_config),
            policy=load_policy(args.policy),
            gap_choices=args.gap_choices,
            code_revision=code_revision(),
        )
    except (FitError, TrainingError, WarehouseUnavailable) as exc:
        log.error("recovery.train_failed", extra={"error": str(exc)})
        sys.stderr.write(f"training failed: {exc}\n")
        return 1
    path = save_artifact(manifest, files, args.out)
    report["artifact"] = str(path)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    summary = {
        "artifact": str(path),
        "champion": report["champion"],
        "selection": report["selection"],
        "survival_fit_cells_failed": report["survival_fit_cells_failed"],
    }
    log.info("recovery.trained", extra=summary)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
