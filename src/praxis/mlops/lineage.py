"""Lineage completeness: what every registered model version must carry (CLAUDE.md s10).

required_test.md s16: data reference, feature version, code revision, parameters, metrics
and artifact checksum; plus the training window, the training time and the synthetic flag.
A version missing any of them cannot be registered.
"""

from __future__ import annotations

import re

from praxis.mlops.store import VersionRecord


class LineageError(ValueError):
    """A version's lineage is incomplete or malformed."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_LINEAGE_KEYS = ("training_start", "training_end", "rows", "config_hash")


def missing_lineage(record: VersionRecord) -> list[str]:
    """Names of every missing or malformed lineage field (empty = complete)."""
    missing = [
        name
        for name in (
            "model_name",
            "model_version",
            "artifact_uri",
            "data_version",
            "feature_version",
            "code_revision",
        )
        if not str(getattr(record, name) or "").strip()
    ]
    if not _SHA256.match(record.artifact_checksum or ""):
        missing.append("artifact_checksum")
    if not record.params:
        missing.append("params")
    if not record.metrics:
        missing.append("metrics")
    missing += [
        f"lineage.{k}" for k in REQUIRED_LINEAGE_KEYS if record.lineage.get(k) in (None, "")
    ]
    if record.trained_at.tzinfo is None:
        missing.append("trained_at (timezone)")
    return missing
