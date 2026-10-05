from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.perf_check import compare, main, measure

SIM = {"event_count": 1_000_000, "elapsed_s": 20.0, "peak_rss_mb": 200.0}
EVENTS = {"events_per_s": 2000.0, "peak_rss_mb": 500.0, "matches_oracle": True}


def baseline(**metrics: float) -> dict[str, Any]:
    base = measure(SIM, EVENTS) | metrics
    return {"min_throughput_ratio": 0.5, "max_memory_ratio": 1.5, "metrics": base}


def test_within_tolerance_passes() -> None:
    assert compare(measure(SIM, EVENTS), baseline(), EVENTS) == []


def test_throughput_collapse_and_memory_blowup_are_flagged() -> None:
    slow = compare(measure(SIM, EVENTS), baseline(events_chaos_events_per_s=5000.0), EVENTS)
    assert len(slow) == 1 and "events_chaos_events_per_s" in slow[0]
    fat = compare(measure(SIM, EVENTS), baseline(simulator_peak_rss_mb=100.0), EVENTS)
    assert len(fat) == 1 and "simulator_peak_rss_mb" in fat[0]


def test_oracle_mismatch_always_fails() -> None:
    bad = EVENTS | {"matches_oracle": False}
    assert any("oracle" in p for p in compare(measure(SIM, bad), baseline(), bad))


def test_write_then_check_round_trip(tmp_path: Path) -> None:
    sim, events, base = tmp_path / "s.json", tmp_path / "e.json", tmp_path / "b.json"
    sim.write_text(json.dumps(SIM))
    events.write_text(json.dumps(EVENTS))
    args = [str(sim), str(events), "--baseline", str(base), "--env", "x"]
    assert main(args) == 0  # no baseline yet: report only
    assert main([*args, "--write"]) == 0
    assert "x" in json.loads(base.read_text())["environments"]
    assert main(args) == 0
    events.write_text(json.dumps(EVENTS | {"events_per_s": 100.0}))
    assert main(args) == 1
    events.write_text(json.dumps(EVENTS | {"matches_oracle": False}))
    assert main([*args, "--write"]) == 1
