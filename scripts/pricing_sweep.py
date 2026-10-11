"""Operating characteristics of the pricing method over many simulated worlds (SYNTHETIC).

Usage: ``python scripts/pricing_sweep.py data/pricing/seed1*-c1000/shadow/shadow.json --out F``

One shadow report per world (``make pricing-world pricing-shadow SEED=n``). A single world is
one draw of the experiment noise; whether a METHOD is sound is a property of its distribution
over worlds: how often the pre-registered acceptance passes, how often a change is right, how
much value is captured, and whether the churn-response posterior is calibrated (the z-scores
of the response truth implies should look standard normal; a 90% interval should cover ~90%).
Development worlds only: never pass held-out reports here before their single evaluation.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

Z90 = 1.6449


def world_row(report: dict[str, Any]) -> dict[str, Any]:
    checks = {c["name"]: c for c in report["checks"]}
    rep = report["reported"]
    changes = checks["direction"].get("changes", 0) or 0
    return {
        "seed": report["seed"],
        "passed": report["passed"],
        "failed": sorted(n for n, c in checks.items() if not c["passed"]),
        "changes": changes,
        "direction": checks["direction"].get("value"),
        "true_delta_per_day": checks["no_harm"]["true_delta_per_day"] / 1e6,
        "oracle_per_day": rep["oracle_true_delta_per_day"] / 1e6,
        "captured_share": rep["captured_share"],
        "status_counts": report["status_counts"],
    }


def calibration(reports: list[dict[str, Any]]) -> dict[str, Any]:
    zs = [
        float(v["z"])
        for r in reports
        for v in r["reported"]["churn_prediction"].get("by_product", {}).values()
        if v.get("z") is not None and math.isfinite(v["z"])
    ]
    if not zs:
        return {"n": 0}
    return {
        "n": len(zs),
        "z_mean": mean(zs),
        "z_sd": pstdev(zs),
        "coverage_90": sum(abs(z) <= Z90 for z in zs) / len(zs),
        "share_true_above_upper_95": sum(z > Z90 for z in zs) / len(zs),
    }


def summarise(reports: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [world_row(r) for r in reports]
    changed = [r for r in rows if r["changes"]]
    return {
        "is_synthetic": True,
        "worlds": len(rows),
        "pass_rate": sum(r["passed"] for r in rows) / len(rows) if rows else None,
        "worlds_with_changes": len(changed),
        "changes": sum(r["changes"] for r in rows),
        "right_changes": sum(round((r["direction"] or 0) * r["changes"]) for r in changed),
        "true_delta_per_day_total": sum(r["true_delta_per_day"] for r in rows),
        "oracle_per_day_total": sum(r["oracle_per_day"] for r in rows),
        "response_calibration": calibration(reports),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pricing_sweep")
    p.add_argument("reports", type=Path, nargs="+")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    out = summarise([json.loads(path.read_text()) for path in args.reports])
    text = json.dumps(out, indent=2, sort_keys=True) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
