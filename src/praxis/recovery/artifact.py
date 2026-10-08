"""Save and load the recovery artifact (CLAUDE.md s10 lineage).

``<root>/<model_version>/`` holds the survival model, the calibrated classifier, the
rate-table baseline and ``manifest.json`` (model version, champion, data / feature /
policy versions, code revision, parameters, metrics, checksums, creation time). The model
version is content-derived, so identical inputs give an identical version. Loading verifies
every checksum and the feature version; any mismatch is an ``ArtifactError`` and the dunning
policy falls back to the deterministic baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from praxis.recovery.classifier import CalibratedClassifier, RateTable
from praxis.recovery.config import ModelConfig
from praxis.recovery.features import FEATURE_VERSION, RecoveryFeatures
from praxis.recovery.survival import CureWeibull

ARTIFACT_FORMAT = 1
MODEL_NAME = "payment_recovery"
MANIFEST = "manifest.json"
SURVIVAL_FILE = "survival.json"
CLASSIFIER_FILE = "classifier.json"
BOOSTER_FILE = "classifier.txt"
TABLE_FILE = "rate_table.json"
_VOLATILE = ("created_at", "artifact_checksum", "model_version")

F64 = NDArray[np.float64]


class ArtifactError(RuntimeError):
    """Artifact is missing, corrupted or incompatible with this code."""


class CollectibleModel(Protocol):
    name: str

    def prob_collectible(self, rows: list[RecoveryFeatures], t: F64) -> F64: ...


@dataclass(frozen=True)
class RecoveryArtifact:
    manifest: dict[str, Any]
    survival: CureWeibull
    classifier: CalibratedClassifier
    table: RateTable

    @property
    def model_version(self) -> str:
        return str(self.manifest["model_version"])

    @property
    def created_at(self) -> datetime:
        return datetime.fromisoformat(str(self.manifest["created_at"]))

    @property
    def champion(self) -> CollectibleModel:
        return self.model(str(self.manifest["champion"]))

    @property
    def challenger(self) -> CollectibleModel:
        return self.model(str(self.manifest["challenger"]))

    def model(self, name: str) -> CollectibleModel:
        if name == self.survival.name:
            return self.survival
        if name == self.classifier.name:
            return self.classifier
        raise ArtifactError(f"unknown model {name}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_checksum(manifest: dict[str, Any]) -> str:
    core = {k: v for k, v in manifest.items() if k not in _VOLATILE}
    return _sha(json.dumps(core, sort_keys=True, separators=(",", ":")).encode())


def _dump(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def model_files(
    survival: CureWeibull, classifier: CalibratedClassifier, table: RateTable
) -> dict[str, str]:
    return {
        SURVIVAL_FILE: _dump(survival.state()),
        CLASSIFIER_FILE: _dump(classifier.state()),
        BOOSTER_FILE: classifier.model_string(),
        TABLE_FILE: _dump(
            {
                "prior_weight": table.prior_weight,
                "overall": table.overall,
                "reason_rate": table.reason_rate,
                "cells": [[r, d, h, n] for (r, d), (h, n) in sorted(table.cells.items())],
            }
        ),
    }


def finalise_manifest(core: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    manifest = {
        **core,
        "artifact_format": ARTIFACT_FORMAT,
        "model_name": MODEL_NAME,
        "feature_version": FEATURE_VERSION,
        "is_synthetic": True,
        "files": {name: _sha(text.encode()) for name, text in sorted(files.items())},
    }
    checksum = _content_checksum(manifest)
    manifest["artifact_checksum"] = checksum
    short = "surv" if manifest["champion"] == CureWeibull.name else "clf"
    manifest["model_version"] = f"recovery-{short}-{checksum[:12]}"
    return manifest


def save_artifact(manifest: dict[str, Any], files: dict[str, str], root: Path) -> Path:
    """Write atomically to ``root/<model_version>``; an existing identical version is kept."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / str(manifest["model_version"])
    if target.exists():
        load_artifact(target)  # raises if the existing copy is corrupt
        return target
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=root))
    try:
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def _read_files(path: Path, manifest: dict[str, Any]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for name in (SURVIVAL_FILE, CLASSIFIER_FILE, BOOSTER_FILE, TABLE_FILE):
        digest = manifest.get("files", {}).get(name)
        file = path / name
        if digest is None or not file.is_file():
            raise ArtifactError(f"missing model file {name}")
        data = file.read_bytes()
        if _sha(data) != digest:
            raise ArtifactError(f"checksum mismatch for {name}")
        texts[name] = data.decode("utf-8")
    return texts


def load_artifact(path: Path) -> RecoveryArtifact:
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        raise ArtifactError(f"no {MANIFEST} in {path}")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError("manifest is not valid JSON") from exc
    if manifest.get("artifact_format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"unsupported artifact format {manifest.get('artifact_format')}")
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise ArtifactError(
            f"artifact feature version {manifest.get('feature_version')} != {FEATURE_VERSION}"
        )
    if _content_checksum(manifest) != manifest.get("artifact_checksum"):
        raise ArtifactError("manifest checksum mismatch")
    texts = _read_files(path, manifest)
    cfg = ModelConfig.model_validate(manifest["model_config"])
    try:
        survival = CureWeibull.from_state(json.loads(texts[SURVIVAL_FILE]))
        classifier = CalibratedClassifier.from_state(
            cfg.classifier, json.loads(texts[CLASSIFIER_FILE]), texts[BOOSTER_FILE]
        )
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    t = json.loads(texts[TABLE_FILE])
    table = RateTable(
        prior_weight=float(t["prior_weight"]),
        cells={(r, int(d)): (float(h), float(n)) for r, d, h, n in t["cells"]},
        reason_rate={k: float(v) for k, v in t["reason_rate"].items()},
        overall=float(t["overall"]),
    )
    artifact = RecoveryArtifact(manifest, survival, classifier, table)
    artifact.model(str(manifest["champion"]))  # validates the champion name
    return artifact


def latest_artifact(root: Path) -> Path | None:
    """Most recently created artifact under ``root`` (by manifest ``created_at``)."""
    best: tuple[str, Path] | None = None
    for manifest in root.glob(f"*/{MANIFEST}"):
        try:
            created = str(json.loads(manifest.read_text(encoding="utf-8"))["created_at"])
        except (json.JSONDecodeError, KeyError):
            continue
        if best is None or created > best[0]:
            best = (created, manifest.parent)
    return best[1] if best else None
