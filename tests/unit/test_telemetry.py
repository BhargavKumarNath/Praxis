"""Telemetry: declared metrics only, bounded labels, conservative quantiles, Praxis trace ids."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from praxis.telemetry import METRICS, MetricsView, Point, Telemetry, trace_id_hex, use_telemetry
from praxis.tracing import trace_context


def test_counter_histogram_gauge_round_trip(tel: Telemetry) -> None:
    tel.inc("http_requests_total", route="/x", method="GET", status="200")
    tel.inc("http_requests_total", 2, route="/x", method="GET", status="200")
    tel.observe("http_request_duration_ms", 7.0, route="/x")
    tel.observe("http_request_duration_ms", 400.0, route="/x")
    tel.set("queue_backlog", 5, subscription="s")
    tel.set("queue_backlog", 3, subscription="s")
    view = tel.snapshot()
    assert view.total("http_requests_total", route="/x") == 3
    assert view.count("http_request_duration_ms") == 2
    assert view.peak("queue_backlog", subscription="s") == 3  # a gauge keeps the last value
    assert view.quantile("http_request_duration_ms", 0.5, route="/x") == 10.0
    assert view.quantile("http_request_duration_ms", 0.99, route="/x") == 500.0


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda t: t.inc("nope"), KeyError),
        (lambda t: t.set("http_requests_total", 1), TypeError),
        (lambda t: t.inc("http_requests_total", route="/x"), ValueError),
        (lambda t: t.inc("queue_backlog", subscription="s"), TypeError),
    ],
)
def test_undeclared_use_is_rejected(
    call: Callable[[Telemetry], None], error: type[Exception], tel: Telemetry
) -> None:
    with pytest.raises(error):
        call(tel)


def test_every_catalogue_entry_can_be_emitted(tel: Telemetry) -> None:
    for spec in METRICS.values():
        labels = dict.fromkeys(spec.labels, "v")
        emit = getattr(tel, {"counter": "inc", "histogram": "observe", "gauge": "set"}[spec.kind])
        emit(spec.name, 1, **labels)
    assert {p.name for p in tel.snapshot().points} == set(METRICS)


def test_quantile_empty_overflow_and_selectors() -> None:
    assert MetricsView().quantile("x", 0.95) is None
    assert MetricsView().peak("x") is None
    assert MetricsView().trough("x") is None
    bounds = (10.0, 20.0)
    over = Point("h", {}, 1.0, 1, bounds, (0, 0, 1))
    assert MetricsView((over,)).quantile("h", 0.5) == float("inf")


def test_spans_share_the_praxis_trace_and_nest(tel: Telemetry) -> None:
    with trace_context() as (tid, _cid):
        with tel.span("outer", stage="a"), tel.span("inner"):
            pass
        with tel.span("sibling"):
            pass
    spans = {s.name: s for s in tel.finished_spans()}
    assert {trace_id_hex(s) for s in spans.values()} == {tid}
    parent = spans["inner"].parent
    assert parent is not None
    outer = spans["outer"].get_span_context()
    assert outer is not None
    assert parent.span_id == outer.span_id
    assert spans["outer"].attributes == {"stage": "a"}


def test_span_records_error_type_never_the_message(tel: Telemetry) -> None:
    with pytest.raises(RuntimeError), tel.span("boom"):
        raise RuntimeError("secret-token-123")
    (span,) = tel.finished_spans()
    assert span.status.description == "RuntimeError"
    assert "secret-token-123" not in repr(span.events) + repr(span.status)


def test_span_without_praxis_trace_gets_its_own(tel: Telemetry) -> None:
    with tel.span("lonely"):
        pass
    assert len(trace_id_hex(tel.finished_spans()[0])) == 32


def test_use_telemetry_restores_previous(tel: Telemetry) -> None:
    from praxis.telemetry import telemetry

    with use_telemetry() as other:
        assert telemetry() is other
    assert telemetry() is tel


def test_increase_subtracts_cumulative_series_and_keeps_gauges() -> None:
    bounds = (10.0, 20.0)
    old = MetricsView((
        Point("http_requests_total", {"route": "/a", "method": "GET", "status": "200"}, 5.0),
        Point("http_requests_total", {"route": "/a", "method": "GET", "status": "200"}, 3.0),
        Point("event_lag_ms", {"subscription": "s"}, 30.0, 2, bounds, (1, 1, 0)),
        Point("queue_backlog", {"subscription": "s"}, 7.0),
    ))  # fmt: skip
    new = MetricsView((
        Point("http_requests_total", {"route": "/a", "method": "GET", "status": "200"}, 12.0),
        Point("event_lag_ms", {"subscription": "s"}, 70.0, 4, bounds, (2, 1, 1)),
        Point("queue_backlog", {"subscription": "s"}, 2.0),
        Point("http_server_errors_total", {"route": "/a"}, 1.0),
    ))  # fmt: skip
    inc = new.increase(old)
    assert inc.total("http_requests_total") == 4.0  # two instances summed: 12 - 8
    assert inc.total("http_server_errors_total") == 1.0  # new series: all of it is new
    (lag,) = inc.select("event_lag_ms")
    assert (lag.value, lag.count, lag.buckets) == (40.0, 2, (1, 0, 1))
    assert inc.peak("queue_backlog") == 2.0  # gauges are current state


def test_increase_treats_a_drop_as_a_reset() -> None:
    labels = {"route": "/a", "method": "GET", "status": "200"}
    before = MetricsView((Point("http_requests_total", labels, 50.0),))
    after = MetricsView((Point("http_requests_total", labels, 4.0),))  # process restarted
    assert after.increase(before).total("http_requests_total") == 4.0
