"""Save and load the price-elasticity artifact (consumed by the Phase 6 optimiser).

An artifact is ``<root>/<model_version>/`` with ``estimates.json`` (segment elasticities with
uncertainty) and ``manifest.json`` (full lineage, CLAUDE.md s10). The model version derives
from content, never the timestamp. Only an analysis whose validity and diagnostic gates passed
can be saved; loading verifies every checksum.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from praxis.elasticity.analysis import Analysis
from praxis.elasticity.config import ElasticityConfig, ExperimentRegistry

ARTIFACT_FORMAT = 1
MODEL_NAME = "price_elasticity"
MANIFEST = "manifest.json"
ESTIMATES = "estimates.json"
ESTIMAND = (
    "causal own-price elasticity of requested units per active day (log-log slope), from "
    "randomised price tests; design-variance-weighted mean over the analysed units"
)
_VOLATILE = ("created_at", "artifact_checksum", "model_version")


class ArtifactError(RuntimeError):
    """Artifact is missing, corrupted, ungated or incompatible with this code."""


@dataclass(frozen=True)
class ElasticityArtifact:
    manifest: dict[str, Any]
    estimates: dict[str, Any]

    @property
    def model_version(self) -> str:
        return str(self.manifest["model_version"])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _content_checksum(manifest: dict[str, Any]) -> str:
    return _sha(_dumps({k: v for k, v in manifest.items() if k not in _VOLATILE}).encode())


def build_artifact(
    analysis: Analysis,
    cfg: ElasticityConfig,
    registry: ExperimentRegistry,
    *,
    code_revision: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Returns (manifest, estimates JSON text). Refuses an analysis that failed a gate."""
    if not analysis.passed:
        raise ArtifactError("analysis failed a validity or diagnostic gate; refusing to save")
    r = analysis.report
    hier = r["hierarchical"]
    estimates = {
        "estimand": ESTIMAND,
        "is_synthetic": True,
        "pooled": {"ols": r["estimates"]["pooled"], "hierarchical": hier["pooled"]},
        "tier": hier["tier"],
        "industry": hier["industry"],
        "cell": hier["cells"],
        "tested_log_price_ratios": sorted({d.log_ratio for d in registry.experiments}),
    }
    text = _dumps(estimates)
    manifest: dict[str, Any] = {
        "artifact_format": ARTIFACT_FORMAT,
        "model_name": MODEL_NAME,
        "model": "two-stage hierarchical Bayesian (PyMC) over log-log cell slopes (ADR 0012)",
        "is_synthetic": True,
        "created_at": (now or datetime.now(UTC)).isoformat(),
        "code_revision": code_revision,
        "data_version": r["data_version"],
        "feature_version": "experiment_units.v1",
        "registry_hash": registry.config_hash,
        "experiments": [d.id for d in registry.experiments],
        "config": cfg.model_dump(mode="json"),
        "config_hash": cfg.config_hash,
        "parameters": hier["sampler"],
        "metrics": {
            "validity_passed": r["validity"]["passed"],
            "diagnostics": hier["diagnostics"]["convergence"],
            "posterior_predictive_p": hier["diagnostics"]["posterior_predictive_p"],
            "prior_sensitivity_max_shift_sd": hier["diagnostics"]["prior_sensitivity"][
                "max_shift_sd"
            ],
            "units": r["units"],
        },
        "files": {ESTIMATES: _sha(text.encode())},
    }
    checksum = _content_checksum(manifest)
    manifest["artifact_checksum"] = checksum
    manifest["model_version"] = f"elasticity-hier-{checksum[:12]}"
    return manifest, text


def save_artifact(manifest: dict[str, Any], estimates_text: str, root: Path) -> Path:
    """Write atomically to ``root/<model_version>``; an existing identical version is kept."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / str(manifest["model_version"])
    if target.exists():
        load_artifact(target)  # raises if the existing copy is corrupt
        return target
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=root))
    try:
        (tmp / ESTIMATES).write_text(estimates_text, encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def load_artifact(path: Path) -> ElasticityArtifact:
    try:
        manifest: dict[str, Any] = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactError(f"no {MANIFEST} in {path}") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactError("manifest is not valid JSON") from exc
    if manifest.get("artifact_format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"unsupported artifact format {manifest.get('artifact_format')}")
    if _content_checksum(manifest) != manifest.get("artifact_checksum"):
        raise ArtifactError("manifest checksum mismatch")
    file = path / ESTIMATES
    if not file.is_file():
        raise ArtifactError(f"missing {ESTIMATES}")
    data = file.read_bytes()
    if _sha(data) != manifest["files"][ESTIMATES]:
        raise ArtifactError(f"checksum mismatch for {ESTIMATES}")
    return ElasticityArtifact(manifest, json.loads(data))
