"""CLI: ``python -m praxis.forecasting --db WAREHOUSE {backtest|train|predict}``.

* ``backtest``: rolling-origin evaluation of every model; JSON report; exit 1 if the
  pre-registered acceptance fails.
* ``train``: fit on all observed data, train twice to prove reproducibility, save the
  artifact (refused unless the supplied backtest report passed acceptance).
* ``predict``: forecast from a saved artifact as of a given date (offline demo / debug);
  exit 2 when no trustworthy forecast exists (e.g. features too old).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from praxis.config import get_settings
from praxis.forecasting.artifact import load_artifact, save_artifact, train_artifact
from praxis.forecasting.backtest import SpikeDays, run_backtest
from praxis.forecasting.config import ForecastConfig, load_forecast_config
from praxis.forecasting.panel import DemandPanel, PricePlan
from praxis.forecasting.service import (
    ForecastRequestError,
    ForecastService,
    ForecastUnavailable,
    WarehouseFeatureSource,
)
from praxis.forecasting.warehouse import connect, load_panel, load_price_plan, usage_date_range
from praxis.logging import configure_logging
from praxis.provenance import code_revision
from praxis.simulator.config import load_config


def _load(db: Path) -> tuple[DemandPanel, PricePlan]:
    con = connect(db)
    try:
        rng = usage_date_range(con)
        if rng is None:
            raise SystemExit("warehouse has no usage data")
        start, end = rng
        return load_panel(con, start, end), load_price_plan(con, until=end)
    finally:
        con.close()


def spike_days_from_scenario(scenario: Path) -> set[tuple[str, str, date]]:
    """(region, product, date) cells inside a simulated demand spike (evaluation labels)."""
    cfg = load_config(scenario=scenario)
    products = [p.id for p in cfg.products]
    out: set[tuple[str, str, date]] = set()
    for sp in cfg.infrastructure.demand_spikes:
        for day in range(sp.start_day, sp.end_day):
            d = cfg.run.start_date + timedelta(days=day)
            for p in [sp.product] if sp.product else products:
                out.add((sp.region, p, d))
    return out


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def cmd_backtest(args: argparse.Namespace, cfg: ForecastConfig) -> int:
    panel, plan = _load(args.db)
    spikes: SpikeDays = spike_days_from_scenario(args.scenario) if args.scenario else set()
    report = run_backtest(panel, plan, cfg, spike_days=spikes)
    report["code_revision"] = code_revision()
    _write(args.report, report)
    summary = {
        name: {k: round(v, 4) for k, v in m["overall"].items()}
        for name, m in report["models"].items()
    }
    sys.stdout.write(json.dumps({"overall": summary, "acceptance": report["acceptance"]}) + "\n")
    return 0 if report["acceptance"]["passed"] else 1


def cmd_train(args: argparse.Namespace, cfg: ForecastConfig) -> int:
    backtest = json.loads(args.backtest_report.read_text()) if args.backtest_report else None
    if backtest is None and not args.allow_unvalidated:
        raise SystemExit("refusing to train without a passing --backtest-report")
    if backtest is not None and not backtest.get("acceptance", {}).get("passed"):
        raise SystemExit("backtest report did not pass acceptance; not saving an artifact")
    if backtest is not None and backtest.get("config_hash") != cfg.config_hash:
        raise SystemExit("backtest report was produced with a different forecast config")
    panel, plan = _load(args.db)
    if backtest is not None and backtest.get("data_version") != panel.data_version():
        raise SystemExit("backtest report was produced on different data")
    rev = code_revision()
    manifest, files = train_artifact(panel, plan, cfg, code_revision=rev, backtest=backtest)
    again, files_again = train_artifact(panel, plan, cfg, code_revision=rev, backtest=backtest)
    reproducible = files == files_again and manifest["model_version"] == again["model_version"]
    if not reproducible:
        sys.stderr.write("retraining did not reproduce the model; artifact not saved\n")
        return 1
    path = save_artifact(manifest, files, args.out)
    _write(
        None,
        {
            "model_version": manifest["model_version"],
            "path": str(path),
            "reproducible": reproducible,
            "data_version": manifest["data_version"],
            "code_revision": rev,
        },
    )
    return 0


def cmd_predict(args: argparse.Namespace, cfg: ForecastConfig) -> int:
    del cfg
    artifact = load_artifact(args.model)
    now = datetime.combine(args.as_of, time(6), tzinfo=UTC) if args.as_of else datetime.now(UTC)
    service = ForecastService(artifact, WarehouseFeatureSource(args.db), clock=lambda: now)
    try:
        result = service.forecast()
    except (ForecastUnavailable, ForecastRequestError) as exc:
        sys.stderr.write(json.dumps({"error": exc.code, "detail": str(exc)}) + "\n")
        return 2
    totals: dict[str, dict[str, float]] = {}
    for p in result.points:
        day = totals.setdefault(p.target_date.isoformat(), {"point": 0.0})
        day["point"] += p.point
    _write(
        args.out,
        {
            "model_version": result.model_version,
            "feature_date": result.feature_date,
            "freshness": result.freshness.value,
            "source": result.source.value,
            "points": len(result.points),
            "metrics": service.metrics.snapshot(),
            "total_point_by_target_date": {
                k: float(np.round(v["point"], 1)) for k, v in sorted(totals.items())
            },
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="praxis.forecasting")
    ap.add_argument("--db", type=Path, required=True, help="DuckDB warehouse with dbt marts")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--external", action="store_true", help="add external-signal features")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest")
    bt.add_argument("--scenario", type=Path, default=None, help="label spike days from it")
    bt.add_argument("--report", type=Path, default=None)
    tr = sub.add_parser("train")
    tr.add_argument("--backtest-report", type=Path, default=None)
    tr.add_argument("--allow-unvalidated", action="store_true")
    tr.add_argument("--out", type=Path, default=Path("data/models/demand"))
    pr = sub.add_parser("predict")
    pr.add_argument("--model", type=Path, required=True)
    pr.add_argument("--as-of", type=date.fromisoformat, default=None, help="pretend 'today'")
    pr.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    configure_logging("praxis-forecasting", get_settings().log_level.value)
    cfg = load_forecast_config(args.config).with_external(args.external)
    handlers = {"backtest": cmd_backtest, "train": cmd_train, "predict": cmd_predict}
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
