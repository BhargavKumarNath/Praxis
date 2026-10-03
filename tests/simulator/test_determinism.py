"""Determinism (required_test.md section 7).

Same seed + config + initial state must give an identical canonical output. Tolerances:
none; checksums are compared exactly. Streams from numpy's Generator are only stable for
a fixed numpy version (NEP 19), so the golden checksum is tied to the locked numpy and
must be regenerated deliberately, with the reason recorded, if numpy changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

from praxis.simulator.runner import run_simulation
from tests.simulator.sim_helpers import scenario

GOLDEN_NUMPY = "2.5.3"
GOLDEN_STREAM_SHA256 = "1d6b7dfbc66c8fb09e9c3c17c37eb92096d08d60f4bc4f29e4aa2865260a8015"


def test_same_seed_same_checksum() -> None:
    cfg = scenario(n_customers=200, days=14)
    a, b = run_simulation(cfg, 7), run_simulation(cfg, 7)
    assert a.checksum == b.checksum
    assert a.ground_truth_checksum == b.ground_truth_checksum
    assert a.event_count == b.event_count > 0
    assert a.counts == b.counts


def test_different_seed_changes_population_and_events() -> None:
    cfg = scenario(n_customers=200, days=14)
    a, b = run_simulation(cfg, 7), run_simulation(cfg, 8)
    assert a.checksum != b.checksum
    assert a.ground_truth_checksum != b.ground_truth_checksum


def test_config_change_changes_identity_and_output() -> None:
    a = scenario(n_customers=200, days=14)
    b = scenario(n_customers=200, days=14, billing={"retry_success_base": 0.2})
    assert a.config_hash != b.config_hash
    assert run_simulation(a, 7).checksum != run_simulation(b, 7).checksum


def test_replay_of_one_engine_is_identical() -> None:
    from praxis.simulator.engine import Engine
    from praxis.simulator.population import generate_population

    cfg = scenario(n_customers=100, days=10)
    pop = generate_population(cfg, 3)
    engine = Engine(cfg, 3, pop)
    assert list(engine.run()) == list(engine.run())


def test_independent_of_python_hash_seed() -> None:
    """Dict/set ordering dependence would make the checksum vary with PYTHONHASHSEED."""
    sums = set()
    for hash_seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "praxis.simulator",
                "--customers",
                "150",
                "--days",
                "10",
                "--seed",
                "5",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout
        sums.add(json.loads(out)["stream_checksum_sha256"])
    assert len(sums) == 1


def test_golden_checksum_pinned_to_numpy_version() -> None:
    assert np.__version__ == GOLDEN_NUMPY, (
        "numpy changed: Generator streams may differ (NEP 19). Re-verify determinism, then "
        "update GOLDEN_NUMPY and the golden checksum deliberately."
    )
    result = run_simulation(scenario(n_customers=200, days=14), 7)
    assert result.checksum == GOLDEN_STREAM_SHA256
