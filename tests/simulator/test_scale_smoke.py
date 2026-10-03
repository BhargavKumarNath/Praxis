"""Scale smoke: 10,000 customers must run inside a documented resource budget.

Budget, fixed BEFORE the first 10K run (dev machine: 16 cores, 14 GB RAM, 10,000 customers,
28 simulated days, streaming with full stream-state validation and Pydantic validation of
every 50th event, nothing persisted):
    wall clock <= 120 s,  peak RSS <= 1536 MB.
The measurement runs in a subprocess so peak RSS reflects the simulator alone.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

WALL_BUDGET_S = 120.0
RSS_BUDGET_MB = 1536.0


@pytest.mark.slow
def test_10k_customers_within_budget_and_valid() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "praxis.simulator",
            "--customers",
            "10000",
            "--days",
            "28",
            "--seed",
            "42",
            "--validate",
            "--schema-every",
            "50",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    m = json.loads(out)
    assert m["quality_status"] == "validated"
    assert m["is_synthetic"] is True
    assert m["event_count"] > 100_000
    assert m["elapsed_s"] <= WALL_BUDGET_S, m["elapsed_s"]
    assert m["peak_rss_mb"] <= RSS_BUDGET_MB, m["peak_rss_mb"]
    assert len(m["stream_checksum_sha256"]) == 64
