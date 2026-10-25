"""Guarded request stores (memory and Postgres share one semantic suite) and the desk."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from sqlalchemy import Engine, exc

from praxis.agent.requests import (
    ActionRequest,
    MemoryRequestStore,
    PostgresRequestStore,
    RequestError,
    RequestStore,
    request_id,
)
from tests.agent.conftest import NOW


def req(kind: str = "freeze_pricing", target: str = "*") -> ActionRequest:
    return ActionRequest(
        request_id(kind, target, NOW), kind, target, {}, "why", ["call_x"],  # type: ignore[arg-type]
        "praxis-agent", "ses_000000000000", NOW,
    )  # fmt: skip


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[RequestStore]:
    if request.param == "memory":
        yield MemoryRequestStore()
    else:
        engine: Engine = request.getfixturevalue("pg_engine")
        yield PostgresRequestStore(engine)


def test_one_pending_request_per_kind_and_target(store: RequestStore) -> None:
    first, created = store.create(req())
    later = replace(req(), request_id="req_later", created_at=NOW.replace(hour=13))
    joined, created2 = store.create(later)
    assert created and not created2 and joined.request_id == first.request_id
    other, created3 = store.create(req(target="gpu_minutes"))
    assert created3 and other.target == "gpu_minutes"
    assert store.get("req_missing") is None
    assert {r.request_id for r in store.recent()} == {first.request_id, other.request_id}


def test_claim_rules(store: RequestStore) -> None:
    made, _ = store.create(req())
    with pytest.raises(RequestError, match="unknown request"):
        store.claim("req_missing", "owner", NOW)
    with pytest.raises(RequestError, match="named"):
        store.claim(made.request_id, " ", NOW)
    with pytest.raises(RequestError, match="its requester"):
        store.claim(made.request_id, "praxis-agent", NOW)
    claimed = store.claim(made.request_id, "owner", NOW)
    assert claimed.status == "executing" and claimed.decided_by == "owner"
    with pytest.raises(RequestError, match="not pending"):
        store.claim(made.request_id, "owner", NOW)
    done = store.finish(made.request_id, "completed", {"ok": True}, by="owner", now=NOW)
    assert done.status == "completed" and done.outcome == {"ok": True}
    assert done.as_dict()["decided_at"] == NOW.isoformat()
    # a decided request no longer blocks a new one for the same target
    _, created = store.create(replace(req(), request_id="req_new"))
    assert created


def test_postgres_duplicate_id_of_a_decided_request_is_an_error(pg_engine: Engine) -> None:
    store = PostgresRequestStore(pg_engine)
    made, _ = store.create(req())
    store.claim(made.request_id, "owner", NOW)
    with pytest.raises(exc.IntegrityError):
        store.create(req())  # same primary key, nothing pending to join
