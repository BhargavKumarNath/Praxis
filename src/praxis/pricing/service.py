"""Pricing cycle: problems -> optimiser -> audit record -> (execution).

For every product in the policy, exactly one decision is produced and recorded, including
UNAVAILABLE ones. The audit rule (required_test.md s12) is enforced here and in the store:

* a decision is ``executable`` only if its record was persisted in this cycle (or already
  existed with identical content), its status is CHANGE and the mode is ``execute``;
* if recording fails (database down, conflict), the decision is returned with
  ``recorded = False`` and is never executable;
* execution reads the decision back from the store (``DecisionStore.execute``).

Nothing here talks to an LLM: the cycle runs with or without the AI operator (ADR 0004).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from praxis.errors import TransientError
from praxis.observability import LatencySample
from praxis.pricing.config import Mode, PricingPolicy
from praxis.pricing.decision import Decision, Status, cycle_id
from praxis.pricing.inputs import ProblemSource
from praxis.pricing.optimiser import optimise, unavailable
from praxis.pricing.problem import Blocked
from praxis.pricing.store import DecisionStore, Execution, StoreError
from praxis.tracing import current_trace_id, trace_context

logger = logging.getLogger(__name__)


class PolicyError(RuntimeError):
    """The requested mode is not allowed by the policy."""


@dataclass(frozen=True)
class CycleItem:
    decision: Decision
    recorded: bool  # the audit record exists (inserted now, or identical already)
    executable: bool
    execution: Execution | None = None
    audit_error: str | None = None


@dataclass(frozen=True)
class CycleResult:
    cycle_id: str
    as_of: date
    mode: Mode
    policy_version: str
    items: tuple[CycleItem, ...]

    @property
    def audit_complete(self) -> bool:
        return all(i.recorded for i in self.items)

    def summary(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "audit_complete": self.audit_complete,
            "decisions": [
                {
                    "decision_id": i.decision.decision_id,
                    "product": i.decision.product,
                    "status": i.decision.status.value,
                    "current_price_micros": i.decision.current_price_micros,
                    "chosen_price_micros": i.decision.chosen_price_micros,
                    "reason_codes": list(i.decision.reason_codes),
                    "recorded": i.recorded,
                    "executable": i.executable,
                    "executed": i.execution is not None,
                    "audit_error": i.audit_error,
                }
                for i in self.items
            ],
        }


@dataclass
class PricingMetrics:
    """In-process counters + latency (Phase 11 exports these through OpenTelemetry)."""

    counters: Counter[str] = field(default_factory=Counter)
    latency: LatencySample = field(default_factory=LatencySample)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, item: CycleItem) -> None:
        with self._lock:
            self.counters["pricing_decisions_total"] += 1
            self.counters[f"pricing_decision_status_total:{item.decision.status.value}"] += 1
            for reason in item.decision.reason_codes:
                self.counters[f"pricing_decision_reason_total:{reason}"] += 1
            if not item.recorded:
                self.counters["pricing_audit_failures_total"] += 1
            if item.execution is not None:
                self.counters["pricing_executions_total"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(sorted(self.counters.items())),
                "latency": self.latency.summary(),
            }


class PricingService:
    def __init__(
        self,
        policy: PricingPolicy,
        source: ProblemSource,
        store: DecisionStore,
        *,
        metrics: PricingMetrics | None = None,
        executor: str = "pricing-service",
    ) -> None:
        self.policy = policy
        self.source = source
        self.store = store
        self.metrics = metrics or PricingMetrics()
        self.executor = executor

    def run_cycle(self, as_of: date, mode: Mode | None = None) -> CycleResult:
        """Decide and record every product once; execute only in execute mode."""
        mode = mode or self.policy.policy.mode
        if mode is Mode.EXECUTE and not self.policy.policy.allow_execute:
            raise PolicyError("execute mode is disabled by the policy (allow_execute = false)")
        if current_trace_id() is not None:
            return self._run(as_of, mode)
        with trace_context():  # one trace per cycle, carried by every decision record
            return self._run(as_of, mode)

    def _run(self, as_of: date, mode: Mode) -> CycleResult:
        started = time.perf_counter()
        problems = self.source.problems(as_of)
        items = []
        for product in self.policy.products:
            problem = problems.get(product) or Blocked(
                product, "input_unavailable", "no problem built for product"
            )
            if isinstance(problem, Blocked):
                decision = unavailable(problem, as_of, self.policy, mode)
            else:
                decision = optimise(problem, self.policy, mode)
            items.append(self._commit(decision, mode))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.latency.add(elapsed_ms)
        result = CycleResult(
            cycle_id(self.policy.policy.name, mode, as_of),
            as_of,
            mode,
            self.policy.version,
            tuple(items),
        )
        logger.info(
            "pricing.cycle",
            extra={
                "cycle_id": result.cycle_id,
                "policy_version": self.policy.version,
                "audit_complete": result.audit_complete,
                "latency_ms": round(elapsed_ms, 3),
            },
        )
        return result

    def _commit(self, decision: Decision, mode: Mode) -> CycleItem:
        trace_id = current_trace_id()
        try:
            self.store.record(decision, trace_id=trace_id)
        except (StoreError, TransientError) as exc:
            item = CycleItem(decision, False, False, audit_error=f"{type(exc).__name__}: {exc}")
            self._log(item, trace_id)
            return item
        executable = decision.status is Status.CHANGE and mode is Mode.EXECUTE
        execution = None
        error = None
        if executable:
            try:
                execution = self.store.execute(decision.decision_id, self.executor)
            except (StoreError, TransientError) as exc:
                error = f"execution failed: {type(exc).__name__}: {exc}"
        item = CycleItem(decision, True, executable, execution, error)
        self._log(item, trace_id)
        return item

    def _log(self, item: CycleItem, trace_id: str | None) -> None:
        self.metrics.record(item)
        d = item.decision
        logger.info(
            "pricing.decision",
            extra={
                "decision_id": d.decision_id,
                "trace_id": trace_id,
                "product": d.product,
                "status": d.status.value,
                "reason_codes": list(d.reason_codes),
                "policy_version": d.policy_version,
                "forecast_model_version": d.lineage.get("forecast_model_version"),
                "elasticity_model_version": d.lineage.get("elasticity_model_version"),
                "current_price_micros": d.current_price_micros,
                "chosen_price_micros": d.chosen_price_micros,
                "recorded": item.recorded,
                "executed": item.execution is not None,
                "audit_error": item.audit_error,
            },
        )
