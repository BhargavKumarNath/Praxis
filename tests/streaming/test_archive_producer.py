"""Archive (append-only, content-addressed) and producer (validate -> archive -> publish)."""

from __future__ import annotations

from pathlib import Path

import pytest

from praxis.events.codec import ATTR_REPLAY, ATTR_SENT_AT, decode
from praxis.streaming.archive import ArchiveIntegrityError, EventArchive
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.producer import EventProducer, ProducerContractError, replay_archive
from praxis.streaming.topology import WAREHOUSE, build_topology
from tests.streaming.helpers import customer_lifecycle, invoice_lifecycle, sim_events

TOPO = build_topology()
WH = TOPO.by_role(WAREHOUSE).name


def test_layout_matches_gcs_partitioning(tmp_path: Path) -> None:
    archive = EventArchive(tmp_path)
    result = archive.append(customer_lifecycle())
    assert result.events == 5 and result.files_written >= 2
    rel = [p.relative_to(tmp_path).parts for p in archive.files()]
    assert rel[0][:3] == ("source=simulator", "date=2026-01-05", "hour=00")
    assert all(parts[3].startswith("part-") and parts[3].endswith(".ndjson") for parts in rel)


def test_append_is_idempotent_and_iteration_dedupes(tmp_path: Path) -> None:
    archive = EventArchive(tmp_path)
    events = list(sim_events(30, 7))
    archive.append(events)
    again = archive.append(events)
    assert again.files_written == 0 and again.files_existing > 0
    archive.append(events[:10] + events[5:15])  # overlapping batch: new file, same events
    replayed = list(archive.iter_events())
    assert len(replayed) == len(events)
    assert {e["event_id"] for e in replayed} == {e["event_id"] for e in events}


def test_corrupted_archive_file_is_detected(tmp_path: Path) -> None:
    archive = EventArchive(tmp_path)
    archive.append(customer_lifecycle())
    victim = archive.files()[0]
    victim.write_bytes(victim.read_bytes().replace(b"saas", b"SAAS"))
    with pytest.raises(ArchiveIntegrityError):
        list(archive.iter_events())


def test_producer_archives_then_publishes_with_attributes(tmp_path: Path) -> None:
    broker = MemoryBroker(TOPO)
    archive = EventArchive(tmp_path)
    producer = EventProducer(broker, TOPO.events_topic, archive, clock=lambda: 1234.5)
    report = producer.publish(customer_lifecycle())
    assert report.published == 5 and report.archived_files >= 1
    deliveries = broker.pull(WH)
    assert len(deliveries) == 5
    assert deliveries[0].attributes[ATTR_SENT_AT] == "1234.500000"
    assert ATTR_REPLAY not in deliveries[0].attributes
    assert decode(deliveries[0].data, deliveries[0].attributes).event_type == "customer.created"


def test_invalid_event_rejects_whole_batch_before_side_effects(tmp_path: Path) -> None:
    broker = MemoryBroker(TOPO)
    archive = EventArchive(tmp_path)
    producer = EventProducer(broker, TOPO.events_topic, archive)
    bad = dict(invoice_lifecycle()[0], payload={"invoice_id": "x"})
    with pytest.raises(ProducerContractError) as info:
        producer.publish([*customer_lifecycle(), bad])
    assert info.value.index == 5 and info.value.reason == "invalid_payload"
    assert archive.files() == [] and broker.backlog(WH) == 0


def test_replay_republishes_same_ids_marked_as_replay(tmp_path: Path) -> None:
    archive = EventArchive(tmp_path)
    archive.append(list(sim_events(30, 7)))
    broker = MemoryBroker(TOPO)
    producer = EventProducer(broker, TOPO.events_topic, archive)
    n = replay_archive(archive, producer, batch_size=1000)
    assert n == len(sim_events(30, 7))
    pulled = broker.pull(WH, n)
    assert {d.attributes["event_id"] for d in pulled} == {e["event_id"] for e in sim_events(30, 7)}
    assert all(d.attributes[ATTR_REPLAY] == "true" for d in pulled)
    assert len(archive.files()) == len(list(tmp_path.rglob("*.ndjson")))  # replay wrote nothing
