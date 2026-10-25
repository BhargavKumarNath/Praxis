"""OpenTelemetry-backed metrics and traces, with an in-process reader (ADR 0018).

Every Praxis metric is declared once in ``METRICS`` (name, kind, label set); emitting an
unknown metric or an undeclared label raises, so dashboards and alert rules can rely on
the catalogue and label cardinality stays bounded. Providers are local to a ``Telemetry``
instance and are NOT installed as the OTel globals: production code calls ``telemetry()``,
tests swap the instance with ``use_telemetry``. An exporter (OTLP, Cloud Monitoring) can be
attached to the same providers later without touching call sites.

Spans adopt the Praxis ``trace_id`` (``praxis.tracing``) as their OTel trace id, so one
event or decision is a single trace across the producer, the consumer, the model, the
optimiser and persistence even though each stage binds its own context. Exception text is
never recorded on spans (it can carry secrets); only the exception type is.
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    Gauge,
    Histogram,
    InMemoryMetricReader,
    Sum,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

from praxis.tracing import current_trace_id

Kind = Literal["counter", "histogram", "gauge"]

# Millisecond buckets shared by every latency histogram (upper bounds; +Inf is implicit).
LATENCY_BOUNDS_MS: tuple[float, ...] = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0,
)  # fmt: skip


@dataclass(frozen=True)
class MetricSpec:
    name: str
    kind: Kind
    description: str
    labels: tuple[str, ...] = ()


def _spec(name: str, kind: Kind, description: str, *labels: str) -> tuple[str, MetricSpec]:
    return name, MetricSpec(name, kind, description, labels)


METRICS: dict[str, MetricSpec] = dict(
    [
        # service
        _spec("http_requests_total", "counter", "HTTP requests", "route", "method", "status"),
        _spec("http_server_errors_total", "counter", "HTTP 5xx responses", "route"),
        _spec("http_request_duration_ms", "histogram", "HTTP latency", "route"),
        _spec("external_calls_total", "counter", "Outbound calls", "service", "outcome"),
        _spec("external_call_duration_ms", "histogram", "Outbound call latency", "service"),
        _spec("webhook_events_total", "counter", "Webhook outcomes", "outcome"),
        # event backbone
        _spec("events_consumed_total", "counter", "Consumer outcomes", "subscription", "outcome"),
        _spec("event_lag_ms", "histogram", "Publish-to-ack latency", "subscription"),
        _spec("queue_backlog", "gauge", "Undelivered messages", "subscription"),
        _spec("dlq_backlog", "gauge", "Dead-lettered messages awaiting redrive", "subscription"),
        # data
        _spec("data_source_age_s", "gauge", "Seconds since the source last delivered", "source"),
        _spec("data_contract_failures_total", "counter", "Contract validation failures", "source"),
        # model
        _spec("model_inference_total", "counter", "Model calls", "model", "outcome"),
        _spec("model_inference_duration_ms", "histogram", "Model call latency", "model"),
        _spec("model_version_info", "gauge", "1 for the serving version", "model", "version"),
        _spec("forecast_age_hours", "gauge", "Age of the freshest forecast inputs", "model"),
        _spec("model_drift_alarm", "gauge", "1 while a drift alarm is active", "model"),
        _spec("model_calibration_80", "gauge", "80% interval coverage", "model"),
        # decisions / business
        _spec("pricing_decisions_total", "counter", "Pricing decisions", "status"),
        _spec("pricing_frozen_decisions_total", "counter", "Frozen decisions", "reason"),
        _spec("pricing_decision_duration_ms", "histogram", "Pricing cycle latency"),
        _spec("price_change_fraction", "gauge", "Share of products whose price changed"),
        _spec("business_kpi_deviation", "gauge", "Relative deviation from baseline", "kpi"),
        # AI operator (Phase 12): tool is a declared tool name or "undeclared" (bounded)
        _spec("agent_tool_calls_total", "counter", "Agent tool calls", "tool", "tier", "status"),
        _spec("agent_investigations_total", "counter", "Agent runs", "provider", "outcome"),
    ]
)


class _RingExporter(SpanExporter):
    """Keeps the most recent spans; bounded so a long run cannot grow without limit."""

    def __init__(self, capacity: int) -> None:
        self._spans: deque[ReadableSpan] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def snapshot(self) -> list[ReadableSpan]:
        with self._lock:
            return list(self._spans)


@dataclass(frozen=True)
class Point:
    """One time series of one metric. Histograms carry cumulative bucket counts."""

    name: str
    labels: dict[str, str]
    value: float  # counter sum, gauge value, or histogram sum
    count: int = 0
    bounds: tuple[float, ...] = ()
    buckets: tuple[int, ...] = ()


def _matches(point: Point, match: dict[str, str]) -> bool:
    return all(point.labels.get(k) == v for k, v in match.items())


@dataclass(frozen=True)
class MetricsView:
    """Immutable read side used by SLO and alert evaluators."""

    points: tuple[Point, ...] = ()

    def select(self, name: str, **match: str) -> list[Point]:
        return [p for p in self.points if p.name == name and _matches(p, match)]

    def total(self, name: str, **match: str) -> float:
        return sum(p.value for p in self.select(name, **match))

    def count(self, name: str, **match: str) -> int:
        return sum(p.count for p in self.select(name, **match))

    def peak(self, name: str, **match: str) -> float | None:
        values = [p.value for p in self.select(name, **match)]
        return max(values) if values else None

    def trough(self, name: str, **match: str) -> float | None:
        values = [p.value for p in self.select(name, **match)]
        return min(values) if values else None

    def quantile(self, name: str, q: float, **match: str) -> float | None:
        """Upper bound of the bucket holding the q-quantile (conservative); None if empty.

        Observations above the last bound report ``inf``.
        """
        pts = [p for p in self.select(name, **match) if p.bounds]
        total = sum(p.count for p in pts)
        if total == 0:
            return None
        bounds = pts[0].bounds
        merged = [sum(p.buckets[i] for p in pts) for i in range(len(bounds) + 1)]
        target, seen = q * total, 0
        for i, n in enumerate(merged):
            seen += n
            if seen >= target:
                return bounds[i] if i < len(bounds) else float("inf")
        return float("inf")


class Telemetry:
    def __init__(self, service: str = "praxis", span_capacity: int = 20_000) -> None:
        self.service = service
        self._reader = InMemoryMetricReader()
        self._meter = MeterProvider(metric_readers=[self._reader]).get_meter("praxis")
        self._exporter = _RingExporter(span_capacity)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._tracer = provider.get_tracer("praxis")
        self._instruments: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _instrument(self, spec: MetricSpec) -> Any:
        with self._lock:
            inst = self._instruments.get(spec.name)
            if inst is None:
                if spec.kind == "counter":
                    inst = self._meter.create_counter(spec.name, description=spec.description)
                elif spec.kind == "gauge":
                    inst = self._meter.create_gauge(spec.name, description=spec.description)
                else:
                    inst = self._meter.create_histogram(
                        spec.name,
                        unit="ms",
                        description=spec.description,
                        explicit_bucket_boundaries_advisory=list(LATENCY_BOUNDS_MS),
                    )
                self._instruments[spec.name] = inst
            return inst

    @staticmethod
    def _checked(name: str, kind: Kind, labels: dict[str, Any]) -> MetricSpec:
        spec = METRICS.get(name)
        if spec is None:
            raise KeyError(f"unknown metric {name!r}")
        if spec.kind != kind:
            raise TypeError(f"metric {name!r} is a {spec.kind}, not a {kind}")
        if set(labels) != set(spec.labels):
            raise ValueError(f"metric {name!r} takes labels {spec.labels}, got {tuple(labels)}")
        return spec

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        spec = self._checked(name, "counter", labels)
        self._instrument(spec).add(value, {k: str(v) for k, v in labels.items()})

    def observe(self, name: str, value_ms: float, **labels: Any) -> None:
        spec = self._checked(name, "histogram", labels)
        self._instrument(spec).record(value_ms, {k: str(v) for k, v in labels.items()})

    def set(self, name: str, value: float, **labels: Any) -> None:
        spec = self._checked(name, "gauge", labels)
        self._instrument(spec).set(value, {k: str(v) for k, v in labels.items()})

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[trace.Span]:
        """Start a span in the Praxis trace (see module docstring); records the error type."""
        with self._tracer.start_as_current_span(
            name,
            context=_praxis_parent(),
            attributes={k: _attr(v) for k, v in attributes.items() if v is not None},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    def finished_spans(self) -> list[ReadableSpan]:
        return self._exporter.snapshot()

    def snapshot(self) -> MetricsView:
        data = self._reader.get_metrics_data()
        points: list[Point] = []
        for rm in data.resource_metrics if data else ():
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    points.extend(_points(metric.name, metric.data))
        return MetricsView(tuple(points))


def _points(name: str, data: Any) -> Iterator[Point]:
    for dp in data.data_points:
        labels = {str(k): str(v) for k, v in (dp.attributes or {}).items()}
        if isinstance(data, Histogram):
            yield Point(
                name, labels, float(dp.sum), int(dp.count),
                tuple(dp.explicit_bounds), tuple(dp.bucket_counts),
            )  # fmt: skip
        elif isinstance(data, Sum | Gauge):
            yield Point(name, labels, float(dp.value))


def _attr(value: Any) -> str | bool | int | float:
    return value if isinstance(value, str | bool | int | float) else str(value)


def _praxis_parent() -> Context | None:
    """Parent context carrying the Praxis trace id, unless the current span already has it."""
    tid = current_trace_id()
    if tid is None:
        return None
    wanted = int(tid, 16)
    current = trace.get_current_span().get_span_context()
    if current.is_valid and current.trace_id == wanted:
        return None
    remote = SpanContext(
        trace_id=wanted,
        span_id=secrets.randbits(64) or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(remote))


_default = Telemetry()


def telemetry() -> Telemetry:
    return _default


@contextmanager
def use_telemetry(instance: Telemetry | None = None) -> Iterator[Telemetry]:
    """Swap the process-wide instance (tests, drills); restores the previous one on exit."""
    global _default
    previous, _default = _default, instance or Telemetry()
    try:
        yield _default
    finally:
        _default = previous


def trace_id_hex(span: ReadableSpan) -> str:
    ctx = span.get_span_context()
    return format(ctx.trace_id if ctx else 0, "032x")
