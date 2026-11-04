"""CLI: ``python -m praxis.live {bootstrap,tick,status}`` (ADR 0027). SYNTHETIC world.

* ``bootstrap --approver NAME``: train, validate and promote the first champion (a named
  human approves; refused once a state exists).
* ``tick [--as-of DATE]``: one live tick (default: today, UTC). Exit 0 = operated or up to
  date, 1 = a step failed (state not advanced), 2 = refused.
* ``status``: the last operated day and the newest tick report.
* ``republish``: rebuild and publish the warehouse the last tick saw (a repair; operates
  nothing, the state is unchanged).

``--root`` (default ``PRAXIS_LIVE_ROOT``) is the persistent, shared directory (the GCS
volume in the cloud); ``--work`` is scratch space for this execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from praxis.config import get_settings
from praxis.live.config import load_live_config
from praxis.live.loop import LiveError, LiveLoop, Paths, StepFailed, read_state
from praxis.logging import configure_logging


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.live")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--root", type=Path, default=None, help="persistent shared directory")
    p.add_argument("--work", type=Path, default=Path("/tmp/praxis-live"))  # noqa: S108
    sub = p.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--approver", required=True, help="the named human promoting it")
    tick = sub.add_parser("tick")
    tick.add_argument("--as-of", type=date.fromisoformat, default=None)
    sub.add_parser("status")
    sub.add_parser("republish")
    return p


def _write(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("praxis-live", settings.log_level.value)
    root = args.root or settings.live_root
    if root is None:
        parser.error("--root or PRAXIS_LIVE_ROOT is required")
    paths = Paths(root=root, work=args.work)
    if args.command == "status":
        last = read_state(paths)
        # tick reports are named by ISO date; bootstrap / repair reports are not ticks
        reports = (
            sorted(p for p in paths.reports.glob("????-??-??.json"))
            if paths.reports.exists()
            else []
        )
        newest = json.loads(reports[-1].read_text()) if reports else None
        _write({"last_as_of": last, "newest_report": newest})
        return 0
    loop = LiveLoop(load_live_config(args.config), paths)
    try:
        if args.command == "bootstrap":
            out = loop.bootstrap(args.approver)
        elif args.command == "republish":
            out = loop.republish()
        else:
            out = loop.tick(args.as_of)
    except LiveError as refused:
        _write({"refused": str(refused)})
        return 2
    except StepFailed as failed:
        _write({"failed": failed.result.as_dict(),
                "steps": [r.as_dict() for r in loop.results]})  # fmt: skip
        return 1
    _write(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
