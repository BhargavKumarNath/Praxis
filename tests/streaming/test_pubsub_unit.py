"""Pub/Sub adapter against fake clients (no emulator): mapping and error handling."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import Future
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from praxis.errors import TransientError
from praxis.streaming.pubsub import PubSubPublisher, PubSubPuller, run_pull_loop
from praxis.streaming.transport import ConsumerCrashed, Delivery, Disposition


class FakePublisherClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, path: str, data: bytes, **attrs: str) -> Future[str]:
        future: Future[str] = Future()
        if self.fail:
            future.set_exception(gexc.ServiceUnavailable("down"))  # type: ignore[no-untyped-call]
        else:
            self.sent.append((path, data, attrs))
            future.set_result(str(len(self.sent)))
        return future


def test_publisher_waits_for_every_future() -> None:
    client = FakePublisherClient()
    pub = PubSubPublisher("p", client)
    assert pub.publish_batch("t", [(b"a", {"k": "v"}), (b"b", {})]) == ["1", "2"]
    assert pub.publish("t", b"c", {}) == "3"
    assert client.sent[0] == ("projects/p/topics/t", b"a", {"k": "v"})


def test_publish_failure_is_transient() -> None:
    with pytest.raises(TransientError):
        PubSubPublisher("p", FakePublisherClient(fail=True)).publish("t", b"a", {})


class FakeSubscriberClient:
    def __init__(self, batches: list[list[Any]] | None = None, *, deadline: bool = False) -> None:
        self.batches = batches or []
        self.deadline = deadline
        self.acked: list[str] = []
        self.nacked: list[str] = []
        self.closed = False

    def subscription_path(self, project: str, sub: str) -> str:
        return f"projects/{project}/subscriptions/{sub}"

    def pull(self, request: dict[str, Any], timeout: float) -> Any:
        if self.deadline:
            raise gexc.DeadlineExceeded("idle")  # type: ignore[no-untyped-call]
        return SimpleNamespace(received_messages=self.batches.pop(0) if self.batches else [])

    def acknowledge(self, request: dict[str, Any]) -> None:
        self.acked.extend(request["ack_ids"])

    def modify_ack_deadline(self, request: dict[str, Any]) -> None:
        assert request["ack_deadline_seconds"] == 0
        self.nacked.extend(request["ack_ids"])

    def close(self) -> None:
        self.closed = True


def _rm(n: int, data: bytes = b"x", attempt: int = 0, publish_time: Any = None) -> Any:
    msg = SimpleNamespace(
        message_id=f"m{n}",
        data=data,
        attributes={"a": "b"},
        publish_time=publish_time or datetime(2026, 1, 1, tzinfo=UTC),
    )
    return SimpleNamespace(ack_id=f"ack{n}", message=msg, delivery_attempt=attempt)


def test_puller_maps_messages_and_ack_nack() -> None:
    client = FakeSubscriberClient([[_rm(1, attempt=3), _rm(2, publish_time="not-a-datetime")]])
    puller = PubSubPuller("p", "sub", client)
    first, second = puller.pull()
    assert (first.subscription, first.message_id, first.ack_id) == ("sub", "m1", "ack1")
    assert first.delivery_attempt == 3 and second.delivery_attempt == 1  # 0 = no DLQ policy
    assert first.attributes == {"a": "b"} and first.publish_time.tzinfo is not None
    assert second.publish_time.tzinfo is not None
    puller.ack(["ack1"])
    puller.nack(["ack2"])
    puller.ack([])
    puller.nack([])
    assert (client.acked, client.nacked) == (["ack1"], ["ack2"])
    puller.close()
    assert client.closed


def test_pull_deadline_means_empty() -> None:
    assert PubSubPuller("p", "s", FakeSubscriberClient(deadline=True)).pull() == []


class ScriptedWorker:
    def __init__(self, batches: bool, crash_on: str | None = None) -> None:
        self.batches = batches
        self.crash_on = crash_on

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        if any(d.message_id == self.crash_on for d in deliveries):
            raise ConsumerCrashed("boom")
        return [Disposition.ACK if d.message_id != "m2" else Disposition.NACK for d in deliveries]


@pytest.mark.parametrize("batches", [True, False])
def test_pull_loop_acks_nacks_and_stops_when_idle(batches: bool) -> None:
    client = FakeSubscriberClient([[_rm(1), _rm(2)], [_rm(3)]])
    report = run_pull_loop(
        PubSubPuller("p", "s", client), ScriptedWorker(batches), idle_timeout_s=0.0
    )
    assert (report.deliveries, report.acks, report.nacks) == (3, 2, 1)
    assert client.acked == ["ack1", "ack3"] and client.nacked == ["ack2"]


def test_pull_loop_stops_on_crash_and_respects_cap() -> None:
    client = FakeSubscriberClient([[_rm(1), _rm(4)]])
    report = run_pull_loop(PubSubPuller("p", "s", client), ScriptedWorker(False, crash_on="m4"))
    assert report.crashed and client.acked == ["ack1"]  # acked before the crash
    capped = run_pull_loop(
        PubSubPuller("p", "s", FakeSubscriberClient([[_rm(1)], [_rm(3)]])),
        ScriptedWorker(True),
        max_deliveries=1,
    )
    assert capped.deliveries == 1
