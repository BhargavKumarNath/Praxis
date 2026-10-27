"""Console read API contracts (required_test.md s19): every view reads real state, marks stale
and missing inputs instead of showing 0, labels synthetic / external / shadow data, renders
decision details and model versions, and fails safely."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine

from praxis.control.metric_snapshots import MetricSnapshotStore
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryStore
from praxis.pricing.store import PostgresDecisionStore
from praxis.telemetry import Telemetry
from tests.console.conftest import Console, make_console
from tests.console.journey import run_journey
from tests.mlops.helpers import mlops_config
from tests.mlops.test_registry import A, with_champion
from tests.pricing.test_store import change

pytestmark = pytest.mark.integration

VIEWS = ("executive", "pricing", "payments", "mlops", "system", "operator")
STATES = {"ok", "no_data", "unavailable"}


def get(console: Console, path: str) -> dict[str, Any]:
    response = console.client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["X-Correlation-ID"]
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("view", VIEWS)
def test_every_panel_states_its_source_and_never_hides_a_gap(console: Console, view: str) -> None:
    body = get(console, f"/v1/console/{view}")
    assert body["view"] == view
    assert body["data_origin"] == "synthetic" and "SYNTHETIC" in body["note"].upper()
    assert body["panels"]
    for name, p in body["panels"].items():
        assert p["status"] in STATES, name
        assert p["source"], name
        assert isinstance(p["synthetic"], bool)
        if p["status"] != "ok":
            assert p["data"] is None and p["reason"], name  # never a silent 0 or blank


def test_empty_fleet_reports_missing_metrics_with_their_feed(console: Console) -> None:
    signals = get(console, "/v1/console/system")["panels"]["signals"]["data"]
    backlog = signals["queue_backlog_max"]
    assert backlog["value"] is None
    assert "Pub/Sub emulator exposes no backlog" in backlog["reason"]
    assert signals["event_lag_p95_ms"]["value"] is None
    assert "event consumers" in signals["event_lag_p95_ms"]["reason"]
    assert signals["api_error_rate"]["value"] is None
    assert "API and the console" in signals["api_error_rate"]["reason"]


def test_an_empty_dead_letter_table_read_by_the_collector_is_zero(console: Console) -> None:
    # The collector answered and found no open dead letter: that is a measured 0, not a gap.
    body = get(console, "/v1/console/system")
    assert body["inputs"]["collectors"]["dead_letters"]["status"] == "ok"
    dlq = body["panels"]["signals"]["data"]["dlq_backlog_max"]
    assert dlq["value"] == 0.0 and dlq["reason"] is None


def test_dead_letter_backlog_is_missing_when_its_collector_did_not_run() -> None:
    from praxis.console.system import signals
    from praxis.ops.system import SystemSnapshot
    from praxis.telemetry import MetricsView

    empty = SystemSnapshot(MetricsView(), "fleet", collectors={"dead_letters": {"status": "error"}})
    dlq = signals(empty)["dlq_backlog_max"]
    assert dlq["value"] is None and "dlq_backlog" in dlq["reason"]


def test_payments_without_cases_say_where_cases_come_from(console: Console) -> None:
    summary = get(console, "/v1/console/payments")["panels"]["summary"]
    assert summary["status"] == "no_data"
    assert "make stripe-verify" in summary["reason"]
    assert "not in `python -m praxis.streaming run-local`" in summary["reason"]


def _publish(engine: Engine, component: str, now: datetime, ttl: timedelta) -> None:
    tel = Telemetry()
    tel.observe("http_request_duration_ms", 42.0, route="/x")
    tel.inc("http_requests_total", route="/x", method="GET", status=200)
    tel.observe("event_lag_ms", 120.0, subscription="ingest")
    MetricSnapshotStore(engine).publish(
        component, f"{component}-1", tel.snapshot(), now=now, ttl=ttl, started_at=now
    )


def test_fleet_metrics_are_read_and_stale_processes_are_named(console: Console) -> None:
    now = datetime.now(UTC)
    _publish(console.engine, "consumer-ingest", now, timedelta(minutes=5))
    _publish(console.engine, "consumer-old", now - timedelta(hours=2), timedelta(minutes=1))
    body = get(console, "/v1/console/system")
    signals = body["panels"]["signals"]["data"]
    assert signals["event_lag_p95_ms"]["value"] is not None
    assert signals["api_error_rate"]["value"] == 0.0
    assert "consumer-old" in body["inputs"]["stale_processes"]
    assert "consumer-ingest" not in body["inputs"]["stale_processes"]
    assert body["inputs"]["telemetry_scope"] == "fleet"


def test_cost_is_an_estimate_with_its_sources(console: Console) -> None:
    cost = get(console, "/v1/console/system")["panels"]["cost"]
    assert cost["status"] == "ok" and cost["synthetic"] is False
    data = cost["data"]
    assert data["kind"] == "estimate" and "not a metered cost" in data["label"]
    assert data["sources"] and data["as_of"]
    assert "Cost if the Terraform stack were applied" in data["not_estimated"]


def test_missing_warehouse_is_unavailable_with_a_reason(console: Console) -> None:
    sources = get(console, "/v1/console/system")["panels"]["sources"]
    assert sources["status"] == "unavailable"
    assert sources["reason"] == "not configured: the warehouse file does not exist"
    executive = get(console, "/v1/console/executive")["panels"]
    assert {p["status"] for p in executive.values()} == {"unavailable"}


def test_operator_view_says_the_investigator_is_not_an_llm(console: Console) -> None:
    body = get(console, "/v1/console/operator")
    provider = body["panels"]["provider"]["data"]
    assert provider["provider"] == "deterministic-triage" and provider["is_llm"] is False
    assert "not an LLM" in provider["note"] and "not an LLM" in body["note"]


def test_incident_links_its_investigation_trace_and_pending_request(console: Console) -> None:
    journey = run_journey(console.engine)
    body = get(console, "/v1/console/operator")
    incidents = body["panels"]["incidents"]["data"]["incidents"]
    assert [i["rule_id"] for i in incidents] == ["price_volatility"]
    request = body["panels"]["requests"]["data"]["requests"][0]
    assert request["status"] == "pending" and request["kind"] == "freeze_pricing"

    detail = get(console, f"/v1/console/incidents/{journey.incident_id}")
    (trace,) = detail["investigations"]
    assert trace["session"]["session_id"] == journey.session_id
    assert trace["session"]["diagnosis"] == "price_volatility"
    assert trace["session"]["grounded"] is True
    tools = [c["tool"] for c in trace["calls"]]
    assert tools[:2] == ["get_system_health", "get_incident"] and "freeze_pricing" in tools
    assert all(c["received_at"] and c["status"] for c in trace["calls"])  # audit complete
    assert [r["request_id"] for r in trace["requests"]] == [journey.request_id]
    assert detail["events"][0]["kind"] == "opened"

    session = get(console, f"/v1/console/sessions/{journey.session_id}")
    assert session["calls"] == trace["calls"]


@pytest.mark.parametrize(
    "path",
    ["/v1/console/incidents/inc_nope", "/v1/console/sessions/ses_nope",
     "/v1/console/pricing/decisions/dec-nope"],
)  # fmt: skip
def test_unknown_detail_is_404_with_correlation_id(console: Console, path: str) -> None:
    response = console.client.get(path)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found" and body["correlation_id"]


def test_decision_detail_renders_the_recorded_audit(console: Console) -> None:
    decision = change()
    PostgresDecisionStore(console.engine).record(decision, trace_id="t" * 32)
    view = get(console, "/v1/console/pricing")
    assert view["shadow"] is True and "not validated for execution" in view["note"]
    (latest,) = view["panels"]["latest"]["data"]["products"]
    assert latest["decision_id"] == decision.decision_id
    assert latest["chosen_price_micros"] == decision.chosen_price_micros

    detail = get(console, f"/v1/console/pricing/decisions/{decision.decision_id}")
    record = detail["decision"]
    assert record["reason_codes"] == list(decision.reason_codes)
    assert record["constraints"]["max_step"] == decision.constraints["max_step"]
    assert record["guardrails"]["checks"] and record["lineage"] == dict(decision.lineage)
    assert len(record["candidates"]) == len(decision.candidates)
    assert detail["record_sha256"] == decision.record_sha256 and detail["execution"] is None
    assert "not validated for execution" in detail["note"]


def test_an_executed_decision_shows_who_executed_it(console: Console) -> None:
    decision = change()
    store = PostgresDecisionStore(console.engine)
    store.record(decision)
    store.execute(decision.decision_id, "owner")
    detail = get(console, f"/v1/console/pricing/decisions/{decision.decision_id}")
    assert detail["execution"]["executed_by"] == "owner"
    assert detail["execution"]["to_price_micros"] == decision.chosen_price_micros
    (latest,) = get(console, "/v1/console/pricing")["panels"]["latest"]["data"]["products"]
    assert latest["executed_by"] == "owner"


def test_model_versions_and_champion_are_visible(console: Console) -> None:
    registry = ModelRegistry(RegistryStore(console.engine), mlops_config())
    with_champion(registry)
    data = get(console, "/v1/console/mlops")["panels"]
    versions = data["versions"]["data"]
    assert versions["champions"] == {registry.model_name: A}
    (row,) = versions["versions"]
    assert row["model_version"] == A and row["stage"] == "champion"
    assert row["validation"]["passed"] is True and row["code_revision"]
    events = [e["to_stage"] for e in data["events"]["data"]["events"]]
    assert "champion" in events
    assert data["serving"]["data"]["calibration_80"]["value"] is None  # nothing published it


def test_store_outage_marks_panels_and_details_fail_safely(tmp_path: Path) -> None:
    dead = create_engine("postgresql+psycopg://praxis@127.0.0.1:1/none", pool_pre_ping=False)
    client = make_console(dead, tmp_path / "absent.duckdb")
    with client:
        body = client.get("/v1/console/pricing").json()
        assert {p["status"] for p in body["panels"].values()} == {"unavailable"}
        assert all(
            p["reason"] == "the control-plane database did not answer"
            for p in body["panels"].values()
        )
        detail = client.get("/v1/console/incidents/inc_x")
    assert detail.status_code == 503
    assert detail.json()["error"] == "store_unavailable" and detail.json()["correlation_id"]
    assert "psycopg" not in detail.text and "Traceback" not in detail.text


def test_unexpected_failure_returns_only_error_and_correlation_id(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_ctx: object) -> None:
        raise RuntimeError("secret internal detail at /srv/app.py line 3")

    monkeypatch.setattr("praxis.console.pricing._latest", boom)
    response = console.client.get("/v1/console/pricing")
    assert response.status_code == 500
    assert set(response.json()) == {"error", "correlation_id", "detail"}
    assert response.json()["error"] == "internal_error"
    assert "secret internal detail" not in response.text


def test_payments_view_reports_recovery_with_its_definition(console: Console) -> None:
    with console.engine.begin() as conn:
        from sqlalchemy import text

        for i, (state, rec) in enumerate([("recovered", True), ("closed", False),
                                          ("past_due", False)]):  # fmt: skip
            conn.execute(
                text(
                    "INSERT INTO dunning_cases (invoice_id, customer_id, provider, state, "
                    "amount_minor, currency, opened_at, attempts_seen, recovered_at) VALUES "
                    "(:i, 'c1', 'synthetic', :s, 1000, 'GBP', :o, 1, :r)"
                ),
                {"i": f"in_{i}", "s": state, "o": datetime(2026, 2, 1, tzinfo=UTC),
                 "r": datetime(2026, 2, 3, tzinfo=UTC) if rec else None},
            )  # fmt: skip
    panels = get(console, "/v1/console/payments")["panels"]
    (gbp,) = panels["summary"]["data"]["by_currency"]
    assert gbp["resolved_recovery_rate"] == 0.5 and gbp["open_cases"] == 1
    assert gbp["recovered_minor"] == 1000 and gbp["ttr_p50_hours"] == 48.0
    assert panels["open_cases"]["data"]["cases"][0]["invoice_id"] == "in_2"
    assert panels["retries"]["status"] == "no_data"
    assert date.fromisoformat(panels["daily"]["data"]["days"][0]["day"]) == date(2026, 2, 1)
