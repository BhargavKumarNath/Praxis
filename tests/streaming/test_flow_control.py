"""Lease window and pull grouping (ADR 0025): what a consumer crash can strand."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from praxis.streaming.transport import (
    ISOLATE_FROM_ATTEMPT,
    Delivery,
    Disposition,
    FlowControl,
    process_pull,
)


def _d(n: int, attempt: int = 1) -> Delivery:
    return Delivery("s", f"m{n}", f"a{n}", b"", {}, datetime(2026, 1, 1, tzinfo=UTC), attempt)


def test_slow_start_doubles_to_the_cap_and_only_crash_evidence_shrinks_it() -> None:
    flow = FlowControl(10)
    seen = []
    for _ in range(5):
        seen.append(flow.limit)
        flow.completed([_d(1)])
    assert seen == [1, 2, 4, 8, 10]
    flow.completed([_d(1), _d(2, attempt=2)])  # a first redelivery (crash or nack) is weak
    assert flow.limit == 10
    flow.completed([_d(1), _d(2, attempt=ISOLATE_FROM_ATTEMPT)])  # redelivered twice
    assert flow.limit == 1
    flow.completed([_d(3)])
    flow.restart()
    assert flow.limit == 1
    with pytest.raises(ValueError, match="max_messages"):
        FlowControl(0)
    with pytest.raises(ValueError, match="isolate_from"):
        FlowControl(5, isolate_from=1)


def test_fresh_first_then_first_redeliveries_then_repeated_redeliveries_alone() -> None:
    flow = FlowControl(10)
    pull = [_d(1, 4), _d(2), _d(3, 2), _d(4), _d(5, 3), _d(6, 2)]
    assert flow.groups(pull, batches=True) == [
        [_d(2), _d(4)], [_d(3, 2), _d(6, 2)], [_d(5, 3)], [_d(1, 4)]]  # fmt: skip
    assert flow.groups(pull, batches=False) == [
        [_d(2)], [_d(4)], [_d(3, 2)], [_d(6, 2)], [_d(5, 3)], [_d(1, 4)]]  # fmt: skip
    assert flow.groups([], batches=True) == []


@given(st.lists(st.integers(min_value=1, max_value=6), max_size=30), st.booleans())
def test_groups_partition_the_pull_and_isolate_repeated_redeliveries(
    attempts: list[int], batches: bool
) -> None:
    flow = FlowControl(100)
    pull = [_d(i, a) for i, a in enumerate(attempts)]
    groups = flow.groups(pull, batches)
    flat = [d for g in groups for d in g]
    assert sorted(flat, key=lambda d: d.message_id) == sorted(pull, key=lambda d: d.message_id)
    for g in groups:
        tier = {min(d.delivery_attempt, ISOLATE_FROM_ATTEMPT) for d in g}
        assert len(tier) == 1  # a group never mixes evidence levels
        if tier == {ISOLATE_FROM_ATTEMPT}:
            assert len(g) == 1
    order = [max(d.delivery_attempt for d in g) for g in groups]
    assert order == sorted(order)  # the most suspicious run last


class _Worker:
    batches = True

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        return [Disposition.NACK if d.message_id == "m2" else Disposition.ACK for d in deliveries]


def test_process_pull_acks_each_group_as_it_completes_then_sizes_the_next_pull() -> None:
    calls: list[tuple[str, list[str]]] = []
    tally = SimpleNamespace(acks=0, nacks=0)
    flow = FlowControl(8)
    process_pull(
        _Worker(), flow, [_d(1), _d(2, 3), _d(3)],
        lambda ids: calls.append(("ack", ids)), lambda ids: calls.append(("nack", ids)), tally,
    )  # fmt: skip
    assert (tally.acks, tally.nacks) == (2, 1)
    assert calls == [("ack", ["a1", "a3"]), ("nack", []), ("ack", []), ("nack", ["a2"])]
    assert flow.limit == 1  # the pull held a repeatedly redelivered message
