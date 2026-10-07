"""Pricing cycle: one recorded decision per product; executable only from a persisted record."""

from __future__ import annotations

import logging
from datetime import date

import pytest
from sqlalchemy import Engine

from praxis.errors import TransientError
from praxis.pricing.config import Mode, PricingPolicy
from praxis.pricing.decision import Decision, Status
from praxis.pricing.problem import Blocked, PricingProblem
from praxis.pricing.service import PolicyError, PricingService
from praxis.pricing.store import (
    DecisionStore,
    Execution,
    MemoryDecisionStore,
    PostgresDecisionStore,
    RecordConflict,
)
from praxis.tracing import trace_context
from tests.pricing.helpers import AS_OF, open_policy, single_segment

PRODUCTS = {"api_requests": {"floor_micros": 1, "ceiling_micros": 10**9},
            "gpu_minutes": {"floor_micros": 1, "ceiling_micros": 10**9}}  # fmt: skip


def two_product_policy(mode: Mode = Mode.SHADOW, allow_execute: bool = True) -> PricingPolicy:
    base = open_policy(max_step=0.05)
    base = base.model_copy(
        update={"products": dict.fromkeys(PRODUCTS, base.products["api_requests"])}
    )
    return base.with_mode(mode, allow_execute=allow_execute)


class Fixed:
    """A problem source returning prepared problems."""

    def __init__(self, problems: dict[str, PricingProblem | Blocked]) -> None:
        self.problems_ = problems
        self.calls: list[date] = []

    def problems(self, as_of: date) -> dict[str, PricingProblem | Blocked]:
        self.calls.append(as_of)
        return self.problems_


def inputs() -> Fixed:
    raise_price = single_segment(-0.5, 10_000.0, 100_000)  # inelastic: CHANGE (+5%)
    return Fixed(
        {
            "api_requests": raise_price,
            "gpu_minutes": Blocked("gpu_minutes", "cost_unavailable", "eu_west", 45_000),
        }
    )


class FailingStore(MemoryDecisionStore):
    def __init__(self, error: Exception, fail_execute: bool = False) -> None:
        super().__init__()
        self.error, self.fail_execute = error, fail_execute

    def record(self, decision: Decision, *, trace_id: str | None = None) -> bool:
        if not self.fail_execute:
            raise self.error
        return super().record(decision, trace_id=trace_id)

    def execute(self, decision_id: str, executor: str) -> Execution:
        raise self.error


def test_shadow_cycle_records_every_product_and_never_executes() -> None:
    store = MemoryDecisionStore()
    result = PricingService(two_product_policy(), inputs(), store).run_cycle(AS_OF)
    assert result.mode is Mode.SHADOW and result.audit_complete
    by_product = {i.decision.product: i for i in result.items}
    assert by_product["api_requests"].decision.status is Status.CHANGE
    assert by_product["gpu_minutes"].decision.status is Status.UNAVAILABLE
    assert by_product["gpu_minutes"].decision.reason_codes == ("cost_unavailable",)
    assert by_product["gpu_minutes"].decision.current_price_micros == 45_000
    assert all(i.recorded and not i.executable and i.execution is None for i in result.items)
    assert store.executions() == []
    for i in result.items:
        assert store.get_record(i.decision.decision_id) == i.decision.record()
    s = result.summary()
    assert s["audit_complete"] and len(s["decisions"]) == 2


def test_execute_mode_executes_recorded_changes_only() -> None:
    store = MemoryDecisionStore()
    pol = two_product_policy(Mode.EXECUTE)
    result = PricingService(pol, inputs(), store).run_cycle(AS_OF)
    by_product = {i.decision.product: i for i in result.items}
    api = by_product["api_requests"]
    assert api.recorded and api.executable and api.execution is not None
    assert api.execution.to_price_micros == api.decision.chosen_price_micros
    assert not by_product["gpu_minutes"].executable
    # re-running the same cycle is idempotent: same decisions, no second execution
    again = PricingService(pol, inputs(), store).run_cycle(AS_OF)
    assert [i.decision.decision_id for i in again.items] == [
        i.decision.decision_id for i in result.items
    ]
    assert len(store.executions()) == 1


