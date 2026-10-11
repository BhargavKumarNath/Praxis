"""CLI: ``python -m praxis.elasticity --db WAREHOUSE {analyze|churn-response}``.

``analyze --out DIR [--models ROOT]``:
Reads the experiment registry and the warehouse (never the simulator), runs the analysis and
writes ``DIR/report.json`` plus ``DIR/units.json`` (the analysed units, for the ground-truth
evaluation in ``praxis.science``). With ``--models`` it also saves the artifact. Exit 1 when a
validity or diagnostic gate fails (no artifact is saved then).

``churn-response --report DIR/report.json --out FILE`` (Phase 9, ADR 0016): the causal
voluntary-churn response to the bill from the same concurrent tests. Refuses (exit 1) unless
the analysis report passed every validity gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from praxis.config import get_settings
from praxis.elasticity import churn
from praxis.elasticity.analysis import analyse
from praxis.elasticity.artifact import build_artifact, save_artifact
from praxis.elasticity.config import load_elasticity_config, load_registry
from praxis.elasticity.dataset import build_units as build_unit_table
from praxis.elasticity.heterogeneity import heterogeneity
from praxis.elasticity.warehouse import connect, load_extract
from praxis.logging import configure_logging
from praxis.provenance import code_revision


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m praxis.elasticity")
    p.add_argument("--db", type=Path, required=True, help="DuckDB warehouse with dbt marts")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--registry", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze", help="validity + estimates + hierarchical model")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--models", type=Path, default=None, help="artifact root (saved if gates pass)")
    c = sub.add_parser("churn-response", help="causal churn response to the bill (Phase 9)")
    c.add_argument("--report", type=Path, required=True, help="the analysis report.json")
    c.add_argument("--out", type=Path, required=True)
    return p


def _churn_response(args: argparse.Namespace) -> int:
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not report.get("validity", {}).get("passed", False):
        sys.stderr.write("refused: the analysis report failed a validity gate\n")
        return 1
    registry = load_registry(args.registry)
    if report.get("registry_hash") != registry.config_hash:
        sys.stderr.write("refused: the report used another experiment registry\n")
        return 1
    cfg = load_elasticity_config(args.config)
    con = connect(args.db)
    try:
        doc = churn.estimate(con, registry, cfg, report_data_version=str(report["data_version"]))
        units, designs, _ = build_unit_table(load_extract(con, registry, cfg), cfg)
    finally:
        con.close()
    if units.data_version() != report["data_version"]:
        sys.stderr.write("refused: the warehouse units differ from the analysed units\n")
        return 1
    doc["elasticity_heterogeneity"] = heterogeneity(units, [d.log_ratio for d in designs])
    doc["code_revision"] = code_revision()
    doc["checksum"] = churn.checksum(doc)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    summary = {p: {"gamma": v["gamma"], "se": v["se"]} for p, v in sorted(doc["products"].items())}
    sys.stdout.write(json.dumps({"pooled": doc["pooled_fixed"], "products": summary}) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("praxis-elasticity", get_settings().log_level)
    if args.command == "churn-response":
        return _churn_response(args)
    log = logging.getLogger("praxis.elasticity")
    cfg = load_elasticity_config(args.config)
    registry = load_registry(args.registry)
    con = connect(args.db)
    try:
        extract = load_extract(con, registry, cfg)
    finally:
        con.close()
    analysis = analyse(extract, cfg)
    report = {
        **analysis.report,
        "code_revision": code_revision(),
        "config_hash": cfg.config_hash,
        "registry_hash": registry.config_hash,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    units = [
        {**row, "in_model": bool(flag)}
        for row, flag in zip(analysis.units.records(), analysis.in_model.tolist(), strict=True)
    ]
    (args.out / "units.json").write_text(json.dumps(units, separators=(",", ":")) + "\n")
    summary = {
        "data_version": report["data_version"],
        "validity_passed": report["validity"]["passed"],
        "diagnostics_passed": report["hierarchical"]["diagnostics"]["passed"],
        "pooled": report["estimates"]["pooled"],
    }
    if not analysis.passed:
        log.error("elasticity.gate_failed", extra=summary)
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return 1
    if args.models is not None:
        manifest, text = build_artifact(
            analysis, cfg, registry, code_revision=report["code_revision"]
        )
        summary["artifact"] = str(save_artifact(manifest, text, args.models))
    log.info("elasticity.analysed", extra=summary)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
