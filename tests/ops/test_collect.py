"""Pull collectors and the system reader (ADR 0020), over a real DuckDB file and Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from praxis.control.db import make_engine
from praxis.control.metric_snapshots import MetricSnapshotStore
from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.data.models import (
    BatchProvenance,
    QualityStatus,
    SignalRecord,
    SourceId,
    make_record_key,
)
from praxis.data.warehouse import Warehouse
from praxis.errors import TransientError
from praxis.ops.collect import (
    CollectorSettings,
    dead_letter_collector,
    revenue_collector,
    run_collectors,
    warehouse_collector,
)
from praxis.ops.config import load_ops_config
from praxis.ops.health import build_health, render_html
from praxis.ops.system import SystemReader
from praxis.telemetry import Telemetry
from tests.data.helpers import NOW, cfg


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    eng = make_engine(pg_url)
    yield eng
    eng.dispose()


def _signal(source: SourceId, observed: datetime) -> SignalRecord:
    return SignalRecord(
        record_key=make_record_key(source, "s", "e", "m", observed),
        source=source, series_id="s", entity_id="e", metric="m", unit="u",
        observed_at=observed, value=1.0, batch_id="b" * 64, retrieved_at=NOW,
    )  # fmt: skip


def _batch(
    source: SourceId, n: int, retrieved: datetime, quality: QualityStatus
) -> BatchProvenance:
    return BatchProvenance(
        batch_id=f"{n:064d}", source=source, series_id="s", endpoint="https://x/y",
        retrieved_at=retrieved, checksum_sha256="c" * 64, http_status=200,
        quality_status=quality,
    )  # fmt: skip


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "wh.duckdb"
    with Warehouse(path) as wh:
        wh.migrate()
        wh.upsert_signals([
            _signal(SourceId.FRED, NOW - timedelta(days=10)),
            _signal(SourceId.EIA, NOW - timedelta(hours=2)),
            _signal(SourceId.EIA, NOW + timedelta(days=1)),  # forecast slot: not an observation
        ])  # fmt: skip
        wh.record_batch(
            _batch(SourceId.FRED, 1, NOW - timedelta(hours=3), QualityStatus.QUARANTINED)
        )
        wh.record_batch(
            _batch(SourceId.FRED, 2, NOW - timedelta(days=3), QualityStatus.QUARANTINED)
        )
        wh.record_batch(_batch(SourceId.EIA, 3, NOW - timedelta(hours=1), QualityStatus.OK))
    return path


def test_warehouse_collector_feeds_source_age_and_windowed_contract_failures(
    warehouse: Path,
) -> None:
    got = run_collectors({"warehouse": warehouse_collector(warehouse, cfg())}, NOW)
    view = got.view
    ages = {p.labels["source"]: p.value for p in view.select("data_source_age_s")}
    assert ages == {"fred": 10 * 86400.0, "eia": 2 * 3600.0}  # missing sources: no age
    failures = {p.labels["source"]: p.value for p in view.select("data_contract_failures_total")}
    assert failures == {"open_meteo": 0, "carbon_intensity": 0, "eia": 0, "fred": 1}  # 3 d ago: out
    note = got.notes["warehouse"]
    assert note["status"] == "ok"
    assert sorted(note["missing"]) == ["carbon_intensity", "open_meteo"]
    assert note["contract_window_h"] == 24


def test_warehouse_collector_alerts_through_the_pinned_rules(warehouse: Path) -> None:
    view = run_collectors({"w": warehouse_collector(warehouse, cfg())}, NOW).view
    fired = {a.rule_id for a in build_health(load_ops_config(), view, now=NOW).alerts()}
    assert "data_contract_failure" in fired
    assert "source_stale" not in fired  # fred 10 d < its 75 d limit, eia 2 h < 5 d
    later = NOW + timedelta(days=80)
    view = run_collectors({"w": warehouse_collector(warehouse, cfg())}, later).view
    fired = {a.rule_id for a in build_health(load_ops_config(), view, now=later).alerts()}
    assert "source_stale" in fired  # a source that stopped delivering is now detected
    assert "data_contract_failure" not in fired  # the quarantine left the window


def test_missing_or_locked_warehouse_marks_only_its_collector(tmp_path: Path) -> None:
    def other(tel: Telemetry, _now: datetime) -> dict[str, Any]:
        tel.set("dlq_backlog", 0, subscription="s")
        return {}

    got = run_collectors(
        {"warehouse": warehouse_collector(tmp_path / "absent.duckdb", cfg()), "other": other}, NOW
    )
    assert got.notes["warehouse"]["status"] == "unavailable"
    assert got.notes["other"] == {"status": "ok"}
    assert not (tmp_path / "absent.duckdb").exists()  # a reader never creates the file
    assert [p.name for p in got.view.points] == ["dlq_backlog"]


def test_collector_bugs_are_not_swallowed() -> None:
    def broken(_tel: Telemetry, _now: datetime) -> dict[str, Any]:
        raise KeyError("bug")

    with pytest.raises(KeyError):
        run_collectors({"broken": broken}, NOW)


def test_dead_letter_collector_reports_open_backlog_and_drained_zero(engine: Engine) -> None:
    store = ControlPlaneStore(engine)
    for sub, data in [("ops", b"a"), ("ops", b"b"), ("analytics", b"c")]:
        store.record_dead_letter(DeadLetterRecord(sub, "malformed_json", None, None, None, None,
                                                  None, 1, data))  # fmt: skip
    with engine.begin() as conn:
        conn.execute(text("UPDATE dead_letters SET redriven_at = now() "
                          "WHERE source_subscription = 'analytics'"))  # fmt: skip
    view = run_collectors({"dlq": dead_letter_collector(store)}, NOW).view
    assert {p.labels["subscription"]: p.value for p in view.select("dlq_backlog")} == {
        "analytics": 0.0, "ops": 2.0,
    }  # fmt: skip


def _pay(engine: Engine, day: date, minor: int, n: int, currency: str = "GBP") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO payment_ledger (invoice_id, customer_id, amount_minor, currency, "
                 "event_id, paid_at) VALUES (:i, 'c', :m, :ccy, gen_random_uuid(), :t)"),
            {"i": f"inv_{currency}_{day}_{n}", "m": minor, "ccy": currency,
             "t": datetime(day.year, day.month, day.day, 10, tzinfo=UTC)},
        )  # fmt: skip


def test_revenue_collector_compares_the_last_complete_day_with_its_median(
    engine: Engine,
) -> None:
    start = date(2026, 3, 1)
    for i in range(10):  # 1 Mar .. 10 Mar at 1000; 5 Mar has no payments (counts as 0)
        if i != 4:
            _pay(engine, start + timedelta(days=i), 1000, 0)
    _pay(engine, start + timedelta(days=10), 500, 0)  # 11 Mar: last COMPLETE day
    _pay(engine, start + timedelta(days=11), 1, 0)  # 12 Mar: newest day, may be partial
    got = run_collectors({"revenue": revenue_collector(ControlPlaneStore(engine))}, NOW)
    (point,) = got.view.select("business_kpi_deviation")
    assert point.labels == {"kpi": "daily_revenue_gbp"}
    assert point.value == pytest.approx(-0.5)  # median of 10 days (one 0) is 1000
    kpi = got.notes["revenue"]["kpis"]["daily_revenue_gbp"]
    assert (kpi["day"], kpi["value_minor"], kpi["history_days"]) == ("2026-03-11", 500, 10)
    assert got.notes["revenue"]["origin"].startswith("synthetic")
    fired = {a.rule_id for a in build_health(load_ops_config(), got.view, now=NOW).alerts()}
    assert "business_kpi_shock" in fired  # |-0.5| > 0.30


def test_revenue_collector_waits_for_enough_history(engine: Engine) -> None:
    for i in range(5):
        _pay(engine, date(2026, 3, 1) + timedelta(days=i), 1000, 0)
    settings = CollectorSettings(kpi_min_history=7)
    got = run_collectors({"r": revenue_collector(ControlPlaneStore(engine), settings)}, NOW)
    assert got.view.select("business_kpi_deviation") == []
    assert got.notes["r"]["kpis"]["daily_revenue_gbp"]["deviation"] is None


def test_revenue_collector_with_an_empty_ledger_reports_nothing(engine: Engine) -> None:
    got = run_collectors({"r": revenue_collector(ControlPlaneStore(engine))}, NOW)
    assert got.notes["r"]["kpis"] == {} and got.view.points == ()


def test_system_reader_merges_fleet_collectors_and_local(
    engine: Engine, tel: Telemetry, warehouse: Path
) -> None:
    snapshots = MetricSnapshotStore(engine)
    other = Telemetry()
    other.set("queue_backlog", 1500, subscription="ops")
    snapshots.publish("consumer", "h:1", other.snapshot(), now=NOW, ttl=timedelta(minutes=1),
                      started_at=NOW)  # fmt: skip
    snapshots.publish("pricing", "h:2", other.snapshot(), now=NOW - timedelta(hours=1),
                      ttl=timedelta(minutes=1), started_at=NOW - timedelta(hours=1))  # fmt: skip
    tel.inc("http_requests_total", route="/x", method="GET", status=200)
    reader = SystemReader(snapshots, {"warehouse": warehouse_collector(warehouse, cfg())})
    system = reader.read(NOW)
    assert system.view.total("http_requests_total") == 1  # local
    assert system.view.peak("queue_backlog") == 1500  # pushed by another process
    assert system.view.select("data_source_age_s")  # pulled
    inputs = system.inputs()
    assert inputs["telemetry_scope"] == "fleet"
    assert inputs["stale_processes"] == ["pricing"]
    report = build_health(load_ops_config(), system.view, now=NOW, inputs=inputs)
    assert "queue_backlog_high" in {a.rule_id for a in report.alerts()}
    assert report.as_dict()["inputs"]["stale_processes"] == ["pricing"]
    page = render_html(report)
    assert "STALE" in page and "collector warehouse" in page


class _DownSnapshots(MetricSnapshotStore):
    def __init__(self) -> None:
        pass

    def read(self, *args: Any, **kwargs: Any) -> Any:
        raise TransientError("database unavailable: OperationalError")


def test_system_reader_degrades_to_process_scope_when_the_store_is_down(tel: Telemetry) -> None:
    tel.inc("http_requests_total", route="/x", method="GET", status=200)
    system = SystemReader(_DownSnapshots()).read(NOW)
    assert system.scope == "process (snapshot store unavailable)"
    assert system.view.total("http_requests_total") == 1
    assert SystemReader().read(NOW).inputs()["telemetry_scope"] == "process"
