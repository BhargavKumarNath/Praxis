"""Pre-registered Phase 8 files are pinned; the policy validates its bounds."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from praxis.recovery.config import RecoveryPolicy, load_model_config, load_policy

REPO = Path(__file__).resolve().parents[2]

# Pinned 2026-10-08 before seed 42 was built. acceptance.toml carries the owner-approved
# amendment (ADR 0015) made on the development world only. A change needs an ADR.
PINNED = {
    "configs/simulator/scenarios/recovery_eval.toml": (
        "9a882c6e997806954d2baecab1d4867ef27c329ba6766bfdf953a3f9123f8ffc"
    ),
    "configs/recovery/policy.toml": (
        "b474a00e2830bbfcbe156ef8f02fe9f63ca79428a63ed7158e7bc542e4dea3f2"
    ),
    "configs/recovery/model.toml": (
        "9c1cd155d511e0c81d3c047e9a4d94f620232913f7cc17fec0c65d72847b2c41"
    ),
    "configs/recovery/acceptance.toml": (
        "82f85dd77719d942d9e9cfb317ab71fd7a6c6d75311b33cb58dcba68d514fe05"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_unchanged(path: str) -> None:
    assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == PINNED[path]


def test_policy_and_model_config_load() -> None:
    policy = load_policy()
    assert policy.version.startswith("dunning-") and len(policy.version) == 20
    assert policy.bounds.max_attempts == 3 and policy.baseline_schedule == (3, 10)
    assert load_model_config().config_hash == load_model_config().config_hash


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("baseline", "retry_offsets_days", [3]),  # needs max_attempts - 1 offsets
        ("baseline", "retry_offsets_days", [15, 10]),  # beyond the horizon
        ("baseline", "retry_offsets_days", [0, 7]),
        ("bounds", "horizon_days", 40),  # Cloud Tasks schedules <= 30 days ahead
    ],
)
def test_policy_rejects_invalid_bounds(section: str, field: str, value: object) -> None:
    raw = tomllib.loads((REPO / "configs/recovery/policy.toml").read_text())
    raw[section][field] = value
    with pytest.raises(ValidationError):
        RecoveryPolicy.model_validate(raw)
