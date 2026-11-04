"""``redrive_all`` drains every open dead letter in pages (Phase 15 finding: one
``redrive_dead_letters`` call stopped at its 1000 limit, and the runbook did not say to repeat)."""

from __future__ import annotations

from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.events.codec import encode
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.redrive import redrive_all
from praxis.streaming.topology import build_topology
from tests.streaming.helpers import customer_lifecycle


def _dead(store: ControlPlaneStore, n: int) -> None:
    for i in range(n):
        e = customer_lifecycle(f"cust_rd{i}")[0]
        body, _ = encode(e)
        store.record_dead_letter(DeadLetterRecord("praxis-local-events-operational",
            "handler_error", "x", e["event_id"], e["event_type"], e["trace_id"],
            e["correlation_id"], 5, body))  # fmt: skip


def test_every_page_is_redriven_and_undecodable_letters_end_the_loop(
    store: ControlPlaneStore,
) -> None:
    _dead(store, 7)
    store.record_dead_letter(DeadLetterRecord("praxis-local-events-operational",
        "malformed_json", "x", None, None, None, None, 1, b"{not json"))  # fmt: skip
    topo = build_topology()
    broker = MemoryBroker(topo)
    report = redrive_all(store, broker, topo.events_topic, page=3)
    assert (report.republished, report.skipped_undecodable) == (7, 1)
    assert store.dead_letter_counts() == {"malformed_json": 1}  # never republished
    assert redrive_all(store, broker, topo.events_topic, page=3).republished == 0


def test_pages_are_bounded(store: ControlPlaneStore) -> None:
    _dead(store, 5)
    topo = build_topology()
    report = redrive_all(store, MemoryBroker(topo), topo.events_topic, page=2, max_pages=2)
    assert report.republished == 4 and sum(store.dead_letter_counts().values()) == 1