def test_recommend_mode_waits_for_approval() -> None:
    store = MemoryDecisionStore()
    result = PricingService(two_product_policy(Mode.RECOMMEND), inputs(), store).run_cycle(AS_OF)
    api = next(i for i in result.items if i.decision.product == "api_requests")
    assert api.recorded and not api.executable and api.execution is None
    store.approve(api.decision.decision_id, "pricing-lead")
    assert store.execute(api.decision.decision_id, "pricing-service").to_price_micros > 0


@pytest.mark.parametrize("error", [TransientError("db down"), RecordConflict("other content")])
def test_a_decision_that_could_not_be_recorded_is_never_executable(error: Exception) -> None:
    store = FailingStore(error)
    service = PricingService(two_product_policy(Mode.EXECUTE), inputs(), store)
    result = service.run_cycle(AS_OF)
    assert not result.audit_complete
    assert all(not i.recorded and not i.executable and i.execution is None for i in result.items)
    assert all(i.audit_error and type(error).__name__ in i.audit_error for i in result.items)
    assert service.metrics.snapshot()["counters"]["pricing_audit_failures_total"] == 2


def test_execution_failure_is_reported_not_raised() -> None:
    store = FailingStore(TransientError("db down"), fail_execute=True)
    result = PricingService(two_product_policy(Mode.EXECUTE), inputs(), store).run_cycle(AS_OF)
    api = next(i for i in result.items if i.decision.product == "api_requests")
    assert api.recorded and api.executable and api.execution is None
    assert api.audit_error is not None and "execution failed" in api.audit_error


def test_execute_mode_needs_the_policy_switch() -> None:
    pol = two_product_policy(Mode.SHADOW, allow_execute=False)
    with pytest.raises(PolicyError):
        PricingService(pol, inputs(), MemoryDecisionStore()).run_cycle(AS_OF, Mode.EXECUTE)


def test_a_product_without_any_input_is_still_decided() -> None:
    result = PricingService(two_product_policy(), Fixed({}), MemoryDecisionStore()).run_cycle(AS_OF)
    assert [i.decision.status for i in result.items] == [Status.UNAVAILABLE] * 2
    assert all(i.decision.reason_codes == ("input_unavailable",) for i in result.items)


def test_metrics_logs_and_trace(caplog: pytest.LogCaptureFixture) -> None:
    service = PricingService(two_product_policy(), inputs(), MemoryDecisionStore())
    caplog.set_level(logging.INFO, logger="praxis.pricing.service")
    with trace_context() as (trace_id, _cid):
        service.run_cycle(AS_OF)
    counters = service.metrics.snapshot()["counters"]
    assert counters["pricing_decisions_total"] == 2
    assert counters["pricing_decision_status_total:change"] == 1
    assert counters["pricing_decision_reason_total:cost_unavailable"] == 1
    logged = [r for r in caplog.records if r.getMessage() == "pricing.decision"]
    assert {getattr(r, "trace_id", None) for r in logged} == {trace_id}
    assert all(getattr(r, "policy_version", "").startswith("policy-") for r in logged)
    service.run_cycle(date(2026, 7, 27))  # without an ambient trace, the cycle opens one
    assert service.metrics.snapshot()["latency"]["count"] == 2


@pytest.mark.integration
def test_postgres_cycle_with_execution(pg_engine: Engine) -> None:
    store: DecisionStore = PostgresDecisionStore(pg_engine)
    result = PricingService(two_product_policy(Mode.EXECUTE), inputs(), store).run_cycle(AS_OF)
    assert result.audit_complete
    executed = store.executions("api_requests")
    assert len(executed) == 1 and executed[0].executed_by == "pricing-service"
