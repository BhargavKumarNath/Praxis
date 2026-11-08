"""Score the pre-registered 2026-11 held-out pricing evaluation (configs/pricing/
evaluation_2026_11.toml) from the per-world shadow reports. Reads only; each world was built
and evaluated once. Money is in micros of GBP per day (SYNTHETIC)."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def main(out: Path) -> int:
    cfg = tomllib.loads((REPO / "configs/pricing/evaluation_2026_11.toml").read_text())
    worlds = []
    for seed in cfg["seeds"]:
        path = REPO / f"data/pricing/seed{seed}-c1000/shadow/shadow.json"
        r = json.loads(path.read_text())
        checks = {c["name"]: c for c in r["checks"]}
        rep = r["reported"]
        worlds.append(
            {
                "seed": seed,
                "passed": r["passed"],
                "failed_checks": sorted(k for k, c in checks.items() if not c["passed"]),
                "status_counts": r["status_counts"],
                "changes": checks["direction"].get("changes", 0),
                "direction": checks["direction"].get("value"),
                "true_delta_gbp_per_day": rep["chosen_true_delta_per_day"] / 1e6,
                "oracle_gbp_per_day": rep["oracle_true_delta_per_day"] / 1e6,
                "captured_share": rep.get("captured_share"),
                "naive_unconstrained_gbp_per_day": rep["naive_baseline_true_delta_per_day"] / 1e6,
                "forecast_artifact": r["forecast_artifact"],
                "policy_version": r["policy_version"],
            }
        )
    deltas = np.array([w["true_delta_gbp_per_day"] for w in worlds])
    h = cfg["hypotheses"]
    rng = np.random.default_rng(h["h2_bootstrap_seed"])
    boots = deltas[rng.integers(0, len(deltas), (h["h2_bootstrap_samples"], len(deltas)))].mean(1)
    alpha = (1 - h["h2_ci_level"]) / 2
    lo, hi = np.quantile(boots, [alpha, 1 - alpha])
    with_changes = sum(1 for w in worlds if w["changes"])
    result = {
        "registration": "configs/pricing/evaluation_2026_11.toml",
        "registration_version": cfg["version"],
        "is_synthetic": True,
        "worlds": worlds,
        "H1_safety_all_worlds_pass": {
            "passed": all(w["passed"] for w in worlds),
            "failing_worlds": [w["seed"] for w in worlds if not w["passed"]],
        },
        "H2_value_vs_static": {
            "mean_true_delta_gbp_per_day": float(deltas.mean()),
            "ci95_bootstrap": [float(lo), float(hi)],
            "bootstrap_share_le_0": float((boots <= 0).mean()),
            "passed": bool(deltas.mean() > 0 and lo > 0),
            "note": "static pricing scores exactly 0; worlds resampled with replacement",
        },
        "H3_decisions": {
            "worlds_with_changes": with_changes,
            "required": h["h3_min_worlds_with_changes"],
            "passed": with_changes >= h["h3_min_worlds_with_changes"],
        },
        "pooled": {
            "changes": int(sum(w["changes"] for w in worlds)),
            "right_direction": round(sum((w["direction"] or 0) * w["changes"] for w in worlds)),
            "true_delta_gbp_per_day": float(deltas.sum()),
            "oracle_gbp_per_day": float(sum(w["oracle_gbp_per_day"] for w in worlds)),
            "naive_unconstrained_gbp_per_day": float(
                sum(w["naive_unconstrained_gbp_per_day"] for w in worlds)
            ),
            "harmed_worlds": [w["seed"] for w in worlds if w["true_delta_gbp_per_day"] < 0],
        },
    }
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k.startswith("H")}, indent=2))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
