"""The ops monitor process (``python -m praxis.ops run``) and the fleet-wide health API.

Another process's published metrics must open an incident through the real monitor loop,
and the API must report the same system, with each input's freshness (ADR 0020).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from praxis.api.app import create_app
from praxis.config import Settings, get_settings
from praxis.control.db import make_engine
from praxis.control.metric_snapshots import MetricSnapshotStore, SnapshotPublisher
from praxis.errors import TransientError
from praxis.ops import __main__ as ops_cli
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import IncidentManager, LogNotifier, MemoryIncidentStore
from praxis.ops.monitor import OpsMonitor
from praxis.ops.system import build_reader
from praxis.telemetry import Telemetry


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    eng = make_engine(pg_url)
    yield eng
    eng.dispose()


def _consumer_reports_backlog(engine: Engine, backlog: int) -> None:
    other = Telemetry()  # stands in for a separate consumer process
    other.set("queue_backlog", backlog, subscription="praxis-ops")
    now = datetime.now(UTC)
    MetricSnapshotStore(engine).publish("consumer", "host:42", other.snapshot(), now=now,
                                        ttl=timedelta(minutes=5), started_at=now)  # fmt: skip


def _ticks(stdout: str) -> list[dict[str, Any]]:
    """Tick summaries; structured log lines share stdout and carry a ``level``."""
    lines = [json.loads(line) for line in stdout.splitlines()]
    return [line for line in lines if "level" not in line]


def _run(monkeypatch: pytest.MonkeyPatch, pg_url: str, *args: str) -> int:
    monkeypatch.setenv("PRAXIS_DATABASE_URL", pg_url)
    get_settings.cache_clear()
    return ops_cli.main(["run", "--interval", "0", *args])


def test_monitor_turns_another_processes_metrics_into_an_incident(
    monkeypatch: pytest.MonkeyPatch, pg_url: str, engine: Engine, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    _consumer_reports_backlog(engine, 2500)
    code = _run(monkeypatch, pg_url, "--max-ticks", "2", "--warehouse", str(tmp_path / "no.db"))
    assert code == 0
    first, second = _ticks(capsys.readouterr().out)
    assert "queue_backlog_high" in first["alerts"]
    assert len(first["opened"]) == 1 and second["opened"] == []  # deduplicated
    assert first["collectors"] == {"dead_letters": "ok", "revenue": "ok",
                                   "warehouse": "unavailable"}  # fmt: skip
    with engine.connect() as conn:
        rule = conn.execute(text("SELECT rule_id FROM ops_incidents WHERE status = 'open'"))
        assert [r[0] for r in rule] == ["queue_backlog_high"]
        rows = conn.execute(text("SELECT component FROM ops_metric_snapshots ORDER BY 1"))
        assert [r[0] for r in rows] == ["consumer", "ops-monitor"]  # the monitor publishes too


def test_monitor_resolves_once_the_backlog_drains(
    monkeypatch: pytest.MonkeyPatch, pg_url: str, engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    _consumer_reports_backlog(engine, 2500)
    _run(monkeypatch, pg_url, "--max-ticks", "1")
    _consumer_reports_backlog(engine, 3)
    _run(monkeypatch, pg_url, "--max-ticks", "2")  # two clear ticks resolve (ADR 0018)
    lines = _ticks(capsys.readouterr().out)
    assert lines[-1]["resolved"] and lines[-1]["alerts"] == []


def test_monitor_needs_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert ops_cli.main(["run", "--max-ticks", "1"]) == 2
    assert "PRAXIS_DATABASE_URL" in capsys.readouterr().err


class _OutageMonitor(OpsMonitor):
    def __init__(self) -> None:
        pass

    def tick(self, now: datetime | None = None, **_: Any) -> Any:
        raise TransientError("database unavailable: OperationalError")


def test_a_failed_tick_is_reported_and_the_loop_survives(engine: Engine) -> None:
    store = MetricSnapshotStore(engine)
    summary = ops_cli._tick_once(_OutageMonitor(), SnapshotPublisher(store, "ops-monitor"), store)
    assert summary["status"] == "tick_failed"
    assert "database unavailable" in summary["error"]


def test_health_api_reports_the_fleet_and_the_freshness_of_each_input(
    engine: Engine, tmp_path: Path
) -> None:
    _consumer_reports_backlog(engine, 1500)
    snapshots = MetricSnapshotStore(engine)
    old = datetime.now(UTC) - timedelta(hours=2)
    snapshots.publish("pricing", "job:1", Telemetry().snapshot(), now=old,
                      ttl=timedelta(minutes=30), started_at=old)  # fmt: skip
    publisher = SnapshotPublisher(snapshots, "api", interval=timedelta(hours=1))
    reader = build_reader(engine, tmp_path / "absent.duckdb", own=("api", publisher.instance))
    app = create_app(Settings(), incident_store=MemoryIncidentStore(),
                     system_view=(reader, publisher))  # fmt: skip
    with TestClient(app) as client:  # lifespan: the API publishes its own snapshot
        body = client.get("/v1/ops/health").json()
        page = client.get("/ops/dashboard").text
    inputs = body["inputs"]
    assert inputs["telemetry_scope"] == "fleet"
    assert inputs["stale_processes"] == ["pricing"]
    assert inputs["collectors"]["warehouse"]["status"] == "unavailable"
    assert {a["rule_id"] for a in body["sections"]["service"]["alerts"]} >= {"queue_backlog_high"}
    assert "STALE" in page
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM ops_metric_snapshots WHERE component='api'"))
        assert n.scalar() == 1


def test_api_built_from_settings_reads_and_joins_the_fleet(
    monkeypatch: pytest.MonkeyPatch, pg_url: str, engine: Engine
) -> None:
    _consumer_reports_backlog(engine, 1500)
    monkeypatch.setenv("PRAXIS_DATABASE_URL", pg_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:  # real builders: incidents, reader, publisher
        body = client.get("/v1/ops/health").json()
        assert client.get("/v1/ops/incidents").json() == {"incidents": []}
    assert body["inputs"]["telemetry_scope"] == "fleet"
    assert [p["component"] for p in body["inputs"]["processes"]] == ["consumer"]  # not itself
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT component FROM ops_metric_snapshots ORDER BY 1"))
        assert [r[0] for r in rows] == ["api", "consumer"]


def test_health_api_without_a_database_says_it_sees_one_process() -> None:
    with TestClient(create_app(Settings())) as client:  # lifespan without a publisher
        body = client.get("/v1/ops/health").json()
    assert body["inputs"] == {"telemetry_scope": "process"}


def test_monitor_without_a_reader_keeps_the_process_scope() -> None:
    monitor = OpsMonitor(load_ops_config(), IncidentManager(MemoryIncidentStore(), LogNotifier()))
    assert monitor.tick().health.inputs == {}
