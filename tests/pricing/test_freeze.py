"""Price freezes (Phase 12): named, non-self approval; execution refused while frozen."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from praxis.pricing.config import Mode
from praxis.pricing.freeze import (
    FreezeError,
    FreezeStore,
    MemoryFreezeStore,
    PostgresFreezeStore,
)
from praxis.pricing.store import MemoryDecisionStore, NotExecutable
from tests.pricing.test_store import change

T0 = datetime(2026, 10, 25, 12, tzinfo=UTC)
pytestmark = pytest.mark.integration


@pytest.fixture(params=["memory", "postgres"])
def freezes(request: pytest.FixtureRequest) -> Iterator[FreezeStore]:
    if request.param == "memory":
        yield MemoryFreezeStore()
    else:
        engine: Engine = request.getfixturevalue("pg_engine")
        yield PostgresFreezeStore(engine)


def test_freeze_lifecycle(freezes: FreezeStore) -> None:
    f = freezes.activate("gpu_minutes", "volatility", requested_by="agent", approved_by="owner",
                         now=T0)  # fmt: skip
    assert f.active and f.covers("gpu_minutes") and not f.covers("cpu_minutes")
    assert freezes.is_frozen("gpu_minutes") and not freezes.is_frozen("cpu_minutes")
    with pytest.raises(FreezeError, match="already frozen"):
        freezes.activate("gpu_minutes", "again", requested_by="agent", approved_by="owner")
    star = freezes.activate("*", "all", requested_by="agent", approved_by="owner",
                            now=T0 + timedelta(seconds=1))  # fmt: skip
    assert freezes.is_frozen("cpu_minutes") and star.as_dict()["active"] is True
    assert [x.scope for x in freezes.active()] == ["gpu_minutes", "*"]
    lifted = freezes.lift(star.freeze_id, by="owner", now=T0 + timedelta(hours=1))
    assert not lifted.active and lifted.lifted_by == "owner"
    assert lifted.as_dict()["lifted_at"] is not None
    assert not freezes.is_frozen("cpu_minutes") and freezes.is_frozen("gpu_minutes")
    with pytest.raises(FreezeError, match="no active freeze"):
        freezes.lift(star.freeze_id, by="owner")


@pytest.mark.parametrize(
    ("scope", "requested_by", "approved_by", "match"),
    [(" ", "agent", "owner", "scope"), ("*", "agent", "", "named"),
     ("*", "", "owner", "named"), ("*", "owner", "owner", "its requester")],
)  # fmt: skip
def test_freeze_needs_a_named_independent_approver(
    freezes: FreezeStore, scope: str, requested_by: str, approved_by: str, match: str
) -> None:
    with pytest.raises(FreezeError, match=match):
        freezes.activate(scope, "x", requested_by=requested_by, approved_by=approved_by)
    with pytest.raises(FreezeError, match="named operator"):
        freezes.lift("frz_x", by=" ")


def test_memory_decision_store_refuses_a_frozen_product() -> None:
    freezes = MemoryFreezeStore()
    store = MemoryDecisionStore(freezes)
    d = change(Mode.EXECUTE)
    store.record(d)
    f = freezes.activate(d.product, "x", requested_by="agent", approved_by="owner")
    with pytest.raises(NotExecutable, match="frozen"):
        store.execute(d.decision_id, "pricing-service")
    freezes.lift(f.freeze_id, by="owner")
    assert store.execute(d.decision_id, "pricing-service").product == d.product
