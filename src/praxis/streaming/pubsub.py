"""Google Pub/Sub transport (emulator for development and CI, GCP via Terraform).

* ``PubSubPublisher``: blocking publish with bounded timeouts; ``publish_batch`` fans
  out futures and waits for all of them, so a returned batch is durable.
* ``PubSubPuller``: synchronous pull, explicit ack / nack (``modify_ack_deadline`` 0).
* ``ensure_topology``: creates topics and subscriptions **on the emulator only**. In GCP,
  Terraform (``infra/terraform/modules/pubsub``) is the source of truth; this function
  refuses to run without ``PUBSUB_EMULATOR_HOST`` unless explicitly allowed.
* ``run_pull_loop``: pulls until the subscription stays empty for ``idle_timeout_s``;
  blocking happens server-side in ``pull``, there are no sleeps.

The client library reads ``PUBSUB_EMULATOR_HOST`` itself; nothing connects at import time.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import google.cloud.pubsub_v1 as pubsub_v1
from google.api_core import exceptions as gexc
from google.protobuf import duration_pb2

from praxis.errors import TransientError
from praxis.streaming.topology import SubscriptionSpec, Topology
from praxis.streaming.transport import ConsumerCrashed, Delivery, Disposition, Worker

EMULATOR_ENV = "PUBSUB_EMULATOR_HOST"


def _duration(seconds: float) -> duration_pb2.Duration:
    whole = int(seconds)
    return duration_pb2.Duration(seconds=whole, nanos=round((seconds - whole) * 1e9))


class PubSubPublisher:
    def __init__(
        self,
        project_id: str,
        client: pubsub_v1.PublisherClient | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._project = project_id
        self._client = client or pubsub_v1.PublisherClient()
        self._timeout = timeout_s

    def _path(self, topic: str) -> str:
        return str(self._client.topic_path(self._project, topic))

    def publish(self, topic: str, data: bytes, attributes: Mapping[str, str]) -> str:
        return self.publish_batch(topic, [(data, attributes)])[0]

    def publish_batch(
        self, topic: str, messages: Sequence[tuple[bytes, Mapping[str, str]]]
    ) -> list[str]:
        path = self._path(topic)
        futures = [self._client.publish(path, data, **dict(attrs)) for data, attrs in messages]
        try:
            return [str(f.result(timeout=self._timeout)) for f in futures]
        except (gexc.GoogleAPICallError, TimeoutError) as exc:
            raise TransientError(f"publish failed: {type(exc).__name__}") from exc


class PubSubPuller:
    def __init__(
        self,
        project_id: str,
        subscription: str,
        client: pubsub_v1.SubscriberClient | None = None,
    ) -> None:
        self.subscription = subscription
        self._client = client or pubsub_v1.SubscriberClient()
        self._path = str(self._client.subscription_path(project_id, subscription))

    def pull(self, max_messages: int = 100, timeout_s: float = 5.0) -> list[Delivery]:
        try:
            response = self._client.pull(
                request={"subscription": self._path, "max_messages": max_messages},
                timeout=timeout_s,
            )
        except gexc.DeadlineExceeded:
            return []
        out: list[Delivery] = []
        for rm in response.received_messages:
            msg = rm.message
            published = msg.publish_time
            out.append(
                Delivery(
                    subscription=self.subscription,
                    message_id=str(msg.message_id),
                    ack_id=str(rm.ack_id),
                    data=bytes(msg.data),
                    attributes=dict(msg.attributes),
                    publish_time=published.astimezone(UTC)
                    if isinstance(published, datetime)
                    else datetime.now(UTC),
                    delivery_attempt=int(rm.delivery_attempt) or 1,
                )
            )
        return out

    def ack(self, ack_ids: Sequence[str]) -> None:
        if ack_ids:
            self._client.acknowledge(request={"subscription": self._path, "ack_ids": list(ack_ids)})

    def nack(self, ack_ids: Sequence[str]) -> None:
        if ack_ids:
            self._client.modify_ack_deadline(
                request={
                    "subscription": self._path,
                    "ack_ids": list(ack_ids),
                    "ack_deadline_seconds": 0,
                }
            )

    def close(self) -> None:
        self._client.close()


def subscription_request(project_id: str, spec: SubscriptionSpec) -> dict[str, Any]:
    request: dict[str, Any] = {
        "name": f"projects/{project_id}/subscriptions/{spec.name}",
        "topic": f"projects/{project_id}/topics/{spec.topic}",
        "ack_deadline_seconds": spec.ack_deadline_s,
        "retry_policy": {
            "minimum_backoff": _duration(spec.min_backoff_s),
            "maximum_backoff": _duration(spec.max_backoff_s),
        },
    }
    if spec.dead_letter_topic and spec.max_delivery_attempts:
        request["dead_letter_policy"] = {
            "dead_letter_topic": f"projects/{project_id}/topics/{spec.dead_letter_topic}",
            "max_delivery_attempts": spec.max_delivery_attempts,
        }
    if spec.attribute_filter:
        request["filter"] = spec.pubsub_filter()
    return request


def ensure_topology(
    project_id: str,
    topology: Topology,
    *,
    publisher: pubsub_v1.PublisherClient | None = None,
    subscriber: pubsub_v1.SubscriberClient | None = None,
    allow_cloud: bool = False,
) -> list[str]:
    """Create missing topics / subscriptions. Returns the names created."""
    if not os.environ.get(EMULATOR_ENV) and not allow_cloud:
        raise RuntimeError(
            f"{EMULATOR_ENV} is not set: cloud topology is managed by Terraform, not by code"
        )
    pub = publisher or pubsub_v1.PublisherClient()
    sub = subscriber or pubsub_v1.SubscriberClient()
    created: list[str] = []
    for topic in topology.topics:
        try:
            pub.create_topic(request={"name": pub.topic_path(project_id, topic)})
            created.append(topic)
        except gexc.AlreadyExists:
            pass
    for spec in topology.subscriptions:
        try:
            sub.create_subscription(request=subscription_request(project_id, spec))
            created.append(spec.name)
        except gexc.AlreadyExists:
            pass
    return created


@dataclass
class PullLoopReport:
    deliveries: int = 0
    acks: int = 0
    nacks: int = 0
    pulls: int = 0
    crashed: bool = False


def run_pull_loop(
    puller: PubSubPuller,
    worker: Worker,
    *,
    max_messages: int = 100,
    idle_timeout_s: float = 5.0,
    max_deliveries: int | None = None,
) -> PullLoopReport:
    """Pull, process, ack / nack until idle for ``idle_timeout_s`` (or a delivery cap)."""
    report = PullLoopReport()
    last_activity = time.monotonic()
    while max_deliveries is None or report.deliveries < max_deliveries:
        deliveries = puller.pull(max_messages, timeout_s=max(1.0, min(idle_timeout_s, 5.0)))
        report.pulls += 1
        if not deliveries:
            if time.monotonic() - last_activity >= idle_timeout_s:
                break
            continue
        last_activity = time.monotonic()
        report.deliveries += len(deliveries)
        groups = [deliveries] if worker.batches else [[d] for d in deliveries]
        try:
            for group in groups:
                dispositions = worker.process(group)
                acks = [
                    d.ack_id
                    for d, s in zip(group, dispositions, strict=True)
                    if s is Disposition.ACK
                ]
                nacks = [
                    d.ack_id
                    for d, s in zip(group, dispositions, strict=True)
                    if s is Disposition.NACK
                ]
                puller.ack(acks)
                puller.nack(nacks)
                report.acks += len(acks)
                report.nacks += len(nacks)
        except ConsumerCrashed:
            report.crashed = True  # in-flight leases expire; Pub/Sub redelivers them
            break
    return report
