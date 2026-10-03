from __future__ import annotations

import pytest

from praxis.tracing import (
    current_correlation_id,
    current_trace_id,
    is_valid_correlation_id,
    is_valid_trace_id,
    new_correlation_id,
    new_trace_id,
    trace_context,
)


def test_generated_ids_are_valid() -> None:
    assert is_valid_trace_id(new_trace_id())
    assert is_valid_correlation_id(new_correlation_id())


@pytest.mark.parametrize("bad", ["", "0" * 32, "A" * 32, "abc", "g" * 32])
def test_invalid_trace_ids(bad: str) -> None:
    assert not is_valid_trace_id(bad)


@pytest.mark.parametrize("bad", ["", "nope", "0B1A2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"])
def test_invalid_correlation_ids(bad: str) -> None:
    assert not is_valid_correlation_id(bad)


def test_context_binds_and_restores() -> None:
    assert current_trace_id() is None
    with trace_context() as (tid, cid):
        assert current_trace_id() == tid
        assert current_correlation_id() == cid
        with trace_context(trace_id="a" * 32, correlation_id=cid) as (inner_tid, inner_cid):
            assert inner_tid == "a" * 32
            assert inner_cid == cid
        assert current_trace_id() == tid
    assert current_trace_id() is None
    assert current_correlation_id() is None


def test_context_rejects_malformed_inbound_ids() -> None:
    with pytest.raises(ValueError, match="trace_id"), trace_context(trace_id="bad"):
        pass
    with pytest.raises(ValueError, match="correlation_id"), trace_context(correlation_id="bad"):
        pass
    assert current_trace_id() is None
