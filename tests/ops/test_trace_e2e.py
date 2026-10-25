"""required_test s17 Tracing: one synthetic transaction is a single trace across all stages."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from praxis.events.codec import DecodedEvent
from praxis.forecasting.artifact import ForecastArtifact
from praxis.pricing.problem import Blocked, PricingProblem
from praxis.pricing.service import PricingService
from praxis.pricing.store import MemoryDecisionStore
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.producer import EventProducer
from praxis.streaming.runtime import ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import OPERATIONAL, build_topology
from praxis.streaming.transport import Delivery, Disposition
from praxis.telemetry import Telemetry, trace_id_hex
from tests.forecasting.test_service import service
from tests.pricing.helpers import AS_OF, open_policy, single_segment
from tests.streaming.helpers import customer_lifecycle

TOPO = build_topology()
SUB = TOPO.by_role(OPERATIONAL)
STAGES = [
    "event.publish",  # ingestion
    "event.consume",  # bus -> consumer
    "model.inference",
    "optimiser.optimise",
    "persistence.record",
]


def test_a_synthetic_transaction_is_one_trace_end_to_end(
    artifact: ForecastArtifact, tel: Telemetry
) -> None:
    forecaster = service(artifact)
    store = MemoryDecisionStore()

    class Source:
        """Problem source that consults the real forecaster, as the production one does."""

        def problems(self, as_of: date) -> dict[str, PricingProblem | Blocked]:
            forecaster.forecast()
            return {"api_requests": single_segment(-0.5, 10_000.0, 100_000)}

    pricing = PricingService(open_policy(), Source(), store)
    results: list[str] = []

    class Decider:
        name = "decider"

        def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
            cycle = pricing.run_cycle(AS_OF)
            results.append(cycle.items[0].decision.decision_id)
            return "decided"

    broker = MemoryBroker(TOPO)
    event = customer_lifecycle()[0]
    EventProducer(broker, TOPO.events_topic).publish([event])  # ingestion -> event bus
    worker = ConsumerWorker(
        Decider(), DeadLetterRouter(broker, TOPO.dead_letter_topic), StreamMetrics()
    )
    assert worker.process(broker.pull(SUB.name)) == [Disposition.ACK]  # consumer

    by_trace = defaultdict(list)
    for span in tel.finished_spans():
        by_trace[trace_id_hex(span)].append(span)
    trace = by_trace[event["trace_id"]]  # the EVENT's own trace id carries the whole flow
    names = [s.name for s in trace]
    for stage in STAGES:
        assert stage in names, f"missing stage {stage}: {names}"
    assert len(by_trace) == 1  # nothing escaped into another trace

    # the decision record carries the same trace id, so the audit row joins the trace
    (decision_id,) = results
    assert store.get_record(decision_id) is not None
    # parent/child order: consume > pricing.cycle > (model, optimiser, persistence)
    ids = {s.name: s.get_span_context().span_id for s in trace}  # type: ignore[union-attr]
    cycle = next(s for s in trace if s.name == "pricing.cycle")
    assert cycle.parent is not None and cycle.parent.span_id == ids["event.consume"]
    for child in ("model.inference", "optimiser.optimise", "persistence.record"):
        span = next(s for s in trace if s.name == child)
        assert span.parent is not None
        assert span.parent.span_id in {ids["pricing.cycle"]}


def test_failed_stage_marks_its_span_without_leaking_the_message(tel: Telemetry) -> None:
    class Failing:
        name = "failing"

        def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
            raise RuntimeError("api_key=sk_live_secret")

    broker = MemoryBroker(TOPO)
    EventProducer(broker, TOPO.events_topic).publish([customer_lifecycle()[0]])
    worker = ConsumerWorker(
        Failing(), DeadLetterRouter(broker, TOPO.dead_letter_topic), StreamMetrics()
    )
    assert worker.process(broker.pull(SUB.name)) == [Disposition.NACK]
    span = next(s for s in tel.finished_spans() if s.name == "event.consume")
    assert span.status.description == "RuntimeError"
    assert "sk_live_secret" not in repr(span.status) + repr(span.events) + repr(span.attributes)
