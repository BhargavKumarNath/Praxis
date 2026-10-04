"""Append-only, content-addressed event archive: the replay source of truth.

Layout (maps one-to-one onto GCS object keys, project.md section 8.2)::

    <root>/source=<source>/date=YYYY-MM-DD/hour=HH/part-<sha256[:24]>.ndjson

Files are named by the hash of their content, written atomically and never modified, so
archiving the same batch twice is a no-op and a corrupted file is detected on read.
Pub/Sub retention is short (1 day, cost); replay never depends on it (ADR 0001).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from praxis.events.codec import canonical_bytes


class ArchiveIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveWrite:
    files_written: int
    files_existing: int
    events: int


def _partition(event: Mapping[str, Any]) -> tuple[str, str, str]:
    occurred = str(event["occurred_at"])  # ISO-8601 UTC, e.g. 2026-01-05T00:01:00Z
    return str(event["source"]), occurred[:10], occurred[11:13]


class EventArchive:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, source: str, day: str, hour: str) -> Path:
        return self.root / f"source={source}" / f"date={day}" / f"hour={hour}"

    def append(self, events: Sequence[Mapping[str, Any]]) -> ArchiveWrite:
        groups: dict[tuple[str, str, str], list[bytes]] = defaultdict(list)
        for event in events:
            groups[_partition(event)].append(canonical_bytes(event) + b"\n")
        written = existing = 0
        for key in sorted(groups):
            body = b"".join(groups[key])
            name = f"part-{hashlib.sha256(body).hexdigest()[:24]}.ndjson"
            path = self._dir(*key) / name
            if path.exists():
                existing += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(path)
            written += 1
        return ArchiveWrite(written, existing, len(events))

    def files(self) -> list[Path]:
        return sorted(self.root.glob("source=*/date=*/hour=*/part-*.ndjson"))

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Every archived event once, ordered by (partition, occurred_at, event_id)."""
        by_partition: dict[Path, list[Path]] = defaultdict(list)
        for path in self.files():
            by_partition[path.parent].append(path)
        for part in sorted(by_partition):
            events: dict[str, dict[str, Any]] = {}
            for path in by_partition[part]:
                body = path.read_bytes()
                if not path.name.startswith(f"part-{hashlib.sha256(body).hexdigest()[:24]}"):
                    raise ArchiveIntegrityError(f"archive file {path} failed checksum")
                for line in body.splitlines():
                    event = json.loads(line)
                    events[str(event["event_id"])] = event
            yield from sorted(
                events.values(), key=lambda e: (str(e["occurred_at"]), str(e["event_id"]))
            )
