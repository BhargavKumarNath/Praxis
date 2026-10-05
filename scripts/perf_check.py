"""Performance regression check against a committed, per-environment baseline.

Inputs are the JSON the tools already emit: the simulator CLI summary (10K x 28d) and the
streaming ``run-local`` chaos report (1K x 28d). Throughput may not drop below
``min_throughput_ratio`` x baseline and peak RSS may not exceed ``max_memory_ratio`` x baseline.
The chaos run must also still match its oracle (a correctness regression is never "noise").

Baselines are keyed by environment (``local``, ``github``) because hardware differs. With no
baseline for the current environment the check reports and passes; record one deliberately
with ``--write`` (``make perf-baseline``), never to hide a regression.

Usage: python scripts/perf_check.py SIM_JSON EVENTS_JSON [--baseline FILE] [--env ENV] [--write]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "benchmarks" / "perf_baseline.json"
THROUGHPUT = ("simulator_events_per_s", "events_chaos_events_per_s")
MEMORY = ("simulator_peak_rss_mb", "events_chaos_peak_rss_mb")


def measure(sim: dict[str, Any], events: dict[str, Any]) -> dict[str, float]:
    return {
        "simulator_events_per_s": sim["event_count"] / sim["elapsed_s"],
        "simulator_peak_rss_mb": float(sim["peak_rss_mb"]),
        "events_chaos_events_per_s": float(events["events_per_s"]),
        "events_chaos_peak_rss_mb": float(events["peak_rss_mb"]),
    }


def compare(
    current: dict[str, float], baseline: dict[str, Any], events: dict[str, Any]
) -> list[str]:
    problems = []
    if not events.get("matches_oracle"):
        problems.append("chaos run no longer matches its oracle (correctness regression)")
    ref = baseline["metrics"]
    for key in THROUGHPUT:
        floor = ref[key] * baseline["min_throughput_ratio"]
        if current[key] < floor:
            problems.append(f"{key}: {current[key]:.0f} < {floor:.0f} (baseline {ref[key]:.0f})")
    for key in MEMORY:
        ceiling = ref[key] * baseline["max_memory_ratio"]
        if current[key] > ceiling:
            problems.append(f"{key}: {current[key]:.0f} > {ceiling:.0f} (baseline {ref[key]:.0f})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sim", type=Path)
    ap.add_argument("events", type=Path)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--env", default="github" if os.environ.get("GITHUB_ACTIONS") else "local")
    ap.add_argument("--write", action="store_true", help="record current values as baseline")
    args = ap.parse_args(argv)
    sim = json.loads(args.sim.read_text())
    events = json.loads(args.events.read_text())
    current = measure(sim, events)
    store: dict[str, Any] = (
        json.loads(args.baseline.read_text()) if args.baseline.exists() else {"environments": {}}
    )
    sys.stdout.write(json.dumps({"env": args.env, "current": current}, indent=2) + "\n")
    if args.write:
        if not events.get("matches_oracle"):
            sys.stderr.write("refusing to record a baseline from a run that fails its oracle\n")
            return 1
        store["environments"][args.env] = {
            "recorded_at": datetime.now(UTC).date().isoformat(),
            "min_throughput_ratio": 0.5,
            "max_memory_ratio": 1.5,
            "metrics": {k: round(v, 1) for k, v in current.items()},
        }
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
        sys.stdout.write(f"baseline for {args.env!r} written to {args.baseline}\n")
        return 0
    baseline = store["environments"].get(args.env)
    if baseline is None:
        sys.stdout.write(f"no baseline for env {args.env!r}: reported only (record one)\n")
        return 0 if events.get("matches_oracle") else 1
    problems = compare(current, baseline, events)
    for p in problems:
        sys.stderr.write(f"perf regression: {p}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
