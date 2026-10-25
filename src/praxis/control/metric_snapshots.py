"""Cross-process metric snapshots in the control plane (ADR 0020).

Telemetry is read in-process (ADR 0018), but Praxis runs as several processes: the API,
the event consumers, the pricing cycle, the MLOps pipeline and the ops monitor. Each one
publishes its latest ``MetricsView`` here under (component, instance), and readers (the
ops monitor, the health API, the AI operator) merge the unexpired rows into one fleet view.

Merge rules, chosen so that ``MetricsView`` aggregations stay correct:

* counters and histograms are cumulative per process, so each instance stays its own
  series and ``total`` / ``quantile`` add them up;
* a gauge with the same name and labels in several instances keeps only the newest value.

Expired rows (``expires_at`` in the past: the process stopped or stalled) are never merged;
they are returned as stale sources so a reader can show them. Points whose metric is not in
the catalogue are dropped and counted, never trusted. Publishing must never break the host
process (observability is off the decision path): ``SnapshotPublisher`` logs and carries on
when the database is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, exc, text

from praxis.control.db import transient_errors
from praxis.errors import TransientError
from praxis.telemetry import METRICS, MetricsView, Point, telemetry

logger = logging.getLogger(__name__)


def default_instance() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class SourceStatus:
    """One publishing process as a reader sees it."""

    component: str
    instance: str
    published_at: datetime
    expires_at: datetime
    started_at: datetime
    age_s: float
    stale: bool
    points: int
    rejected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "instance": self.instance,
            "published_at": self.published_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "started_at": self.started_at.isoformat(),
            "age_s": round(self.age_s, 3),
            "stale": self.stale,
            "points": self.points,
            "rejected": self.rejected,
        }


@dataclass(frozen=True)
class FleetView:
    view: MetricsView
    sources: tuple[SourceStatus, ...] = ()

    @property
    def stale(self) -> tuple[SourceStatus, ...]:
        return tuple(s for s in self.sources if s.stale)


def encode(view: MetricsView) -> list[dict[str, Any]]:
    return [
        {"name": p.name, "labels": p.labels, "value": p.value, "count": p.count,
         "bounds": list(p.bounds), "buckets": list(p.buckets)}
        for p in view.points
    ]  # fmt: skip


def _decode(raw: list[dict[str, Any]]) -> tuple[list[Point], int]:
    points, rejected = [], 0
    for item in raw:
        spec = METRICS.get(str(item.get("name")))
        labels = {str(k): str(v) for k, v in dict(item.get("labels") or {}).items()}
        if spec is None or set(labels) != set(spec.labels):
            rejected += 1
            continue
        points.append(
            Point(
                spec.name,
                labels,
                float(item["value"]),
                int(item.get("count", 0)),
                tuple(float(b) for b in item.get("bounds", ())),
                tuple(int(b) for b in item.get("buckets", ())),
            )
        )
    return points, rejected


def merge(parts: list[tuple[datetime, list[Point]]]) -> MetricsView:
    """Merge per-process points (see module docstring); ``parts`` carry publish times."""
    series: list[Point] = []
    newest: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[datetime, Point]] = {}
    for published_at, points in parts:
        for p in points:
            if METRICS[p.name].kind != "gauge":
                series.append(p)
                continue
            key = (p.name, tuple(sorted(p.labels.items())))
            seen = newest.get(key)
            if seen is None or published_at >= seen[0]:
                newest[key] = (published_at, p)
    return MetricsView(tuple(series) + tuple(p for _, p in newest.values()))


class MetricSnapshotStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(
        self,
        component: str,
        instance: str,
        view: MetricsView,
        *,
        now: datetime,
        ttl: timedelta,
        started_at: datetime,
    ) -> None:
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops_metric_snapshots (component, instance, published_at, "
                    "expires_at, started_at, points) VALUES (:c, :i, :now, :exp, :start, "
                    "CAST(:points AS JSONB)) ON CONFLICT (component, instance) DO UPDATE SET "
                    "published_at = excluded.published_at, expires_at = excluded.expires_at, "
                    "points = excluded.points"
                ),
                {"c": component, "i": instance, "now": now, "exp": now + ttl,
                 "start": started_at, "points": json.dumps(encode(view))},
            )  # fmt: skip

    def read(
        self,
        now: datetime,
        *,
        local: MetricsView | None = None,
        exclude: tuple[str, str] | None = None,
    ) -> FleetView:
        """Fleet view at ``now``. ``local`` (the reader's own live metrics) wins over its
        published copy, which ``exclude`` = (component, instance) leaves out."""
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT component, instance, published_at, expires_at, started_at, points "
                    "FROM ops_metric_snapshots ORDER BY component, instance"
                )
            ).all()
        parts: list[tuple[datetime, list[Point]]] = []
        sources: list[SourceStatus] = []
        for component, instance, published_at, expires_at, started_at, raw in rows:
            if exclude == (component, instance):
                continue
            points, rejected = _decode(raw)
            stale = expires_at <= now
            if not stale:
                parts.append((published_at, points))
            sources.append(
                SourceStatus(
                    component,
                    instance,
                    published_at,
                    expires_at,
                    started_at,
                    max((now - published_at).total_seconds(), 0.0),
                    stale,
                    len(points),
                    rejected,
                )
            )
        if local is not None:
            parts.append((now, list(local.points)))
        return FleetView(merge(parts), tuple(sources))

    def prune(self, expired_before: datetime) -> int:
        """Delete rows that expired before ``expired_before`` (old instances of restarts)."""
        with transient_errors(), self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM ops_metric_snapshots WHERE expires_at < :t"),
                {"t": expired_before},
            )
            return int(result.rowcount)


@dataclass
class SnapshotPublisher:
    """Publishes this process's metrics at most every ``interval``; never raises on a
    database failure. A failed attempt also waits one interval, so an outage cannot put a
    connect timeout on every iteration of the caller's loop."""

    store: MetricSnapshotStore
    component: str
    interval: timedelta = timedelta(seconds=15)
    ttl: timedelta | None = None  # default: four intervals
    instance: str = field(default_factory=default_instance)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    last_attempt: datetime | None = None
    failures: int = 0

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    def maybe_publish(self) -> bool:
        now = self.clock()
        if self.last_attempt is not None and now - self.last_attempt < self.interval:
            return False
        return self.publish(now)

    def publish(self, now: datetime | None = None) -> bool:
        now = now or self.clock()
        started = self.started_at or now
        self.last_attempt = now
        try:
            self.store.publish(
                self.component, self.instance, telemetry().snapshot(), now=now,
                ttl=self.ttl or self.interval * 4, started_at=min(started, now),
            )  # fmt: skip
        except (TransientError, exc.SQLAlchemyError) as error:
            self.failures += 1
            logger.warning(
                "metric snapshot not published",
                extra={"component": self.component, "error": type(error).__name__},
            )
            return False
        return True


# A daily batch job's metrics stay current until its next run, plus slack for a late run.
BATCH_TTL = timedelta(hours=26)


def publish_batch(engine: Engine, component: str, ttl: timedelta = BATCH_TTL) -> bool:
    """Publish this process's metrics once, at the end of a batch job (never raises)."""
    return SnapshotPublisher(MetricSnapshotStore(engine), component, ttl=ttl).publish()
