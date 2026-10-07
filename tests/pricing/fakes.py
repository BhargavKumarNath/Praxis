"""Evidence fixtures: a valid, checksummed elasticity artifact and its analysis report.

Built by hand (no PyMC fit) with the artifact module's own checksum scheme, so the loader's
verification is exercised for real. SYNTHETIC numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from praxis.elasticity.artifact import (
    ARTIFACT_FORMAT,
    ESTIMATES,
    _content_checksum,  # the artifact's own scheme: tests must not drift from it
    save_artifact,
)
from praxis.elasticity.config import ExperimentRegistry, load_registry

DATA_VERSION = "units-test0000000000"
TIERS = {"starter": (-1.9, 0.03), "growth": (-1.2, 0.03), "enterprise": (-0.7, 0.03)}


def write_artifact(
    root: Path,
    registry: ExperimentRegistry | None = None,
    *,
    tiers: dict[str, tuple[float, float]] | None = None,
    data_version: str = DATA_VERSION,
) -> Path:
    registry = registry or load_registry()
    estimates = {
        "estimand": "test",
        "is_synthetic": True,
        "tier": {t: {"mean": m, "sd": s} for t, (m, s) in (tiers or TIERS).items()},
    }
    text = json.dumps(estimates, sort_keys=True, separators=(",", ":"))
    manifest: dict[str, Any] = {
        "artifact_format": ARTIFACT_FORMAT,
        "model_name": "price_elasticity",
        "is_synthetic": True,
        "created_at": "2026-02-28T00:00:00+00:00",
        "data_version": data_version,
        "registry_hash": registry.config_hash,
        "files": {ESTIMATES: hashlib.sha256(text.encode()).hexdigest()},
    }
    checksum = _content_checksum(manifest)
    manifest["artifact_checksum"] = checksum
    manifest["model_version"] = f"elasticity-hier-{checksum[:12]}"
    return save_artifact(manifest, text, root)


def write_report(
    path: Path,
    registry: ExperimentRegistry | None = None,
    *,
    data_version: str = DATA_VERSION,
    passed: bool = True,
    churn: tuple[float, float] = (0.035, 0.040),
    units: int = 2000,
) -> Path:
    registry = registry or load_registry()
    report = {
        "data_version": data_version,
        "registry_hash": registry.config_hash,
        "validity": {"passed": passed, "checks": []},
        "experiments": [
            {
                "id": e.id,
                "product": e.product,
                "guardrails": {
                    "control": {"units": units, "churn_rate": churn[0]},
                    "treatment": {"units": units, "churn_rate": churn[1]},
                },
            }
            for e in registry.experiments
        ],
    }
    path.write_text(json.dumps(report))
    return path
