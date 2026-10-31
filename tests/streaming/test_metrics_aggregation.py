"""StreamMetrics aggregates counter increments and flushes them to OpenTelemetry (ADR 0022).

Invariant: any ``Telemetry.snapshot`` sees exactly the increments made so far, whether or
not the local buffer reached ``FLUSH_EVERY``, and no increment is counted twice.
"""

from __future__ import annotations

import gc

from praxis.streaming import metrics as metrics_mod
from praxis.streaming.metrics import FLUSH_EVERY, StreamMetrics
from praxis.telemetry import Telemetry, use_telemetry


def _total(t: Telemetry, outcome: str = "acked") -> float:
    return t.snapshot().total("events_consumed_total", subscription="s", outcome=outcome)


def test_snapshot_sees_unflushed_increments_exactly_once() -> None:
    with use_telemetry() as t:
        m = StreamMetrics()
        m.inc("s", "acked")
        m.inc("s", "acked", 4)
        assert _total(t) == 5  # below FLUSH_EVERY: flushed by the snapshot itself
        assert _total(t) == 5  # a second snapshot does not double count
        m.inc("s", "acked")
        assert _total(t) == 6
        assert m.count("s", "acked") == 6


def test_buffer_flushes_in_bulk_at_threshold() -> None:
    with use_telemetry() as t:
        m = StreamMetrics()
        for _ in range(FLUSH_EVERY):
            m.inc("s", "acked")
        assert m._pending_ops == 0  # flushed by the threshold, not by a reader
        m.inc("s", "nacked")
        assert _total(t) == FLUSH_EVERY
        assert _total(t, "nacked") == 1


def test_swapping_the_telemetry_instance_flushes_to_the_old_one_first() -> None:
    with use_telemetry() as first:
        m = StreamMetrics()
        m.inc("s", "acked", 2)
        with use_telemetry() as second:
            m.inc("s", "acked", 3)
            assert _total(second) == 3
        assert _total(first) == 2


def test_collected_metrics_flush_their_buffer_and_leave_the_flush_list() -> None:
    with use_telemetry() as t:
        m = StreamMetrics()
        m.inc("s", "acked")
        del m
        gc.collect()
        assert _total(t) == 1  # buffered counts survive the instance
        assert t._pre_collect == []


def test_flush_every_is_bounded() -> None:
    # Large buffers delay readers that bypass snapshot; keep the batch small.
    assert 1 <= metrics_mod.FLUSH_EVERY <= 10_000
