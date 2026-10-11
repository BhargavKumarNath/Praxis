"""Pre-registered customer-value configuration: pinned, validated."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from praxis.customer.config import CustomerValueConfig, load_config

REPO = Path(__file__).resolve().parents[2]
# Pinned 2026-10-10, before any churn / CLV model was fitted on any world (ADR 0016); re-pinned
# 2026-10-11 for the trend calibration (ADR 0016 second amendment, dev worlds only). A change is
# a new configuration and needs an ADR (CLAUDE.md s16): never edit to flatter a result.
PINNED = {
    "configs/customer/value.toml": (
        "f47c5a629e43f8f313bffdc834c69c3c5771a71e2f2fc59fb7dbf8bf290eb71e"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_unchanged(path: str) -> None:
    assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == PINNED[path]


def test_the_registered_valuation() -> None:
    cfg = load_config()
    assert cfg.label.window_days == 28  # the churn evidence window of the pricing policy
    assert cfg.clv.horizon_days == 365  # = the Phase 6 proxy cap: the method changes, not it
    assert cfg.clv.annual_discount_rate == 0.10
    assert cfg.segments.names == ("starter", "growth", "enterprise")
    assert len(cfg.config_hash) == 64


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("label", "snapshot_step_days", 7),  # overlapping label windows
        ("label", "first_snapshot_day", 10),  # shorter than the feature history
        ("clv", "annual_discount_rate", 1.0),
        ("monitoring", "max_calibration_ratio", 1.0),
    ],
)
def test_invalid_configurations_are_rejected(section: str, field: str, value: object) -> None:
    raw = tomllib.loads((REPO / "configs/customer/value.toml").read_text())
    raw[section][field] = value
    with pytest.raises(ValidationError):
        CustomerValueConfig.model_validate(raw)
