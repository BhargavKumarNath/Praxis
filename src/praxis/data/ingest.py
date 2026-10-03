"""Ingestion orchestration: fetch -> raw archive -> validate/normalise -> warehouse.

Properties this module guarantees (each has a test):

* Idempotent: the same request returning the same bytes is the same batch; re-ingesting
  it changes neither the archive nor the warehouse's logical content.
* Raw first: the body is archived before any transformation, so a parser bug never loses data.
* Isolated failure: an outage, rejection or missing key on one request is reported and the
  run continues; existing warehouse data is never touched by a failed fetch.
* Contract drift quarantines the batch (raw kept, no normalised rows, status recorded).
* Replay: ``replay`` rebuilds normalised rows from the archive with no network access.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from praxis.data.errors import (
    MissingCredentialError,
    SchemaDriftError,
    SourceRejectedError,
    SourceUnavailableError,
)
from praxis.data.fetch import HttpFetcher
from praxis.data.models import (
    BatchProvenance,
    ParsedBatch,
    QualityStatus,
    RequestSpec,
    SourceId,
    TimeWindow,
    make_batch_id,
    sha256_hex,
)
from praxis.data.raw_store import LocalRawStore, RawMeta
from praxis.data.sources import Source
from praxis.data.warehouse import Warehouse

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"  # already archived; warehouse re-asserted idempotently
    QUARANTINED = "quarantined"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    MISSING_CREDENTIAL = "missing_credential"


_SUCCESS = {Outcome.STORED, Outcome.DUPLICATE}


@dataclass(frozen=True)
class BatchResult:
    source: SourceId
    series_id: str
    outcome: Outcome
    batch_id: str | None = None
    new_records: int = 0
    detail: str | None = None


@dataclass
class IngestReport:
    results: list[BatchResult] = field(default_factory=list)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.results if r.outcome is outcome)

    @property
    def new_records(self) -> int:
        return sum(r.new_records for r in self.results)

    @property
    def all_succeeded(self) -> bool:
        return all(r.outcome in _SUCCESS for r in self.results)

    def summary(self) -> dict[str, int]:
        out = {o.value: self.count(o) for o in Outcome}
        out["new_records"] = self.new_records
        return out


class IngestService:
    def __init__(
        self,
        sources: dict[SourceId, Source],
        fetcher: HttpFetcher,
        raw_store: LocalRawStore,
        warehouse: Warehouse,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sources = sources
        self._fetcher = fetcher
        self._raw = raw_store
        self._wh = warehouse
        self._clock = clock

    def ingest(self, source_id: SourceId, window: TimeWindow) -> IngestReport:
        source = self._sources[source_id]
        report = IngestReport()
        try:
            credentials = source.credential_params()
        except MissingCredentialError as exc:
            report.results.append(
                BatchResult(source_id, "*", Outcome.MISSING_CREDENTIAL, detail=str(exc))
            )
            logger.warning("source skipped", extra={"source": source_id.value, "reason": str(exc)})
            return report
        for request in source.build_requests(window):
            report.results.append(self._ingest_one(source, request, credentials))
        return report

    def replay(self, source_id: SourceId | None = None) -> IngestReport:
        """Rebuild normalised rows from the archive. Makes no network call."""
        report = IngestReport()
        for meta in self._raw.iter_meta(source_id):
            body = self._raw.read_body(meta)
            source = self._sources[meta.request.source]
            parsed, reason = _parse(source, body, meta.request, meta.batch_id, meta.retrieved_at)
            report.results.append(self._persist(meta, parsed, reason, duplicate=True))
        return report

    # --- internals --------------------------------------------------------------------
    def _ingest_one(
        self, source: Source, request: RequestSpec, credentials: dict[str, str]
    ) -> BatchResult:
        try:
            fetched = self._fetcher.get(request.url, {**request.params, **credentials})
        except SourceUnavailableError as exc:
            return self._failed(request, Outcome.UNAVAILABLE, exc)
        except SourceRejectedError as exc:
            return self._failed(request, Outcome.REJECTED, exc)

        batch_id = make_batch_id(request, source.fingerprint(fetched.body))
        existing = self._raw.get_meta(request.source, batch_id)
        retrieved_at = existing.retrieved_at if existing else self._clock()
        parsed, reason = _parse(source, fetched.body, request, batch_id, retrieved_at)
        meta = existing or RawMeta(
            batch_id=batch_id,
            request=request,
            endpoint=fetched.endpoint,
            retrieved_at=retrieved_at,
            checksum_sha256=sha256_hex(fetched.body),
            http_status=fetched.status,
            quarantine_reason=reason,
        )
        if existing is None:
            self._raw.put(meta, fetched.body)
        return self._persist(meta, parsed, reason, duplicate=existing is not None)

    def _persist(
        self, meta: RawMeta, parsed: ParsedBatch | None, reason: str | None, *, duplicate: bool
    ) -> BatchResult:
        request = meta.request
        if parsed is None:
            quality, outcome = QualityStatus.QUARANTINED, Outcome.QUARANTINED
        else:
            quality = parsed.quality
            outcome = Outcome.DUPLICATE if duplicate else Outcome.STORED
        self._wh.record_batch(
            BatchProvenance(
                batch_id=meta.batch_id,
                source=request.source,
                series_id=request.series_id,
                endpoint=meta.endpoint,
                retrieved_at=meta.retrieved_at,
                source_timestamp_min=parsed.source_timestamp_min if parsed else None,
                source_timestamp_max=parsed.source_timestamp_max if parsed else None,
                checksum_sha256=meta.checksum_sha256,
                http_status=meta.http_status,
                quality_status=quality,
                quality_detail=reason,
                record_count=len(parsed.records) if parsed else 0,
                skipped_count=parsed.skipped if parsed else 0,
            )
        )
        new = self._wh.upsert_signals(parsed.records) if parsed else 0
        logger.info(
            "batch processed",
            extra={
                "source": request.source.value,
                "series_id": request.series_id,
                "batch_id": meta.batch_id,
                "outcome": outcome.value,
                "new_records": new,
                "quality_status": quality.value,
            },
        )
        return BatchResult(request.source, request.series_id, outcome, meta.batch_id, new, reason)

    @staticmethod
    def _failed(request: RequestSpec, outcome: Outcome, exc: Exception) -> BatchResult:
        logger.error(
            "source request failed permanently",
            extra={
                "source": request.source.value,
                "series_id": request.series_id,
                "reason": str(exc),
            },
        )
        return BatchResult(request.source, request.series_id, outcome, detail=str(exc))


def _parse(
    source: Source, body: bytes, request: RequestSpec, batch_id: str, retrieved_at: datetime
) -> tuple[ParsedBatch | None, str | None]:
    try:
        return source.parse(body, request, batch_id=batch_id, retrieved_at=retrieved_at), None
    except SchemaDriftError as exc:
        return None, str(exc)
