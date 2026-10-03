"""Append-only raw archive of fetched bodies.

Layout (maps one-to-one onto GCS object keys when promoted to Cloud Storage)::

    <root>/<source>/<batch_id>.body        exact bytes received
    <root>/<source>/<batch_id>.meta.json   request, endpoint (no credentials), checksum

``batch_id`` is derived from request and content, so storing the same batch twice is a
no-op. The meta file is written last; its presence marks a committed batch. Nothing is
ever overwritten or deleted. A body whose checksum no longer matches raises on read.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from praxis.data.errors import RawIntegrityError
from praxis.data.models import RequestSpec, SourceId, UtcDatetime, sha256_hex


class RawMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    request: RequestSpec
    endpoint: str
    retrieved_at: UtcDatetime
    checksum_sha256: str
    http_status: int
    quarantine_reason: str | None = None


class LocalRawStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _paths(self, source: SourceId, batch_id: str) -> tuple[Path, Path]:
        base = self._root / source.value
        return base / f"{batch_id}.body", base / f"{batch_id}.meta.json"

    def put(self, meta: RawMeta, body: bytes) -> bool:
        """Store a batch. Returns False (and writes nothing) if it already exists."""
        if sha256_hex(body) != meta.checksum_sha256:
            raise RawIntegrityError("checksum in meta does not match body")
        body_path, meta_path = self._paths(meta.request.source, meta.batch_id)
        if meta_path.exists():
            return False
        body_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(body_path, body)
        _atomic_write(meta_path, meta.model_dump_json(indent=2).encode())
        return True

    def read_body(self, meta: RawMeta) -> bytes:
        body_path, _ = self._paths(meta.request.source, meta.batch_id)
        body = body_path.read_bytes()
        if sha256_hex(body) != meta.checksum_sha256:
            raise RawIntegrityError(f"raw batch {meta.batch_id} failed checksum verification")
        return body

    def iter_meta(self, source: SourceId | None = None) -> Iterator[RawMeta]:
        """Committed batches in deterministic (retrieved_at, batch_id) order."""
        sources = [source] if source else list(SourceId)
        metas: list[RawMeta] = []
        for src in sources:
            for path in (self._root / src.value).glob("*.meta.json"):
                metas.append(RawMeta.model_validate_json(path.read_text()))
        yield from sorted(metas, key=lambda m: (m.retrieved_at, m.batch_id))

    def get_meta(self, source: SourceId, batch_id: str) -> RawMeta | None:
        meta_path = self._paths(source, batch_id)[1]
        if not meta_path.exists():
            return None
        return RawMeta.model_validate_json(meta_path.read_text())


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
