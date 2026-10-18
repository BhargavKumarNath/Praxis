"""CLI: ``python -m praxis.mlops --database-url URL --warehouse DB <command>``.

Lifecycle of the demand forecaster (ADR 0017). Exit codes: 0 = done, 1 = the gate refused
(candidate rejected, promotion refused), 2 = invalid request.

* ``migrate``: apply the control-plane migrations (incl. 0005 model registry).
* ``register``: register an existing artifact as a CANDIDATE (``--backtest`` report).
* ``validate``: offline gate -> CHALLENGER or REJECTED (automatic).
* ``run-day`` / ``operate``: probation, shadow evaluation, drift monitoring, retraining for
  one data day / every day of a range (the running subsystem).
* ``request-retraining`` + ``retrain``: manual request; ``--forecast-config`` trains with
  another forecast config (development drills, e.g. a deliberately bad candidate).
* ``start-shadow``, ``promote`` (named approver), ``force-promote`` (dev drill only),
  ``rollback``, ``reject``: explicit lifecycle actions, each audited.
* ``status``, ``history``: registry state and the audit trail; ``sync-tracking``: rebuild
  the MLflow aliases from the registry.
* ``drill``: the pre-registered lifecycle drill (configs/mlops/lifecycle_acceptance.toml)
  over a steady and a drift world; ``--database-url`` is then an ADMIN url on a throwaway
  local server: the drill (re)creates its own ``praxis_mlops_drill_*`` databases.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from praxis.config import Environment, get_settings
from praxis.control.db import make_engine, migrate
from praxis.domain.model_lifecycle import ModelStage
from praxis.domain.states import InvalidTransition
from praxis.forecasting.artifact import load_artifact
from praxis.forecasting.config import ForecastConfig, load_forecast_config
from praxis.logging import configure_logging
from praxis.mlops.config import load_mlops_config
from praxis.mlops.drill import DrillEnv, DrillPlan, run_drill
from praxis.mlops.lineage import LineageError
from praxis.mlops.pipeline import DemandLifecycle
from praxis.mlops.registry import ModelRegistry, PromotionRefused
from praxis.mlops.scoring import load_data
from praxis.mlops.serving import artifact_path, load_registered
from praxis.mlops.store import RegistryError, RegistryStore
from praxis.mlops.tracking import ExperimentTracker, MlflowTracker, NullTracker
from praxis.mlops.training import reference_profile, version_record
from praxis.mlops.validation import Gate, validate_candidate
from praxis.provenance import code_revision


def _write(payload: Any, path: Path | None = None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    sys.stdout.write(text)


def _tracker(args: argparse.Namespace) -> ExperimentTracker:
    uri = args.tracking_uri or get_settings().mlflow_tracking_uri
    if not uri:
        return NullTracker()
    return MlflowTracker(uri, args.tracking_artifacts, experiment="praxis-demand-forecaster")


def _lifecycle(args: argparse.Namespace) -> DemandLifecycle:
    store = RegistryStore(make_engine(args.database_url))
    registry = ModelRegistry(
        store,
        load_mlops_config(args.config),
        tracker=_tracker(args),
        environment=Environment(args.environment),
    )
    return DemandLifecycle(
        registry,
        args.warehouse,
        args.models,
        load_forecast_config(),
        code_revision(),
    )


def cmd_register(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    art = load_artifact(args.artifact)
    backtest = json.loads(args.backtest.read_text()) if args.backtest else None
    end = date.fromisoformat(str(art.manifest["training"]["panel_end"]))
    data = load_data(args.warehouse, end=end, series=art.series)
    reference = reference_profile(art, data, lc.cfg.monitoring)
    record = version_record(art, args.artifact, backtest, reference)
    row = lc.registry.register(
        record, files={"artifact": args.artifact}, actor=args.actor, reason=args.reason
    )
    if backtest is not None:
        report_path = args.models / "reports" / f"{row.model_version}.backtest.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(backtest, sort_keys=True))
    _write({"model_version": row.model_version, "stage": row.stage.value})
    return 0


def cmd_validate(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    row = lc.store.require(args.version)
    report_path = args.models / "reports" / f"{row.model_version}.backtest.json"
    backtest = json.loads(report_path.read_text()) if report_path.exists() else None
    champion = lc.store.one_in_stage(lc.registry.model_name, ModelStage.CHAMPION)
    report = validate_candidate(
        artifact_path(row.record.artifact_uri),
        row.record,
        warehouse=args.warehouse,
        as_of=args.as_of,
        gate=Gate(lc.cfg, lc.forecast_config.acceptance),
        backtest=backtest,
        champion=lc.artifact(champion) if champion else None,
    )
    after = lc.registry.record_validation(row.model_version, report)
    _write({**report, "stage": after.stage.value}, args.report)
    return 0 if report["passed"] else 1


def cmd_run_day(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    _write(lc.run_day(args.as_of), args.report)
    return 0


def cmd_operate(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    days = []
    d = args.start
    while d <= args.end:
        days.append(lc.run_day(d))
        d = date.fromordinal(d.toordinal() + 1)
    _write({"days": days}, args.report)
    return 0


def cmd_request(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    request, created = lc.request(args.as_of, args.reason)
    _write({"request_id": request.request_id, "created": created, "status": request.status})
    return 0


def cmd_retrain(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    cfg = load_forecast_config(args.forecast_config) if args.forecast_config else None
    out = lc.retrain(args.as_of, forecast_config=cfg)
    if out is None:
        _write({"retrained": False, "reason": "no open retraining request"})
        return 0
    _write(out, args.report)
    return 0 if out["validation_passed"] or out.get("duplicate") else 1


def cmd_lifecycle_action(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    reg = lc.registry
    if args.command == "start-shadow":
        row = reg.start_shadow(args.version, as_of=args.as_of, actor=args.actor)
        _write({"model_version": row.model_version, "stage": row.stage.value})
        return 0
    if args.command == "promote":
        decision = reg.promote(
            args.version, approver=args.actor, reason=args.reason, as_of=args.as_of
        )
    elif args.command == "force-promote":
        decision = reg.force_promote(
            args.version, actor=args.actor, reason=args.reason, as_of=args.as_of
        )
    elif args.command == "rollback":
        decision = reg.rollback(actor=args.actor, reason=args.reason, as_of=args.as_of)
    else:
        decision = reg.reject(args.version, actor=args.actor, reason=args.reason)
    _write(
        {
            "decision_id": decision.decision_id,
            "moves": [[e.model_version, e.from_stage, e.to_stage] for e in decision.events],
        }
    )
    return 0


def _drill_registry(admin_url: str, name: str, args: argparse.Namespace) -> ModelRegistry:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    db = f"praxis_mlops_drill_{name}_s{args.seed}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    admin.dispose()
    url = make_url(admin_url).set(database=db).render_as_string(hide_password=False)
    migrate(url)
    tracker = MlflowTracker(
        f"sqlite:///{(args.out / 'mlflow.db').resolve()}",
        args.out / "mlflow-artifacts",
        experiment=f"praxis-drill-{name}-s{args.seed}",
    )
    return ModelRegistry(
        RegistryStore(make_engine(url)),
        load_mlops_config(args.config),
        tracker=tracker,
        environment=Environment.DEV,
    )


def cmd_drill(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    pinned = load_forecast_config()
    raw = pinned.model_dump(mode="json")
    raw["ridge"]["alpha"] = 1e9  # deliberately misconfigured recipe (rejection / rollback)
    raw["lightgbm"]["num_boost_round"] = 1
    env = DrillEnv(
        make_registry=lambda name: _drill_registry(args.database_url, name, args),
        policy=load_mlops_config(args.config),
        candidates_root=args.out / "models",
        forecast_config=pinned,
        bad_config=ForecastConfig.model_validate(raw),
        code_revision=code_revision(),
    )
    plan = DrillPlan.from_acceptance(args.start_date)
    report = run_drill(plan, args.steady, args.drift, env)
    report["seed"] = args.seed
    _write(report, args.out / "drill.json")
    return 0 if report["passed"] else 1


def cmd_status(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    name = lc.registry.model_name
    out: dict[str, Any] = {"model_name": name, "policy_version": lc.cfg.policy_version}
    for stage in ModelStage:
        out[stage.value] = [r.model_version for r in lc.store.in_stage(name, stage)]
    out["retraining_requests"] = [
        {
            "request_id": r.request_id,
            "status": r.status,
            "triggers": r.triggers,
            "as_of": r.as_of,
            "candidate": r.candidate_version,
        }
        for r in lc.store.requests(name)
    ]
    champion = lc.store.one_in_stage(name, ModelStage.CHAMPION)
    if champion is not None:
        art = load_registered(champion)
        out["champion_lineage"] = {
            "model_version": champion.model_version,
            "data_version": champion.record.data_version,
            "feature_version": champion.record.feature_version,
            "code_revision": champion.record.code_revision,
            "artifact_checksum": champion.record.artifact_checksum,
            "artifact_loads": art.model_version == champion.model_version,
            "tracking_run_id": champion.tracking_run_id,
        }
    _write(out, args.report)
    return 0


def cmd_history(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    events = lc.store.events(model_name=lc.registry.model_name)
    _write([e.__dict__ for e in events], args.report)
    return 0


def cmd_sync(lc: DemandLifecycle, args: argparse.Namespace) -> int:
    lc.registry.sync_tracker()
    _write({"synced": True})
    return 0


COMMANDS = {
    "register": cmd_register,
    "validate": cmd_validate,
    "run-day": cmd_run_day,
    "operate": cmd_operate,
    "request-retraining": cmd_request,
    "retrain": cmd_retrain,
    "start-shadow": cmd_lifecycle_action,
    "promote": cmd_lifecycle_action,
    "force-promote": cmd_lifecycle_action,
    "rollback": cmd_lifecycle_action,
    "reject": cmd_lifecycle_action,
    "status": cmd_status,
    "history": cmd_history,
    "sync-tracking": cmd_sync,
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.mlops")
    p.add_argument("--database-url", required=True)
    p.add_argument("--warehouse", type=Path, default=Path("data/warehouse/praxis.duckdb"))
    p.add_argument("--models", type=Path, default=Path("data/models/demand_candidates"))
    p.add_argument("--config", type=Path, default=None, help="lifecycle policy TOML")
    p.add_argument("--tracking-uri", default=None, help="MLflow URI (default: settings)")
    p.add_argument("--tracking-artifacts", type=Path, default=Path("data/mlflow/artifacts"))
    p.add_argument("--environment", default="local", choices=[e.value for e in Environment])
    p.add_argument("--actor", default="mlops-automation")
    p.add_argument("--reason", default="operator request")
    p.add_argument("--report", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    reg = sub.add_parser("register")
    reg.add_argument("--artifact", type=Path, required=True)
    reg.add_argument("--backtest", type=Path, default=None)
    for name in ("validate", "start-shadow", "promote", "force-promote", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("--version", required=True)
        sp.add_argument("--as-of", type=date.fromisoformat, required=name != "reject")
    rb = sub.add_parser("rollback")
    rb.add_argument("--as-of", type=date.fromisoformat, default=None)
    for name in ("run-day", "request-retraining"):
        sub.add_parser(name).add_argument("--as-of", type=date.fromisoformat, required=True)
    rt = sub.add_parser("retrain")
    rt.add_argument("--as-of", type=date.fromisoformat, required=True)
    rt.add_argument("--forecast-config", type=Path, default=None)
    op = sub.add_parser("operate")
    op.add_argument("--start", type=date.fromisoformat, required=True)
    op.add_argument("--end", type=date.fromisoformat, required=True)
    for name in ("status", "history", "sync-tracking"):
        sub.add_parser(name)
    dr = sub.add_parser("drill")
    dr.add_argument("--steady", type=Path, required=True, help="steady world warehouse")
    dr.add_argument("--drift", type=Path, required=True, help="drift world warehouse")
    dr.add_argument("--seed", type=int, required=True)
    dr.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 1, 5))
    dr.add_argument("--out", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("praxis-mlops", get_settings().log_level.value)
    if args.command == "migrate":
        migrate(args.database_url)
        _write({"migrated": True})
        return 0
    if args.command == "drill":
        return cmd_drill(args)
    lc = _lifecycle(args)
    try:
        return COMMANDS[args.command](lc, args)
    except (PromotionRefused, LineageError) as exc:
        _write({"refused": str(exc)})
        return 1
    except (RegistryError, InvalidTransition) as exc:
        _write({"error": str(exc)})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
