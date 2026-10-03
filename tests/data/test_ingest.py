"""Ingestion behaviour at the real boundaries: HTTP (mock transport), filesystem raw archive,
DuckDB warehouse. Gate items: idempotent ingestion, raw replay without the source, safe
behaviour on outage."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from praxis.data.fetch import HttpFetcher, RetryPolicy
from praxis.data.ingest import IngestService, Outcome
from praxis.data.models import SourceId
from praxis.data.raw_store import LocalRawStore
from praxis.data.warehouse import Warehouse
from tests.data.helpers import (
    FAKE_EIA_KEY,
    FAKE_FRED_KEY,
    FIXTURE_FOR_SOURCE,
    NOW,
    WINDOW,
    fixture_bytes,
    fixture_handler,
    make_fetcher,
    registry,
)


def _service(
    tmp_path: Path, handler: object = None, wh: Warehouse | None = None, **keys: object
) -> tuple[IngestService, Warehouse, LocalRawStore]:
    wh = wh or Warehouse()
    wh.migrate()
    raw = LocalRawStore(tmp_path / "raw")
    fetcher = make_fetcher(handler or fixture_handler())  # type: ignore[arg-type]
    return IngestService(registry(**keys), fetcher, raw, wh, clock=lambda: NOW), wh, raw


def _signal_state(wh: Warehouse) -> list[tuple[object, ...]]:
    return wh.con.execute(
        "SELECT record_key, value, batch_id, retrieved_at FROM raw.external_signals ORDER BY 1"
    ).fetchall()


def _archive_listing(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("source", list(SourceId))
def test_ingesting_the_same_batch_twice_creates_no_duplicates(
    tmp_path: Path, source: SourceId
) -> None:
    service, wh, _ = _service(tmp_path)
    first = service.ingest(source, WINDOW)
    assert first.new_records > 0 and first.all_succeeded
    state_1, files_1 = _signal_state(wh), _archive_listing(tmp_path / "raw")
    batches_1 = wh.count("raw.ingest_batches")

    second = service.ingest(source, WINDOW)
    assert second.new_records == 0
    assert second.count(Outcome.DUPLICATE) == len(second.results)
    assert _signal_state(wh) == state_1
    assert _archive_listing(tmp_path / "raw") == files_1
    assert wh.count("raw.ingest_batches") == batches_1


def test_duplicate_fetch_keeps_original_retrieval_time(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    service.ingest(SourceId.FRED, WINDOW)
    later = IngestService(
        registry(),
        make_fetcher(fixture_handler()),
        LocalRawStore(tmp_path / "raw"),
        wh,
        clock=lambda: NOW.replace(year=2027),
    )
    later.ingest(SourceId.FRED, WINDOW)
    years = {
        r[0]
        for r in wh.con.execute("SELECT year(retrieved_at) FROM raw.external_signals").fetchall()
    }
    assert years == {2026}


def test_changed_body_is_a_new_batch_and_revises_by_logical_key(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    service.ingest(SourceId.FRED, WINDOW)
    count = wh.count("raw.external_signals")
    revised = json.loads(fixture_bytes("fred_cpiaucsl.json"))
    revised["observations"][0]["value"] = "999.000"
    later_service = IngestService(
        registry(),
        make_fetcher(fixture_handler({SourceId.FRED: json.dumps(revised).encode()})),
        LocalRawStore(tmp_path / "raw"),
        wh,
        clock=lambda: NOW.replace(day=11),
    )
    result = later_service.ingest(SourceId.FRED, WINDOW)
    assert result.new_records == 0  # same logical records, revised value
    assert wh.count("raw.external_signals") == count
    assert wh.count("raw.ingest_batches") == 3 + 3  # three series, two vintages each
    value = wh.con.execute(
        "SELECT value FROM raw.external_signals WHERE metric = 'cpiaucsl' "
        "AND observed_date = DATE '2026-01-01'"
    ).fetchone()
    assert value == (999.0,)


def test_crash_between_archive_and_warehouse_heals_on_rerun(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    service.ingest(SourceId.EIA, WINDOW)
    expected = _signal_state(wh)
    wh.con.execute("DELETE FROM raw.external_signals")  # simulate lost warehouse write
    report = service.ingest(SourceId.EIA, WINDOW)
    assert report.count(Outcome.DUPLICATE) == len(report.results)  # archive already had it
    assert _signal_state(wh) == expected


def test_raw_replay_rebuilds_records_without_any_network_call(tmp_path: Path) -> None:
    service, wh, raw = _service(tmp_path)
    for sid in SourceId:
        service.ingest(sid, WINDOW)
    expected = _signal_state(wh)
    assert expected

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network used during replay: {request.url}")

    fresh_wh = Warehouse()
    fresh_wh.migrate()
    offline = IngestService(
        registry(), make_fetcher(forbidden), raw, fresh_wh, clock=lambda: NOW.replace(year=2030)
    )
    report = offline.replay()
    assert report.new_records == len(expected)
    assert _signal_state(fresh_wh) == expected  # identical, including original retrieved_at
    replay_again = offline.replay()
    assert replay_again.new_records == 0


def test_replay_can_be_restricted_to_one_source(tmp_path: Path) -> None:
    service, _, raw = _service(tmp_path)
    for sid in (SourceId.FRED, SourceId.EIA):
        service.ingest(sid, WINDOW)
    other = Warehouse()
    other.migrate()
    only_eia = IngestService(registry(), make_fetcher(fixture_handler()), raw, other)
    only_eia.replay(SourceId.EIA)
    sources = {
        r[0]
        for r in other.con.execute("SELECT DISTINCT source FROM raw.external_signals").fetchall()
    }
    assert sources == {"eia"}


# --- outage / failure isolation -------------------------------------------------------
def test_outage_is_reported_and_leaves_existing_data_untouched(tmp_path: Path) -> None:
    service, wh, raw = _service(tmp_path)
    service.ingest(SourceId.OPEN_METEO, WINDOW)
    before, files = _signal_state(wh), _archive_listing(tmp_path / "raw")

    calls: list[int] = []

    def down(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    sleeps: list[float] = []
    fetcher = HttpFetcher(
        httpx.Client(transport=httpx.MockTransport(down)),
        RetryPolicy(max_attempts=2),
        sleeps.append,
    )
    outage = IngestService(registry(), fetcher, raw, wh, clock=lambda: NOW)
    report = outage.ingest(SourceId.OPEN_METEO, WINDOW)

    assert report.count(Outcome.UNAVAILABLE) == len(report.results) > 0
    assert len(calls) == 2 * len(report.results)  # bounded retries, one batch per request
    assert report.new_records == 0 and not report.all_succeeded
    assert _signal_state(wh) == before
    assert _archive_listing(tmp_path / "raw") == files


def test_one_failing_request_does_not_block_the_others(tmp_path: Path) -> None:
    ok = fixture_handler()
    seen: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("latitude", ""))
        if request.url.params.get("latitude") == "51.5074":
            return httpx.Response(400, json={"error": True, "reason": "bad"})
        return ok(request)

    service, _, _ = _service(tmp_path, flaky)
    report = service.ingest(SourceId.OPEN_METEO, WINDOW)
    assert report.count(Outcome.REJECTED) == 1
    assert report.count(Outcome.STORED) == len(report.results) - 1


def test_missing_credential_skips_source_without_any_request(tmp_path: Path) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the API without a key")

    service, wh, _ = _service(tmp_path, forbidden, fred=False)
    report = service.ingest(SourceId.FRED, WINDOW)
    assert [r.outcome for r in report.results] == [Outcome.MISSING_CREDENTIAL]
    assert "PRAXIS_FRED_API_KEY" in (report.results[0].detail or "")
    assert wh.count("raw.external_signals") == 0


# --- schema drift ---------------------------------------------------------------------
def test_schema_change_quarantines_raw_and_produces_no_records(tmp_path: Path) -> None:
    broken = json.loads(fixture_bytes(FIXTURE_FOR_SOURCE[SourceId.EIA]))
    broken["response"].pop("data")
    service, wh, raw = _service(
        tmp_path, fixture_handler({SourceId.EIA: json.dumps(broken).encode()})
    )
    report = service.ingest(SourceId.EIA, WINDOW)
    assert report.count(Outcome.QUARANTINED) == len(report.results) > 0
    assert wh.count("raw.external_signals") == 0
    metas = list(raw.iter_meta(SourceId.EIA))
    assert all(m.quarantine_reason for m in metas) and len(metas) == len(report.results)
    row = wh.con.execute(
        "SELECT DISTINCT quality_status, record_count FROM raw.ingest_batches"
    ).fetchall()
    assert row == [("quarantined", 0)]


def test_quarantined_batch_stays_archived_and_replay_is_stable(tmp_path: Path) -> None:
    # Archive a body the current parser rejects, as a drifted source would deliver it.
    drifted = json.loads(fixture_bytes("fred_cpiaucsl.json"))
    drifted["observations"][0]["value"] = "n/a"
    drift_service, wh, _ = _service(
        tmp_path, fixture_handler({SourceId.FRED: json.dumps(drifted).encode()})
    )
    drift_service.ingest(SourceId.FRED, WINDOW)
    assert wh.count("raw.external_signals") == 0
    # Replay re-parses the archive; the unchanged parser keeps it quarantined, nothing is lost.
    again = drift_service.replay(SourceId.FRED)
    assert again.count(Outcome.QUARANTINED) == len(again.results)


def test_partial_batch_is_flagged_not_failed(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    service.ingest(SourceId.FRED, WINDOW)
    rows = wh.con.execute(
        "SELECT quality_status, skipped_count FROM raw.ingest_batches "
        "WHERE series_id = 'fred:CPIAUCSL'"
    ).fetchall()
    assert rows == [("partial", 1)]


# --- provenance and secrecy -----------------------------------------------------------
def test_every_batch_carries_full_provenance(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    for sid in SourceId:
        service.ingest(sid, WINDOW)
    rows = wh.con.execute(
        "SELECT source, series_id, endpoint, retrieved_at IS NOT NULL, "
        "source_timestamp_min IS NOT NULL, source_timestamp_max IS NOT NULL, schema_version, "
        "length(checksum_sha256), http_status, quality_status, is_synthetic "
        "FROM raw.ingest_batches"
    ).fetchall()
    assert rows
    for row in rows:
        assert row[1] and row[2].startswith("https://")
        assert row[3] and row[4] and row[5]
        assert row[6] == 1 and row[7] == 64 and row[8] == 200
        assert row[9] in {"ok", "partial"} and row[10] is False


def test_credentials_never_reach_archive_warehouse_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    service, wh, _ = _service(tmp_path)
    for sid in (SourceId.FRED, SourceId.EIA):
        service.ingest(sid, WINDOW)
    for path in (tmp_path / "raw").rglob("*"):
        if path.is_file():
            blob = path.read_bytes().decode(errors="ignore")
            assert FAKE_FRED_KEY not in blob and FAKE_EIA_KEY not in blob
    dump = json.dumps(wh.con.execute("SELECT * FROM raw.ingest_batches").fetchall(), default=str)
    assert FAKE_FRED_KEY not in dump and FAKE_EIA_KEY not in dump
    assert FAKE_FRED_KEY not in caplog.text and FAKE_EIA_KEY not in caplog.text
    assert "REDACTED" in dump  # the redaction marker is what is stored


def test_synthetic_flag_is_false_for_real_external_data(tmp_path: Path) -> None:
    service, wh, _ = _service(tmp_path)
    service.ingest(SourceId.CARBON_INTENSITY, WINDOW)
    assert wh.con.execute("SELECT bool_or(is_synthetic) FROM raw.ingest_batches").fetchone() == (
        False,
    )


def test_refetch_differing_only_in_server_timing_is_the_same_batch(tmp_path: Path) -> None:
    base = json.loads(fixture_bytes(FIXTURE_FOR_SOURCE[SourceId.OPEN_METEO]))
    timings = iter(range(1, 100))

    def handler(request: httpx.Request) -> httpx.Response:
        body = {**base, "generationtime_ms": float(next(timings))}
        return httpx.Response(200, content=json.dumps(body).encode())

    service, wh, _ = _service(tmp_path, handler)
    service.ingest(SourceId.OPEN_METEO, WINDOW)
    batches, files = wh.count("raw.ingest_batches"), _archive_listing(tmp_path / "raw")
    again = service.ingest(SourceId.OPEN_METEO, WINDOW)
    assert again.count(Outcome.DUPLICATE) == len(again.results)
    assert wh.count("raw.ingest_batches") == batches
    assert _archive_listing(tmp_path / "raw") == files
