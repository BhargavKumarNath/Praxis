"""Health report sections, HTML rendering, the read-only ops endpoints, and KPI signals."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.ops.config import OpsConfig, load_ops_config
from praxis.ops.evaluate import Alert
from praxis.ops.health import SECTIONS, build_health, render_html
from praxis.ops.incidents import IncidentManager, MemoryIncidentStore, RecordingNotifier
from praxis.ops.monitor import OpsMonitor
from praxis.ops.signals import kpi_deviation, record_kpi, sample_backlog
from praxis.telemetry import MetricsView, Telemetry

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg() -> OpsConfig:
    return load_ops_config()


def test_empty_system_is_no_data_not_healthy(cfg: OpsConfig) -> None:
    report = build_health(cfg, MetricsView(), now=NOW)
    assert report.status == "no_data" and [s.name for s in report.sections] == list(SECTIONS)


def test_dashboard_shows_service_data_model_and_business_health(
    cfg: OpsConfig, tel: Telemetry
) -> None:
    tel.inc("http_requests_total", 30, route="/x", method="GET", status=200)
    tel.set("data_source_age_s", 3600, source="eia")
    tel.set("model_calibration_80", 0.62, model="demand_forecast")  # model section degrades
    tel.inc("pricing_decisions_total", 4, status="hold")
    report = build_health(cfg, tel.snapshot(), now=NOW)
    status = {s.name: s.status for s in report.sections}
    assert status == {"service": "ok", "data": "ok", "model": "degraded", "business": "ok"}
    assert report.status == "degraded"
    model = next(s for s in report.sections if s.name == "model")
    assert [a.rule_id for a in model.alerts] == ["calibration_regression"]
    assert model.alerts[0].runbook_path.endswith("severe-forecast-error.md")
    payload = report.as_dict()
    assert payload["sections"]["model"]["alerts"][0]["runbook"].startswith("docs/runbooks/")
    assert payload["data_origin"] == "synthetic"


def test_html_is_escaped_and_lists_alerts_and_incidents(cfg: OpsConfig, tel: Telemetry) -> None:
    tel.set("model_calibration_80", 0.5, model="demand_forecast")
    store = MemoryIncidentStore()
    mgr = IncidentManager(store, RecordingNotifier())
    mgr.process(
        [Alert("<script>x</script>", "model", "page", "<b>s</b>", "stale-model", 1, 1)], NOW
    )
    html = render_html(build_health(cfg, tel.snapshot(), store.live(), now=NOW))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "calibration_regression" in html and "docs/runbooks/severe-forecast-error.md" in html
    assert "Incidents" in html and "synthetic" in html


def test_ops_endpoints_serve_health_dashboard_and_incidents(tel: Telemetry) -> None:
    store = MemoryIncidentStore()
    IncidentManager(store, RecordingNotifier()).process(
        [Alert("queue_backlog_high", "service", "page", "s", "pubsub-backlog", 5000, 1000)], NOW
    )
    client = TestClient(create_app(Settings(), incident_store=store))
    health = client.get("/v1/ops/health").json()
    assert set(health["sections"]) == set(SECTIONS) and len(health["incidents"]) == 1
    assert "Praxis health" in client.get("/ops/dashboard").text
    body = client.get("/v1/ops/incidents?limit=5").json()
    assert body["incidents"][0]["rule_id"] == "queue_backlog_high"


def test_incidents_endpoint_is_503_without_a_store() -> None:
    resp = TestClient(create_app(Settings())).get("/v1/ops/incidents")
    assert resp.status_code == 503 and resp.json()["error"] == "incidents_unavailable"


def test_monitor_tick_turns_a_firing_alert_into_one_incident(
    cfg: OpsConfig, tel: Telemetry
) -> None:
    mgr = IncidentManager(MemoryIncidentStore(), RecordingNotifier(), clear_after=1)
    monitor = OpsMonitor(cfg, mgr)
    tel.set("price_change_fraction", 0.9)
    assert len(monitor.tick(NOW).incidents.opened) == 1
    assert len(monitor.tick(NOW).incidents.touched) == 1
    tel.set("price_change_fraction", 0.1)
    assert len(monitor.tick(NOW).incidents.resolved) == 1


def test_kpi_deviation_uses_the_median_baseline(tel: Telemetry) -> None:
    history = [100.0] * 6 + [1000.0]  # one earlier shock does not move the median
    assert kpi_deviation(70.0, history) == pytest.approx(-0.3)
    assert kpi_deviation(70.0, history[:3]) is None  # not enough history
    assert kpi_deviation(5.0, [0.0] * 7) is None  # zero baseline
    assert record_kpi(tel, "revenue", 130.0, [100.0] * 7) == pytest.approx(0.3)
    assert tel.snapshot().peak("business_kpi_deviation", kpi="revenue") == pytest.approx(0.3)
    assert record_kpi(tel, "x", 1.0, []) is None


def test_sample_backlog_sets_both_gauges(tel: Telemetry) -> None:
    sample_backlog(tel, ["a", "b"], lambda s: {"a": 3, "b": 7}[s], lambda s: 1 if s == "a" else 0)
    view = tel.snapshot()
    assert view.peak("queue_backlog", subscription="b") == 7
    assert view.peak("dlq_backlog", subscription="a") == 1
