"""Save and load the customer-value (churn hazard) artifact (CLAUDE.md s10 lineage).

``<root>/<model_version>/`` holds the voluntary-churn GLM, the tier rate-table baseline, the
involuntary-churn rate table and ``manifest.json`` (model version, champion, data / feature /
config versions, code revision, parameters, metrics, file checksums, creation time). The model
version is content-derived, so identical inputs give an identical version. Loading verifies
every checksum and the feature version; any mismatch is an ``ArtifactError``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from praxis.customer.features import FEATURE_VERSION
from praxis.customer.hazard import HazardModel, PoissonHazard, RateTable

ARTIFACT_FORMAT = 1
MODEL_NAME = "customer_churn"
MANIFEST = "manifest.json"
GLM_FILE = "glm.json"
TABLE_FILE = "rate_table.json"
INVOLUNTARY_FILE = "involuntary.json"
FILES = (GLM_FILE, TABLE_FILE, INVOLUNTARY_FILE)
_VOLATILE = ("created_at", "artifact_checksum", "model_version")


class ArtifactError(RuntimeError):
    """Artifact is missing, corrupted or incompatible with this code."""


@dataclass(frozen=True)
class CustomerArtifact:
    manifest: dict[str, Any]
    glm: PoissonHazard
    table: RateTable
    involuntary: RateTable

    @property
    def model_version(self) -> str:
        return str(self.manifest["model_version"])

    @property
    def champion(self) -> HazardModel:
        name = str(self.manifest["champion"])
        if name == self.glm.name:
            return self.glm
        if name == self.table.name:
            return self.table
        raise ArtifactError(f"unknown champion {name}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _content_checksum(manifest: dict[str, Any]) -> str:
    core = {k: v for k, v in manifest.items() if k not in _VOLATILE}
    return _sha(_dump(core).encode())


def model_files(glm: PoissonHazard, table: RateTable, involuntary: RateTable) -> dict[str, str]:
    return {
        GLM_FILE: _dump(glm.state()),
        TABLE_FILE: _dump(table.state()),
        INVOLUNTARY_FILE: _dump(involuntary.state()),
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
    short = "glm" if manifest["champion"] == "poisson_glm" else "table"
    manifest["model_version"] = f"churn-{short}-{checksum[:12]}"
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


def _read(path: Path, manifest: dict[str, Any], names: Sequence[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for name in names:
        digest = manifest.get("files", {}).get(name)
        file = path / name
        if digest is None or not file.is_file():
            raise ArtifactError(f"missing model file {name}")
        data = file.read_bytes()
        if _sha(data) != digest:
            raise ArtifactError(f"checksum mismatch for {name}")
        texts[name] = data.decode("utf-8")
    return texts


def load_artifact(path: Path) -> CustomerArtifact:
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        raise ArtifactError(f"no {MANIFEST} in {path}")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError("manifest is not valid JSON") from exc
    if manifest.get("artifact_format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"unsupported artifact format {manifest.get('artifact_format')}")
    if manifest.get("model_name") != MODEL_NAME:
        raise ArtifactError(f"not a {MODEL_NAME} artifact")
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise ArtifactError(
            f"artifact feature version {manifest.get('feature_version')} != {FEATURE_VERSION}"
        )
    if _content_checksum(manifest) != manifest.get("artifact_checksum"):
        raise ArtifactError("manifest checksum mismatch")
    texts = _read(path, manifest, FILES)
    try:
        artifact = CustomerArtifact(
            manifest,
            PoissonHazard.from_state(json.loads(texts[GLM_FILE])),
            RateTable.from_state(json.loads(texts[TABLE_FILE])),
            RateTable.from_state(json.loads(texts[INVOLUNTARY_FILE])),
        )
    except (ValueError, KeyError) as exc:
        raise ArtifactError(f"model state unusable: {exc}") from exc
    artifact.champion  # noqa: B018 - validates the champion name
    return artifact


def find_artifact(root: Path, data_version: str) -> Path | None:
    """The artifact under ``root`` trained on exactly ``data_version`` (lineage guard)."""
    if (root / MANIFEST).is_file():
        root = root.parent
    for manifest in sorted(root.glob(f"*/{MANIFEST}")):
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("data_version") == data_version:
                return manifest.parent
        except json.JSONDecodeError:
            continue
    return None
