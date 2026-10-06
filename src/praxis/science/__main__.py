"""CLI: ``python -m praxis.science elasticity --analysis DIR --sim SIMDIR --scenario TOML``.

Joins the elasticity analysis (``DIR/report.json``, ``DIR/units.json``) with the simulator's
ground truth (``SIMDIR/ground_truth.npz``) and applies the pre-registered acceptance
(``configs/elasticity/acceptance.toml``). Writes ``DIR/recovery.json``; exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from praxis.science.elasticity_recovery import Truth, evaluate, load_acceptance, load_json
from praxis.simulator.config import load_config


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.science")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("elasticity", help="elasticity recovery vs ground truth")
    e.add_argument("--analysis", type=Path, required=True, help="praxis.elasticity --out DIR")
    e.add_argument("--sim", type=Path, required=True, help="simulator output dir")
    e.add_argument("--scenario", type=Path, required=True, help="scenario overlay of the world")
    e.add_argument("--acceptance", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = load_json(args.sim / "manifest.json")
    # The scenario fixes everything but the population size; Truth.load checks the hash.
    world = load_config(scenario=args.scenario).with_overrides(
        n_customers=int(manifest["n_customers"])
    )
    truth = Truth.load(args.sim / "ground_truth.npz", world)
    result = evaluate(
        load_json(args.analysis / "report.json"),
        load_json(args.analysis / "units.json"),
        truth,
        world,
        load_acceptance(args.acceptance),
    )
    (args.analysis / "recovery.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    summary = {
        "mode": result["mode"],
        "passed": result["passed"],
        "checks": {c["name"]: c["passed"] for c in result["checks"]},
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
