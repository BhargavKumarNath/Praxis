"""Cross-process metric snapshots (ADR 0020): publish, merge, staleness, publisher safety."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, exc, text

from praxis.control.db import make_engine
from praxis.control.metric_snapshots import (
    MetricSnapshotStore,
    SnapshotPublisher,
    encode,
    merge,
)
from praxis.errors import TransientError
from praxis.telemetry import MetricsView, Point, Telemetry, use_telemetry

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    eng = make_engine(pg_url)
    yield eng
    eng.dispose()


@pytest.fixture
def snapshots(engine: Engine) -> MetricSnapshotStore:
    return MetricSnapshotStore(engine)


def _view(build: Any) -> MetricsView:
    tel = Telemetry()
    build(tel)
    return tel.snapshot()


def _publish(store: MetricSnapshotStore, component: str, instance: str, view: MetricsView,
             at: datetime, ttl: timedelta = 5 * MIN) -> None:  # fmt: skip
    store.publish(component, instance, view, now=at, ttl=ttl, started_at=at - MIN)


def test_counters_and_histograms_add_up_and_the_newest_gauge_wins(
    snapshots: MetricSnapshotStore,
) -> None:
    def a(t: Telemetry) -> None:
        t.inc("events_consumed_total", 3, subscription="ops", outcome="applied")
        t.observe("event_lag_ms", 7, subscription="ops")
        t.set("queue_backlog", 10, subscription="ops")

    def b(t: Telemetry) -> None:
        t.inc("events_consumed_total", 4, subscription="ops", outcome="applied")
        for _ in range(9):
            t.observe("event_lag_ms", 400, subscription="ops")
        t.set("queue_backlog", 2500, subscription="ops")

    _publish(snapshots, "consumer", "host:1", _view(a), T0)
    _publish(snapshots, "consumer", "host:2", _view(b), T0 + MIN)
    fleet = snapshots.read(T0 + 2 * MIN)
    view = fleet.view
    assert view.total("events_consumed_total") == 7
    assert view.count("event_lag_ms") == 10
    assert view.quantile("event_lag_ms", 0.95) == 500.0  # merged buckets, not one instance
    assert [p.value for p in view.select("queue_backlog")] == [2500.0]
    assert [(s.instance, s.stale) for s in fleet.sources] == [("host:1", False), ("host:2", False)]


def test_expired_rows_are_reported_stale_and_never_merged(snapshots: MetricSnapshotStore) -> None:
    _publish(snapshots, "pricing", "job:1",
             _view(lambda t: t.set("price_change_fraction", 0.9)), T0, ttl=MIN)  # fmt: skip
    _publish(snapshots, "api", "web:1",
             _view(lambda t: t.inc("http_requests_total", route="/x", method="GET", status=200)),
             T0, ttl=10 * MIN)  # fmt: skip
    fleet = snapshots.read(T0 + 2 * MIN)
    assert fleet.view.select("price_change_fraction") == []
    assert fleet.view.total("http_requests_total") == 1
    (stale,) = fleet.stale
    assert (stale.component, stale.age_s, stale.points) == ("pricing", 120.0, 1)
    assert stale.as_dict()["stale"] is True


def test_expiry_is_exclusive_at_the_boundary(snapshots: MetricSnapshotStore) -> None:
    _publish(snapshots, "api", "web:1", _view(lambda t: t.set("price_change_fraction", 1)),
             T0, ttl=MIN)  # fmt: skip
    assert not snapshots.read(T0 + MIN - timedelta(seconds=1)).stale
    assert snapshots.read(T0 + MIN).stale  # expires_at <= now is stale


def test_republish_overwrites_one_row_per_instance(
    snapshots: MetricSnapshotStore, engine: Engine
) -> None:
    for i in range(3):
        _publish(snapshots, "api", "web:1",
                 _view(lambda t, i=i: t.set("price_change_fraction", i)), T0 + i * MIN)  # fmt: skip
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ops_metric_snapshots")).scalar() == 1
    assert [p.value for p in snapshots.read(T0 + 3 * MIN).view.points] == [2.0]


def test_local_view_replaces_the_readers_own_published_copy(
    snapshots: MetricSnapshotStore,
) -> None:
    _publish(snapshots, "ops-monitor", "me:1",
             _view(lambda t: t.inc("http_requests_total", route="/x", method="GET", status=200)),
             T0)  # fmt: skip
    local = _view(lambda t: t.inc("http_requests_total", 5, route="/x", method="GET", status=200))
    fleet = snapshots.read(T0 + MIN, local=local, exclude=("ops-monitor", "me:1"))
    assert fleet.view.total("http_requests_total") == 5  # not 6: no double count
    assert fleet.sources == ()


def test_unknown_metrics_and_labels_are_dropped_and_counted(
    snapshots: MetricSnapshotStore, engine: Engine
) -> None:
    hostile = [
        {"name": "not_a_metric", "labels": {}, "value": 1},
        {"name": "queue_backlog", "labels": {"subscription": "a", "extra": "x"}, "value": 1},
        {"name": "queue_backlog", "labels": {"subscription": "a"}, "value": 4},
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ops_metric_snapshots VALUES ('consumer', 'h:1', :t, :e, :t, "
                "CAST(:p AS JSONB))"
            ),
            {"t": T0, "e": T0 + MIN, "p": json.dumps(hostile)},
        )
    fleet = snapshots.read(T0)
    assert [p.value for p in fleet.view.points] == [4.0]
    assert (fleet.sources[0].points, fleet.sources[0].rejected) == (1, 2)


def test_database_refuses_bad_rows(snapshots: MetricSnapshotStore) -> None:
    with pytest.raises(exc.IntegrityError):
        _publish(snapshots, "api", "web:1", MetricsView(), T0, ttl=timedelta(0))
    with pytest.raises(exc.IntegrityError):
        _publish(snapshots, "Bad Name; DROP", "web:1", MetricsView(), T0)


def test_prune_removes_only_long_expired_rows(snapshots: MetricSnapshotStore) -> None:
    _publish(snapshots, "api", "old:1", MetricsView(), T0, ttl=MIN)
    _publish(snapshots, "api", "new:1", MetricsView(), T0 + 10 * MIN, ttl=MIN)
    assert snapshots.prune(T0 + 5 * MIN) == 1
    assert [s.instance for s in snapshots.read(T0 + 10 * MIN).sources] == ["new:1"]


def test_merge_keeps_the_newest_gauge_regardless_of_order() -> None:
    old = Point("queue_backlog", {"subscription": "a"}, 1.0)
    new = Point("queue_backlog", {"subscription": "a"}, 9.0)
    assert merge([(T0 + MIN, [new]), (T0, [old])]).points == (new,)
    assert encode(MetricsView((new,)))[0]["labels"] == {"subscription": "a"}


def test_publisher_throttles_and_publishes_the_process_metrics(
    snapshots: MetricSnapshotStore,
) -> None:
    clock = [T0]
    pub = SnapshotPublisher(snapshots, "consumer", interval=MIN, instance="h:1",
                            clock=lambda: clock[0])  # fmt: skip
    with use_telemetry() as tel:
        tel.set("queue_backlog", 3, subscription="a")
        assert pub.maybe_publish() is True
        tel.set("queue_backlog", 8, subscription="a")
        clock[0] = T0 + timedelta(seconds=30)
        assert pub.maybe_publish() is False  # within the interval
        assert [p.value for p in snapshots.read(clock[0]).view.points] == [3.0]
        clock[0] = T0 + MIN
        assert pub.maybe_publish() is True
    (src,) = snapshots.read(T0 + MIN).sources
    assert (src.expires_at - src.published_at, src.started_at) == (4 * MIN, T0)


class _DownStore(MetricSnapshotStore):
    def __init__(self) -> None:
        self.calls = 0

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise TransientError("database unavailable: OperationalError")


def test_publisher_survives_an_outage_and_backs_off() -> None:
    clock = [T0]
    down = _DownStore()
    pub = SnapshotPublisher(down, "consumer", interval=MIN, clock=lambda: clock[0])
    assert pub.maybe_publish() is False
    clock[0] = T0 + timedelta(seconds=10)
    assert pub.maybe_publish() is False
    assert (down.calls, pub.failures) == (1, 1)  # no retry storm inside the interval
    clock[0] = T0 + MIN
    pub.maybe_publish()
    assert (down.calls, pub.failures) == (2, 2)
