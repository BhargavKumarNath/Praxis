"""Topology: names, retry/DLQ policy, filters, and agreement with Terraform."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from praxis.streaming.pubsub import subscription_request
from praxis.streaming.topology import (
    DLQ_INSPECT,
    MONITORING,
    OPERATIONAL,
    WAREHOUSE,
    build_topology,
)

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "modules" / "pubsub"


def test_default_names_follow_the_resource_prefix() -> None:
    t = build_topology("praxis", "dev")
    assert t.events_topic == "praxis-dev-events"
    assert t.dead_letter_topic == "praxis-dev-events-dlq"
    assert {s.role: s.name for s in t.subscriptions} == {
        OPERATIONAL: "praxis-dev-events-operational",
        WAREHOUSE: "praxis-dev-events-warehouse",
        MONITORING: "praxis-dev-events-monitoring",
        DLQ_INSPECT: "praxis-dev-events-dlq-inspect",
    }


def test_every_consumer_subscription_has_bounded_retries_and_a_dlq() -> None:
    t = build_topology()
    for role in (OPERATIONAL, WAREHOUSE, MONITORING):
        spec = t.by_role(role)
        assert spec.dead_letter_topic == t.dead_letter_topic
        assert spec.max_delivery_attempts == 5
    assert t.by_role(DLQ_INSPECT).dead_letter_topic is None
    with pytest.raises(KeyError):
        t.by_role("nope")


def test_operational_filter_keeps_bulk_events_out_of_postgres() -> None:
    spec = build_topology().by_role(OPERATIONAL)
    assert spec.pubsub_filter() == 'attributes.stateful = "true"'
    assert spec.matches({"stateful": "true"})
    assert not spec.matches({"stateful": "false"})
    assert not spec.matches(None)
    assert build_topology().by_role(WAREHOUSE).matches(None)


def test_backoff_is_exponential_and_capped() -> None:
    spec = build_topology(min_backoff_s=10, max_backoff_s=60).by_role(OPERATIONAL)
    assert [spec.backoff_s(n) for n in range(1, 6)] == [10, 20, 40, 60, 60]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_delivery_attempts": 4},
        {"max_delivery_attempts": 101},
        {"min_backoff_s": 20, "max_backoff_s": 10},
        {"max_backoff_s": 601},
        {"ack_deadline_s": 5},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        build_topology(**kwargs)  # type: ignore[arg-type]


def test_overlong_filter_is_rejected() -> None:
    from dataclasses import replace

    spec = replace(build_topology().by_role(OPERATIONAL), attribute_filter=(("k" * 300, "v"),))
    with pytest.raises(ValueError, match="256 bytes"):
        spec.pubsub_filter()


def test_subscription_request_shape() -> None:
    t = build_topology(min_backoff_s=1.5)
    req = subscription_request("proj", t.by_role(OPERATIONAL))
    assert req["dead_letter_policy"]["max_delivery_attempts"] == 5
    assert (
        req["dead_letter_policy"]["dead_letter_topic"]
        == "projects/proj/topics/praxis-local-events-dlq"
    )
    assert req["retry_policy"]["minimum_backoff"].seconds == 1
    assert req["retry_policy"]["minimum_backoff"].nanos == 500_000_000
    assert req["filter"] == 'attributes.stateful = "true"'
    dlq = subscription_request("proj", t.by_role(DLQ_INSPECT))
    assert "dead_letter_policy" not in dlq and "filter" not in dlq


def test_terraform_declares_the_same_topology() -> None:
    """Static drift check: every subscription, policy and filter exists in Terraform."""
    main = (TF / "main.tf").read_text()
    variables = (TF / "variables.tf").read_text()
    t = build_topology("PREFIX", "ENV")
    for spec in t.subscriptions:
        suffix = spec.name.removeprefix("PREFIX-ENV-")
        assert f'"${{var.prefix}}-${{var.environment}}-{suffix}"' in main, suffix
    assert main.count("dead_letter_policy {") == 3
    assert 'filter                     = "attributes.stateful = \\"true\\""' in main
    assert re.search(r'max_delivery_attempts"\s*{[^}]*default\s*=\s*5', variables, re.S)
    assert 'minimum_backoff = "10s"' in main and 'maximum_backoff = "300s"' in main
    # Pub/Sub's service agent must be able to publish to the DLQ and ack the source.
    assert "roles/pubsub.publisher" in main and "roles/pubsub.subscriber" in main
