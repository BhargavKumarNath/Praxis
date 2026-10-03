"""Runner provenance, persistence and CLI (data rules: provenance, replayability, labelling)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from praxis.simulator.runner import main, run_simulation
from tests.simulator.sim_helpers import scenario


def test_persisted_run_is_replayable_and_labelled_synthetic(tmp_path: Path) -> None:
    cfg = scenario(n_customers=120, days=12)
    result = run_simulation(cfg, 9, out_dir=tmp_path, validate=True)
    events = (tmp_path / "events.ndjson").read_bytes()
    assert hashlib.sha256(events).hexdigest() == result.checksum
    assert events.count(b"\n") == result.event_count
    first = json.loads(events.splitlines()[0])
    assert first["is_synthetic"] is True and first["source"] == "simulator"

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    for key in (
        "source",
        "retrieved_at",
        "source_period",
        "event_schema_version",
        "payload_schema_version",
        "batch_id",
        "quality_status",
        "config_hash",
        "stream_checksum_sha256",
        "numpy_version",
        "code_revision",
        "seed",
    ):
        assert key in manifest, key
    assert manifest["is_synthetic"] is True
    assert manifest["quality_status"] == "validated"
    assert manifest["config_hash"] == cfg.config_hash

    truth = np.load(tmp_path / "ground_truth.npz")
    assert bool(truth["synthetic"]) is True
    assert str(truth["config_hash"]) == cfg.config_hash
    assert truth["elasticity"].shape == (120,) and np.all(truth["elasticity"] < 0)


def test_unvalidated_run_says_so() -> None:
    result = run_simulation(scenario(n_customers=50, days=5), 1)
    assert result.quality_status == "unvalidated"


def test_cli_prints_summary_and_writes_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--customers",
            "60",
            "--days",
            "8",
            "--seed",
            "3",
            "--validate",
            "--schema-every",
            "5",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert "retrieved_at" not in summary
    assert summary["n_customers"] == 60 and summary["quality_status"] == "validated"
    assert (tmp_path / "events.ndjson").exists()
