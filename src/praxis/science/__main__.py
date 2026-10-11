"""CLI: ``python -m praxis.science {elasticity|pricing-shadow|pricing-compare|recovery}``.

* ``elasticity --analysis DIR --sim SIMDIR --scenario TOML``: joins the elasticity analysis
  (``DIR/report.json``, ``DIR/units.json``) with the simulator's ground truth
  (``SIMDIR/ground_truth.npz``) and applies ``configs/elasticity/acceptance.toml``. Writes
  ``DIR/recovery.json``.
* ``pricing-shadow --db WAREHOUSE --sim SIMDIR --scenario TOML ... --out DIR``: runs the pricing
  optimiser in shadow mode on the pricing world and scores every decision against simulator
  truth (``configs/pricing/shadow_acceptance.toml``). Writes ``DIR/shadow.json`` and
  ``DIR/decisions.jsonl``.
* ``pricing-compare --before A --after B --out FILE``: the same world decided by two methods
  (Phase 9 controlled example: the Phase 6 seed-42 decisions vs the Phase 9 method).
* ``recovery --db WAREHOUSE --sim SIMDIR --scenario TOML --models ROOT --out DIR``: evaluates the
  recovery artifact and the dunning policy against the simulator's latent cure times
  (``configs/recovery/acceptance.toml``). Writes ``DIR/evaluation.json``.

Exit 1 on any failed pre-registered check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from praxis.customer.config import load_config as load_customer_config
from praxis.elasticity.config import load_registry
from praxis.pricing.config import load_policy
from praxis.recovery.config import load_policy as load_recovery_policy
from praxis.science import pricing_shadow
from praxis.science import recovery as recovery_eval
from praxis.science.elasticity_recovery import Truth, evaluate, load_acceptance, load_json
from praxis.simulator.config import SimulationConfig, load_config


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
    s.add_argument("--churn-response", type=Path, required=True, help="churn_response.json")
    s.add_argument("--customer-models", type=Path, required=True, help="customer artifact root")
    s.add_argument("--customer-config", type=Path, default=None)
    s.add_argument("--registry", type=Path, default=None)
    s.add_argument("--policy", type=Path, default=None)
    s.add_argument("--acceptance", type=Path, default=None)
    s.add_argument(
        "--churn-archive",
        type=Path,
        default=None,
        help="development leave-one-out MAP archive (never for a held-out evaluation)",
    )
    s.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("pricing-compare", help="two shadow reports of one world, side by side")
    c.add_argument("--before", type=Path, required=True, help="earlier method's shadow.json")
    c.add_argument("--after", type=Path, required=True, help="later method's shadow.json")
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("recovery", help="recovery models + dunning policy vs truth")
    r.add_argument("--db", type=Path, required=True, help="warehouse of the recovery world")
    r.add_argument("--sim", type=Path, required=True, help="simulator output dir of that world")
    r.add_argument("--scenario", type=Path, required=True)
    r.add_argument("--models", type=Path, required=True, help="recovery artifact root")
    r.add_argument("--policy", type=Path, default=None)
    r.add_argument("--acceptance", type=Path, default=None)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument(
        "--known-failures",
        type=Path,
        default=None,
        help="JSON {seed, checks, reason}: documented failures of THIS world (dev only); they "
        "stay failed in the report but do not set the exit code",
    )
    return p


def _world(sim: Path, scenario: Path) -> tuple[SimulationConfig, int]:
    manifest = load_json(sim / "manifest.json")
    world = load_config(scenario=scenario).with_overrides(n_customers=int(manifest["n_customers"]))
    if world.config_hash != manifest["config_hash"]:
        raise SystemExit("simulator output does not belong to this scenario")
    return world, int(manifest["seed"])


def recovery_main(args: argparse.Namespace) -> int:
    world, seed = _world(args.sim, args.scenario)
    inputs = recovery_eval.Inputs(
        world=world,
        seed=seed,
        db=args.db,
        models=args.models,
        policy=load_recovery_policy(args.policy),
        acceptance=recovery_eval.load_acceptance(args.acceptance),
    )
    try:
        report = recovery_eval.evaluate(inputs)
    except recovery_eval.EvaluationError as exc:
        sys.stderr.write(f"recovery evaluation refused: {exc}\n")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    failed = {c["name"] for c in report["checks"] if not c["passed"]}
    known = _known_failures(args.known_failures, seed)
    summary = {
        "passed": report["passed"],
        "champion": report["champion"],
        "checks": {c["name"]: c["passed"] for c in report["checks"]},
        "known_failures": sorted(failed & known),
        "new_failures": sorted(failed - known),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if not failed - known else 1


def _known_failures(path: Path | None, seed: int) -> set[str]:
    if path is None:
        return set()
    spec = load_json(path)
    if int(spec["seed"]) != seed or seed == 42:  # never on the held-out world
        raise SystemExit("known failures apply only to the development world they name")
    return set(spec["checks"])


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
        churn_response=args.churn_response,
        customer_models=args.customer_models,
        customer_config=load_customer_config(args.customer_config),
        churn_archive=args.churn_archive,
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


def pricing_compare_main(args: argparse.Namespace) -> int:
    before, after = load_json(args.before), load_json(args.after)
    if before.get("world_config_hash") != after.get("world_config_hash") or before.get(
        "seed"
    ) != after.get("seed"):
        sys.stderr.write("refused: the two reports are not of the same world\n")
        return 1
    out = pricing_shadow.compare_reports(before, after)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    keys = ("before_changes", "before_losing_changes", "losing_altered", "after_true_delta_per_day")
    sys.stdout.write(json.dumps({k: out[k] for k in keys}) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "pricing-shadow":
        return pricing_shadow_main(args)
    if args.command == "pricing-compare":
        return pricing_compare_main(args)
    if args.command == "recovery":
        return recovery_main(args)
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
