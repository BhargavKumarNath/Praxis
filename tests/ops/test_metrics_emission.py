"""required_test s17 Metrics: each named metric is emitted by the real code path."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.events.codec import DecodedEvent, encode
from praxis.forecasting.artifact import ForecastArtifact
from praxis.ops.signals import sample_backlog
from praxis.pricing.decision import Status
from praxis.pricing.problem import Blocked, PricingProblem
from praxis.pricing.service import PricingService
from praxis.pricing.store import MemoryDecisionStore
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.runtime import ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import OPERATIONAL, build_topology
from praxis.streaming.transport import Delivery, Disposition
from praxis.telemetry import Telemetry
from tests.forecasting.test_service import service
from tests.pricing.helpers import AS_OF, open_policy, single_segment
from tests.streaming.helpers import customer_lifecycle

TOPO = build_topology()
SUB = TOPO.by_role(OPERATIONAL)


def test_request_count_errors_and_latency(tel: Telemetry) -> None:
    app = create_app(Settings())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/healthz")
    client.get("/healthz")
    client.get("/boom")
    view = tel.snapshot()
    assert view.total("http_requests_total", route="/healthz", status="200") == 2
    assert view.total("http_requests_total", route="/boom", status="500") == 1
    assert view.total("http_server_errors_total") == 1  # only the 5xx
    assert view.count("http_request_duration_ms", route="/healthz") == 2
    assert view.quantile("http_request_duration_ms", 0.95, route="/healthz") is not None


def test_unmatched_routes_do_not_explode_label_cardinality(tel: Telemetry) -> None:
    client = TestClient(create_app(Settings()))
    for i in range(5):
        client.get(f"/no/such/path/{i}")
    assert {p.labels["route"] for p in tel.snapshot().select("http_requests_total")} == {
        "unmatched"
    }


class _Ok:
    name = "ok"

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        return "applied"


def test_event_lag_consumer_outcomes_and_queue_backlog(tel: Telemetry) -> None:
    broker = MemoryBroker(TOPO)
    worker = ConsumerWorker(
        _Ok(), DeadLetterRouter(broker, TOPO.dead_letter_topic), StreamMetrics()
    )
    body, attrs = encode(customer_lifecycle()[0])
    attrs["praxis_sent_at"] = f"{datetime.now(UTC).timestamp():.6f}"
    broker.publish(TOPO.events_topic, body, attrs)
    broker.publish(TOPO.events_topic, b"not json", {"stateful": "true"})
    sample_backlog(tel, [SUB.name], broker.backlog, lambda _s: 0)
    assert tel.snapshot().peak("queue_backlog", subscription=SUB.name) == 2
    dispositions = worker.process(broker.pull(SUB.name))
    assert dispositions.count(Disposition.ACK) >= 1
    view = tel.snapshot()
    assert view.total("events_consumed_total", subscription=SUB.name, outcome="deliveries") == 2
    assert view.total("events_consumed_total", subscription=SUB.name, outcome="status_applied") == 1
    assert view.count("event_lag_ms", subscription=SUB.name) == 1


def test_model_version_and_inference_metrics(artifact: ForecastArtifact, tel: Telemetry) -> None:
    service(artifact).forecast()
    view = tel.snapshot()
    versions = view.select("model_version_info", model="demand_forecast")
    assert [p.labels["version"] for p in versions] == [artifact.model_version]
    assert view.total("model_inference_total", outcome="served") == 1
    assert view.count("model_inference_duration_ms") == 1
    assert view.peak("forecast_age_hours", model="demand_forecast") == 24.0


class _Source:
    def problems(self, as_of: object) -> dict[str, PricingProblem | Blocked]:
        return {"api_requests": single_segment(-0.5, 10_000.0, 100_000, sd=1.0)}  # too uncertain


def test_decision_and_frozen_decision_counts(tel: Telemetry) -> None:
    result = PricingService(open_policy(), _Source(), MemoryDecisionStore()).run_cycle(AS_OF)
    assert [i.decision.status for i in result.items] == [Status.FROZEN]
    view = tel.snapshot()
    assert view.total("pricing_decisions_total", status="frozen") == 1
    assert view.total("pricing_frozen_decisions_total") >= 1
    assert view.count("pricing_decision_duration_ms") == 1
    assert view.peak("price_change_fraction") == 0.0


def test_drift_monitor_gauges(tel: Telemetry) -> None:
    from praxis.mlops.pipeline import _emit_model_health

    _emit_model_health({"alarms": ["residual_drift"], "residual": {"coverage_80": 0.66}})
    _emit_model_health({"alarms": [], "residual": {"status": "insufficient_data"}})
    view = tel.snapshot()
    assert view.peak("model_drift_alarm", model="demand_forecast") == 0.0  # last value wins
    assert view.peak("model_calibration_80", model="demand_forecast") == 0.66  # not overwritten
