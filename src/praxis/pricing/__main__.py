"""CLI: ``python -m praxis.pricing {decide|approve|execute|show}``.

* ``decide``: run one pricing cycle as of a date (default mode: the policy's, i.e. shadow).
  Without ``--database-url`` decisions are kept in memory and written to ``--out`` only;
  recommend / execute modes need the Postgres audit store. Exit 1 if any record failed.
* ``approve`` / ``execute`` / ``show``: act on one recorded decision (Postgres).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from praxis.config import get_settings
from praxis.control.db import make_engine
from praxis.elasticity.config import load_registry
from praxis.forecasting.artifact import load_artifact
from praxis.forecasting.service import WarehouseFeatureSource
from praxis.logging import configure_logging
from praxis.pricing.config import Mode, load_policy
from praxis.pricing.evidence import EvidenceError, PricingEvidence, load_evidence
from praxis.pricing.inputs import WarehouseProblemSource, forecast_factory
from praxis.pricing.service import PolicyError, PricingService
from praxis.pricing.store import (
    DecisionStore,
    MemoryDecisionStore,
    PostgresDecisionStore,
    StoreError,
)


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _evidence(args: argparse.Namespace) -> tuple[PricingEvidence | None, str]:
    try:
        registry = load_registry(args.registry)
        return load_evidence(args.elasticity_model, args.elasticity_report, registry), ""
    except (EvidenceError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _store(url: str | None) -> DecisionStore:
    return MemoryDecisionStore() if url is None else PostgresDecisionStore(make_engine(url))


def cmd_decide(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    mode = Mode(args.mode) if args.mode else policy.policy.mode
    if mode is not Mode.SHADOW and args.database_url is None:
        raise SystemExit(f"{mode.value} mode needs --database-url (the audit store)")
    store = _store(args.database_url)
    evidence, error = _evidence(args)
    source = WarehouseProblemSource(
        policy=policy,
        warehouse=args.db,
        forecaster=forecast_factory(
            lambda: load_artifact(args.forecast_model), WarehouseFeatureSource(args.db)
        ),
        evidence=evidence,
        store=store,
        evidence_error=error,
    )
    try:
        result = PricingService(policy, source, store).run_cycle(args.as_of, mode)
    except PolicyError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    payload = {
        **result.summary(),
        "is_synthetic": True,
        "records": [i.decision.record() for i in result.items],
    }
    _write(args.out, payload)
    sys.stdout.write(json.dumps(result.summary()["decisions"]) + "\n")
    return 0 if result.audit_complete else 1


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        _store(args.database_url).approve(args.decision_id, args.approver)
    except StoreError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    try:
        done = _store(args.database_url).execute(args.decision_id, args.executor)
    except StoreError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    _write(None, {"execution": done.__dict__})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    record = _store(args.database_url).get_record(args.decision_id)
    if record is None:
        sys.stderr.write("no such decision\n")
        return 2
    _write(None, record)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m praxis.pricing")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("decide", help="run one pricing cycle")
    d.add_argument("--db", type=Path, required=True, help="DuckDB warehouse with dbt marts")
    d.add_argument("--forecast-model", type=Path, required=True)
    d.add_argument("--elasticity-model", type=Path, required=True)
    d.add_argument("--elasticity-report", type=Path, required=True)
    d.add_argument("--registry", type=Path, default=None)
    d.add_argument("--policy", type=Path, default=None)
    d.add_argument("--as-of", type=date.fromisoformat, required=True)
    d.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    d.add_argument("--database-url", default=None)
    d.add_argument("--out", type=Path, default=None)
    d.set_defaults(func=cmd_decide)
    for name, func in (("approve", cmd_approve), ("execute", cmd_execute), ("show", cmd_show)):
        s = sub.add_parser(name)
        s.add_argument("--database-url", required=True)
        s.add_argument("--decision-id", required=True)
        if name == "approve":
            s.add_argument("--approver", required=True)
        if name == "execute":
            s.add_argument("--executor", default="pricing-cli")
        s.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
