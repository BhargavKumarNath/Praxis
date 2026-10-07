"""CLI: ``python -m praxis.science {elasticity|pricing-shadow}``.

* ``elasticity --analysis DIR --sim SIMDIR --scenario TOML``: joins the elasticity analysis
  (``DIR/report.json``, ``DIR/units.json``) with the simulator's ground truth
  (``SIMDIR/ground_truth.npz``) and applies ``configs/elasticity/acceptance.toml``. Writes
  ``DIR/recovery.json``.
* ``pricing-shadow --db WAREHOUSE --sim SIMDIR --scenario TOML ... --out DIR``: runs the pricing
  optimiser in shadow mode on the pricing world and scores every decision against simulator
  truth (``configs/pricing/shadow_acceptance.toml``). Writes ``DIR/shadow.json`` and
  ``DIR/decisions.jsonl``.

Exit 1 on any failed pre-registered check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from praxis.elasticity.config import load_registry
from praxis.pricing.config import load_policy
from praxis.science import pricing_shadow
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
    s = sub.add_parser("pricing-shadow", help="shadow-mode pricing evaluation vs truth")
    s.add_argument("--db", type=Path, required=True, help="warehouse of the pricing world")
    s.add_argument("--sim", type=Path, required=True, help="simulator output dir of that world")
    s.add_argument("--scenario", type=Path, required=True)
    s.add_argument("--forecast-models", type=Path, required=True, help="demand artifact root")
    s.add_argument("--elasticity-model", type=Path, required=True)
    s.add_argument("--elasticity-report", type=Path, required=True)
    s.add_argument("--registry", type=Path, default=None)
    s.add_argument("--policy", type=Path, default=None)
    s.add_argument("--acceptance", type=Path, default=None)
    s.add_argument("--out", type=Path, required=True)
    return p


def pricing_shadow_main(args: argparse.Namespace) -> int:
    manifest = load_json(args.sim / "manifest.json")
    world = load_config(scenario=args.scenario).with_overrides(
        n_customers=int(manifest["n_customers"])
    )
    if world.config_hash != manifest["config_hash"]:
        raise SystemExit("simulator output does not belong to this scenario")
    inputs = pricing_shadow.ShadowInputs(
        world=world,
        seed=int(manifest["seed"]),
        warehouse=args.db,
        forecast_models=args.forecast_models,
        elasticity_model=args.elasticity_model,
        elasticity_report=args.elasticity_report,
        registry=load_registry(args.registry),
        policy=load_policy(args.policy),
    )
    cfg = pricing_shadow.load_shadow_config(args.acceptance)
    try:
        report, decisions = pricing_shadow.evaluate(inputs, cfg)
    except pricing_shadow.ShadowError as exc:
        sys.stderr.write(f"shadow evaluation refused: {exc}\n")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "shadow.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (args.out / "decisions.jsonl").write_text("".join(d.record_json() + "\n" for d in decisions))
    summary = {
        "passed": report["passed"],
        "status_counts": report["status_counts"],
        "checks": {c["name"]: c["passed"] for c in report["checks"]},
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "pricing-shadow":
        return pricing_shadow_main(args)
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
