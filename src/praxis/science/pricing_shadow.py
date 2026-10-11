"""Shadow-mode evaluation of the pricing optimiser against simulator truth (Phase 6).

The optimiser runs in shadow mode on the pricing shadow world, one cycle per week, exactly as
it would in production (forecast service, evidence, marts, audit store). Nothing executes:
the world keeps its prices. Each decision is then scored with the simulator's OWN demand and
churn equations (``Engine.expected_demand`` / ``Engine.churn_hazard``, via a read-only
observer), evaluated at every candidate price the decision considered, with the world's
actual customers and service state on each day of the cycle. This is the only module that
joins pricing decisions with ground truth.

Also: an independent re-check of every change against its own record (constraint
compliance), audit completeness, forecast lineage, failure-injection ("stress") cycles that
must fail safe, and a naive baseline (point-estimate optimum, no constraints) for reference.
Acceptance: ``configs/pricing/shadow_acceptance.toml`` (pre-registered).

Phase 9 (ADR 0016): the decision values each extra churner at its segment's CLV (tier in force
at the decision), so truth does the same: true extra churners are counted per tier (the
simulator's current tier on each day) and valued at the CLV the decision recorded for that
tier. The valuation is the decision's; the churn and demand RESPONSES are the simulator's.
"""

from __future__ import annotations

import json
import math
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from praxis.customer.artifact import find_artifact as find_customer_artifact
from praxis.customer.artifact import load_artifact as load_customer_artifact
from praxis.customer.config import CustomerValueConfig
from praxis.customer.dataset import build_panel, snapshot_dates
from praxis.customer.warehouse import connect as connect_customers
from praxis.elasticity.config import ExperimentRegistry
from praxis.forecasting.artifact import ArtifactError, load_artifact
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.forecasting.warehouse import connect, load_panel
from praxis.pricing import constraints as cons
from praxis.pricing.config import Mode, PricingPolicy
from praxis.pricing.decision import Decision, Reason, Status
from praxis.pricing.evidence import PricingEvidence, load_evidence, prior_settings
from praxis.pricing.inputs import (
    CustomerSource,
    CutoffFeatureSource,
    Forecaster,
    WarehouseProblemSource,
    customer_factory,
    decision_clock,
    forecast_factory,
)
from praxis.pricing.objective import evaluate as evaluate_objective
from praxis.pricing.problem import (
    ChurnResponse,
    ChurnSegment,
    PricingProblem,
    RegionContext,
    SegmentBaseline,
    TierElasticity,
)
from praxis.pricing.service import PricingService
from praxis.pricing.store import MemoryDecisionStore
from praxis.simulator.config import SimulationConfig
from praxis.simulator.engine import DayView, Engine
from praxis.simulator.population import customer_id, generate_population

F64 = NDArray[np.float64]
DEFAULT_SHADOW = Path(__file__).resolve().parents[3] / "configs/pricing/shadow_acceptance.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Shadow(_Cfg):
    first_cycle_day: int = Field(ge=28)
    cycles: int = Field(ge=1)
    cycle_days: int = Field(ge=1, le=7)


class ShadowAcceptance(_Cfg):
    max_constraint_violations: int = Field(ge=0)
    max_shadow_executions: int = Field(ge=0)
    require_complete_records: bool
    require_forecast_lineage: bool
    min_total_true_delta: float
    min_direction_rate: float = Field(ge=0, le=1)
    min_changes_for_calibration: int = Field(ge=1)
    prediction_ratio_range: tuple[float, float]
    churn_guardrail_must_hold: bool
    stress_must_fail_safe: bool


class ShadowConfig(_Cfg):
    shadow: Shadow
    acceptance: ShadowAcceptance


def load_shadow_config(path: Path | None = None) -> ShadowConfig:
    return ShadowConfig.model_validate(
        tomllib.loads((path or DEFAULT_SHADOW).read_text(encoding="utf-8"))
    )


class ShadowError(RuntimeError):
    """The shadow world, warehouse and artifacts do not belong together."""


@dataclass(frozen=True)
class ShadowInputs:
    world: SimulationConfig
    seed: int
    warehouse: Path
    forecast_models: Path
    elasticity_model: Path
    elasticity_report: Path
    registry: ExperimentRegistry
    policy: PricingPolicy
    churn_response: Path
    customer_models: Path
    customer_config: CustomerValueConfig
    churn_archive: Path | None = None  # development leave-one-out override (nightly seed 1)

    def cycle_dates(self, cfg: Shadow) -> list[date]:
        start = self.world.run.start_date
        return [
            start + timedelta(days=cfg.first_cycle_day + k * cfg.cycle_days)
            for k in range(cfg.cycles)
        ]


# --------------------------------------------------------------------- lineage
def select_forecast_artifact(inputs: ShadowInputs, cfg: Shadow) -> tuple[Path | None, str]:
    """The artifact trained on exactly the world's first ``first_cycle_day`` days, if any."""
    start = inputs.world.run.start_date
    con = connect(inputs.warehouse)
    try:
        prefix = load_panel(con, start, start + timedelta(days=cfg.first_cycle_day - 1))
    finally:
        con.close()
    version = prefix.data_version()
    for manifest in sorted(inputs.forecast_models.glob("*/manifest.json")):
        if json.loads(manifest.read_text()).get("data_version") == version:
            return manifest.parent, version
    return None, version


def select_customer_artifact(inputs: ShadowInputs, cfg: Shadow) -> tuple[Path | None, str]:
    """The customer artifact trained on exactly the world's first ``first_cycle_day`` days."""
    start = inputs.world.run.start_date
    cutoff = start + timedelta(days=cfg.first_cycle_day)
    con = connect_customers(inputs.warehouse)
    try:
        dates = snapshot_dates(start, cutoff, inputs.customer_config.label)
        version = build_panel(con, dates, inputs.customer_config).data_version()
    finally:
        con.close()
    return find_customer_artifact(inputs.customer_models, version), version


# ---------------------------------------------------------------------- cycles
def _customers(inputs: ShadowInputs, artifact: Path) -> CustomerSource:
    return customer_factory(
        inputs.warehouse,
        lambda: load_customer_artifact(artifact),
        inputs.customer_config,
        inputs.policy,
    )


def _service(
    inputs: ShadowInputs,
    forecaster: Callable[[date], Forecaster],
    evidence: PricingEvidence | None,
    store: MemoryDecisionStore,
    customers: CustomerSource | None,
) -> PricingService:
    source = WarehouseProblemSource(
        policy=inputs.policy,
        warehouse=inputs.warehouse,
        forecaster=forecaster,
        evidence=evidence,
        customers=customers,
        store=store,
        evidence_error="evidence withheld (stress case)",
    )
    return PricingService(inputs.policy, source, store, executor="shadow-evaluation")


def run_cycles(
    inputs: ShadowInputs,
    cfg: Shadow,
    artifact: Path,
    evidence: PricingEvidence,
    customer_artifact: Path,
) -> tuple[list[Decision], int]:
    """Shadow decisions for every cycle, and the number of executions (must be 0)."""
    store = MemoryDecisionStore()
    forecaster = forecast_factory(
        lambda: load_artifact(artifact), WarehouseFeatureSource(inputs.warehouse)
    )
    service = _service(inputs, forecaster, evidence, store, _customers(inputs, customer_artifact))
    decisions = []
    for as_of in inputs.cycle_dates(cfg):
        result = service.run_cycle(as_of, Mode.SHADOW)
        decisions += [i.decision for i in result.items]
    return decisions, len(store.executions())


def _lagged(artifact: Path, warehouse: Path, lag_days: int) -> Callable[[date], ForecastService]:
    """Forecast service whose newest feature day is ``lag_days`` before the decision date."""
    source = WarehouseFeatureSource(warehouse)

    def make(as_of: date) -> ForecastService:
        return ForecastService(
            load_artifact(artifact),
            CutoffFeatureSource(source, as_of - timedelta(days=lag_days)),
            clock=decision_clock(as_of),
        )

    return make


def _broken(_as_of: date) -> ForecastService:
    raise ArtifactError("forecast artifact deleted (stress case)")


def run_stress(
    inputs: ShadowInputs,
    cfg: Shadow,
    artifact: Path,
    evidence: PricingEvidence,
    customer_artifact: Path,
) -> list[dict[str, Any]]:
    """Failure injection on the first cycle date: every case must end without a change."""
    as_of = inputs.cycle_dates(cfg)[0]
    loose = replace(
        evidence,
        tiers={t: TierElasticity(e.mean, e.sd * 20.0) for t, e in evidence.tiers.items()},
    )
    fresh = forecast_factory(
        lambda: load_artifact(artifact), WarehouseFeatureSource(inputs.warehouse)
    )
    customers = _customers(inputs, customer_artifact)
    cases: list[
        tuple[str, Callable[[date], Forecaster], PricingEvidence | None, CustomerSource | None, str]
    ] = [
        (
            "stale_features",
            _lagged(artifact, inputs.warehouse, 4),
            evidence,
            customers,
            Reason.FORECAST_STALE,
        ),
        (
            "features_too_old",
            _lagged(artifact, inputs.warehouse, 10),
            evidence,
            customers,
            Reason.FORECAST_UNAVAILABLE,
        ),
        ("forecast_model_unavailable", _broken, evidence, customers, Reason.MODEL_UNAVAILABLE),
        ("evidence_unavailable", fresh, None, customers, Reason.MODEL_UNAVAILABLE),
        ("inflated_uncertainty", fresh, loose, customers, Reason.UNCERTAINTY_ELASTICITY_SD),
        ("customer_model_unavailable", fresh, evidence, None, Reason.MODEL_UNAVAILABLE),
    ]
    out = []
    for name, forecaster, ev, cust, expected in cases:
        store = MemoryDecisionStore()
        result = _service(inputs, forecaster, ev, store, cust).run_cycle(as_of, Mode.SHADOW)
        statuses = [i.decision.status.value for i in result.items]
        reasons = [list(i.decision.reason_codes) for i in result.items]
        passed = (
            all(i.decision.status is not Status.CHANGE for i in result.items)
            and all(expected in r for r in reasons)
            and result.audit_complete
            and not store.executions()
        )
        out.append(
            {
                "case": name,
                "expected_reason": str(expected),
                "statuses": statuses,
                "reasons": reasons,
                "passed": passed,
            }
        )
    return out


# ----------------------------------------------------------------------- truth
@dataclass
class _Acc:
    prices: NDArray[np.int64]
    product: int
    loss: float
    contribution: F64
    extra_churn: F64  # (k,) all customers
    extra_by_tier: F64  # (k, T) per tier in force on the day
    exposed: float = 0.0
    days: int = 0


@dataclass
class TruthCollector:
    """Engine observer: true contribution and extra churn of each candidate price per day."""

    engine_ref: list[Engine] = field(default_factory=list)
    by_day: dict[int, list[_Acc]] = field(default_factory=lambda: defaultdict(list))
    tier_hazards: tuple[float, ...] = ()  # base hazard of each tier (identifies the tier)
    hazard_days: frozenset[int] = frozenset()  # days whose TRUE hazards are kept (Phase 9)
    hazards: dict[int, tuple[F64, NDArray[np.bool_]]] = field(default_factory=dict)

    def add(self, days: range, acc: _Acc) -> None:
        for d in days:
            self.by_day[d].append(acc)

    def __call__(self, view: DayView) -> None:
        engine = self.engine_ref[0]
        if view.day in self.hazard_days:
            self.hazards[view.day] = (
                engine.churn_hazard(view, view.prices).copy(),
                view.active.copy(),
            )
        items = self.by_day.get(view.day)
        if not items:
            return
        base_q = 1.0 - np.exp(-engine.churn_hazard(view, view.prices))
        active = view.active
        tiers = [active & (view.tier_hazard == h) for h in self.tier_hazards]
        for acc in items:
            p = acc.product
            cost = view.unit_cost[view.region, p]
            served = view.served_ratio[:, p]
            for j, price in enumerate(acc.prices.tolist()):
                alt = view.prices.copy()
                alt[:, p] = price
                lam = engine.expected_demand(view, alt)[:, p]
                acc.contribution[j] += float(
                    (lam * served * (price * (1.0 - acc.loss) - cost)).sum()
                )
                q = 1.0 - np.exp(-engine.churn_hazard(view, alt))
                acc.extra_churn[j] += float((q - base_q)[active].sum())
                for t, m in enumerate(tiers):
                    acc.extra_by_tier[j, t] += float((q - base_q)[m].sum())
            acc.exposed += float((active & (engine.pop.mix[:, p] > 0)).sum())
            acc.days += 1


def collect_truth(
    world: SimulationConfig,
    seed: int,
    plan: list[tuple[int, str, list[int], float]],
    cfg: Shadow,
    hazards_out: dict[int, tuple[F64, NDArray[np.bool_]]] | None = None,
) -> list[_Acc]:
    """``plan``: (cycle day, product, candidate prices, payment loss) -> one accumulator each.

    ``hazards_out`` (optional) receives the true voluntary hazard of every customer on each
    cycle day (the customer-model truth check).
    """
    products = [p.id for p in world.products]
    collector = TruthCollector(
        tier_hazards=tuple(t.churn_hazard_daily for t in world.tiers),
        hazard_days=frozenset(day for day, *_ in plan) if hazards_out is not None else frozenset(),
    )
    accs = []
    for day, product, prices, loss in plan:
        k = len(prices)
        acc = _Acc(
            np.array(prices, dtype=np.int64),
            products.index(product),
            loss,
            np.zeros(k),
            np.zeros(k),
            np.zeros((k, len(world.tiers))),
        )
        collector.add(range(day, day + cfg.cycle_days), acc)
        accs.append(acc)
    engine = Engine(world, seed, generate_population(world, seed), observer=collector)
    collector.engine_ref.append(engine)
    for _ in engine.run():
        pass
    if hazards_out is not None:
        hazards_out.update(collector.hazards)
    return accs


# -------------------------------------------------------------------- baseline
def problem_from_record(record: dict[str, Any]) -> PricingProblem:
    """Rebuild the optimiser's problem from an audit record (reproducibility + baselines)."""
    inp = record["inputs"]
    churn = inp["churn"]
    return PricingProblem(
        product=record["product"],
        as_of=date.fromisoformat(record["as_of"]),
        current_price_micros=int(record["current_price_micros"]),
        last_change=None if inp["last_change"] is None else date.fromisoformat(inp["last_change"]),
        anchor_price_micros=inp["anchor_price_micros"],
        evidence_date=None
        if inp["evidence_date"] is None
        else date.fromisoformat(inp["evidence_date"]),
        elasticity={t: TierElasticity(e["mean"], e["sd"]) for t, e in inp["elasticity"].items()},
        segments=tuple(
            SegmentBaseline(
                s["region"], s["tier"], s["demand"], s["demand_high"], s["served_share"]
            )
            for s in inp["segments"]
        ),
        regions={
            r: RegionContext(r, c["unit_cost_micros"], c["utilization"])
            for r, c in inp["regions"].items()
        },
        payment_loss_rate=inp["payment_loss_rate"],
        churn=ChurnResponse(
            churn["response"],
            churn["response_sd"],
            churn["response_upper"],
            churn["window_days"],
            tuple(
                ChurnSegment(
                    g["segment"],
                    g["exposed_customers"],
                    g["clv_micros"],
                    g["window_churn"],
                    g["churn_exposure"],
                )
                for g in churn["segments"]
            ),
            churn["bill_response"],
            churn["bill_response_sd"],
            churn["calibration_factor"],
            churn["calibration_sd_log"],
            churn.get("bill_share", 1.0),
            tuple((float(m), float(s)) for m, s in churn.get("alternatives", [])),
        ),
        forecast_relative_width=inp["forecast_relative_width"],
        lineage=record["lineage"],
        price_change_counts={int(k): int(n) for k, n in inp["price_change_counts"].items()},
    )


def naive_price(problem: PricingProblem, tick: int) -> int:
    """Point-estimate contribution optimum over x0.5..x3, no constraints, no churn, no risk."""
    p0 = problem.current_price_micros
    grid = np.unique(
        np.append(
            np.rint(p0 * np.exp(np.linspace(math.log(0.5), math.log(3.0), 601)) / tick) * tick, p0
        )
    ).astype(np.int64)
    point = replace(problem, churn=replace(problem.churn, response=0.0))
    ev = evaluate_objective(point, grid, z=np.zeros(1), risk_aversion=0.0)
    return int(grid[int(np.argmax(ev.delta_contribution_mean))])


# ------------------------------------------------------------------- acceptance
_REQUIRED = (
    "decision_id",
    "cycle_id",
    "policy_version",
    "status",
    "reason_codes",
    "lineage",
    "constraints",
    "candidates",
    "rejected_alternatives",
    "guardrails",
    "prediction",
)
_LINEAGE = (
    "forecast_model_version",
    "elasticity_model_version",
    "market_snapshot",
    "customer_model_version",
    "customer_snapshot",
    "churn_response_version",
)


def record_complete(rec: dict[str, Any]) -> list[str]:
    """Missing audit fields (empty list = complete for its status)."""
    missing = [k for k in _REQUIRED if k not in rec]
    if not rec.get("reason_codes"):
        missing.append("reason_codes (empty)")
    if rec.get("status") == Status.UNAVAILABLE.value:
        return missing + ([] if rec.get("errors") else ["errors (empty)"])
    missing += [f"lineage.{k}" for k in _LINEAGE if not rec.get("lineage", {}).get(k)]
    if not rec.get("constraints"):
        missing.append("constraints (empty)")
    evaluated = rec.get("status") != Status.FROZEN.value or rec.get("candidates")
    if evaluated:
        for k in ("candidates", "guardrails", "prediction"):
            if not rec.get(k):
                missing.append(f"{k} (empty)")
    if rec.get("status") == Status.CHANGE.value and rec.get("chosen_price_micros") is None:
        missing.append("chosen_price_micros")
    return missing


def compliance(rec: dict[str, Any], policy: PricingPolicy) -> list[str]:
    """Independent re-check of a CHANGE against its own record and the policy."""
    if rec["status"] != Status.CHANGE.value:
        return []
    p, c, g = rec["chosen_price_micros"], rec["constraints"], rec["guardrails"]
    p0, anchor = rec["current_price_micros"], rec["inputs"]["anchor_price_micros"]
    pc = policy.constraints
    out = []
    if not c["price_floor_micros"] <= p <= c["price_ceiling_micros"]:
        out.append("price bounds")
    lo, hi = c["step_range_micros"]
    if not lo <= p <= hi or abs(math.log(p / p0)) > policy.max_log_step + 1e-12:
        out.append("max step")
    if c["in_cooldown"]:
        out.append("cooldown")
    if anchor is None or abs(math.log(p / anchor)) > policy.evidence.max_extrapolation + 1e-12:
        out.append("extrapolation")
    if g["projected_incremental_churn"] > pc.max_incremental_churn + 1e-12:
        out.append("churn guardrail")
    out += _long_term_compliance(rec, policy)
    if g["min_net_margin"] < pc.min_contribution_margin - 1e-12:
        out.append("margin floor")
    chosen = [x for x in rec["candidates"] if x["price_micros"] == p]
    if len(chosen) != 1 or not chosen[0]["feasible"] or chosen[0]["violations"]:
        out.append("chosen candidate infeasible")
    if not g["passed"] or not all(g["checks"].values()):
        out.append("guardrail outcome")
    return out


def _long_term_compliance(rec: dict[str, Any], policy: PricingPolicy) -> list[str]:
    """Phase 9 guardrails, re-derived from the record's own inputs (not its guardrail block)."""
    lt, out = policy.long_term, []
    p, p0 = rec["chosen_price_micros"], rec["current_price_micros"]
    churn = rec["inputs"]["churn"]
    rise = max(math.log(p / p0), 0.0)
    upper = max(churn["response_upper"], 0.0)
    for g in churn["segments"]:
        n = g["exposed_customers"]
        if n < lt.min_segment_customers:
            continue
        extra = upper * g["churn_exposure"] * rise / n
        limit = lt.max_segment_incremental_churn.get(
            g["segment"], policy.constraints.max_incremental_churn
        )
        if extra > limit + 1e-12:
            out.append(f"segment churn guardrail ({g['segment']})")
        if rise > 0 and g["window_churn"] / n + extra > lt.max_segment_window_churn + 1e-12:
            out.append(f"churn-risk ceiling ({g['segment']})")
    counts = rec["inputs"]["price_change_counts"]
    if any(int(k) + 1 > lt.max_price_changes_per_customer and n for k, n in counts.items()):
        out.append("price-change frequency")
    return out


@dataclass(frozen=True)
class Scored:
    """One decision with its truth."""

    record: dict[str, Any]
    true_delta: dict[int, float]  # price -> true objective delta per day
    true_delta_contribution: dict[int, float]
    true_churn_per_exposed: dict[int, float]  # extra churn probability over the window
    naive_price: int | None
    true_extra_churners: dict[int, float] = field(default_factory=dict)  # per day
    true_extra_by_segment: dict[int, dict[str, float]] = field(default_factory=dict)  # per day

    @property
    def chosen(self) -> int:
        rec = self.record
        return int(rec["chosen_price_micros"] or rec["current_price_micros"])

    @property
    def oracle(self) -> float:
        feasible = [x["price_micros"] for x in self.record["candidates"] if x["feasible"]]
        return max((self.true_delta[p] for p in feasible), default=0.0)


def score(
    decisions: list[Decision],
    inputs: ShadowInputs,
    cfg: Shadow,
    hazards_out: dict[int, tuple[F64, NDArray[np.bool_]]] | None = None,
) -> list[Scored]:
    tick = inputs.policy.policy.price_tick_micros
    start = inputs.world.run.start_date
    plan, meta = [], []
    for d in decisions:
        rec = d.record()
        if not rec["candidates"]:
            continue
        prob = problem_from_record(rec)
        naive = naive_price(prob, tick)
        prices = sorted({x["price_micros"] for x in rec["candidates"]} | {naive})
        plan.append(((d.as_of - start).days, d.product, prices, prob.payment_loss_rate))
        meta.append((rec, prob, naive, prices))
    accs = collect_truth(inputs.world, inputs.seed, plan, cfg, hazards_out)
    tiers = [t.id for t in inputs.world.tiers]
    out = []
    for (rec, prob, naive, prices), acc in zip(meta, accs, strict=True):
        days = max(acc.days, 1)
        contrib = acc.contribution / days
        churn = acc.extra_churn / days
        by_tier = acc.extra_by_tier / days
        base = contrib[prices.index(prob.current_price_micros)]
        exposed = acc.exposed / days
        window = prob.churn.window_days
        clv = np.array([_segment_clv(prob, t) for t in tiers])
        value = by_tier @ clv  # micro-GBP of lost lifetime value per day, per candidate
        out.append(
            Scored(
                rec,
                {p: float(contrib[j] - base - value[j]) for j, p in enumerate(prices)},
                {p: float(contrib[j] - base) for j, p in enumerate(prices)},
                {
                    p: float(churn[j] / exposed * window) if exposed > 0 else 0.0
                    for j, p in enumerate(prices)
                },
                naive,
                {p: float(churn[j]) for j, p in enumerate(prices)},
                {
                    p: {t: float(by_tier[j, k]) for k, t in enumerate(tiers)}
                    for j, p in enumerate(prices)
                },
            )
        )
    return out


def _segment_clv(problem: PricingProblem, tier: str) -> float:
    """The CLV the decision recorded for ``tier`` (0 when the product had no such customers)."""
    return next((g.clv_micros for g in problem.churn.segments if g.segment == tier), 0.0)


def customer_model_truth(
    inputs: ShadowInputs,
    cfg: Shadow,
    customer_artifact: Path,
    hazards: dict[int, tuple[F64, NDArray[np.bool_]]],
) -> dict[str, Any]:
    """Reported, not gated: the calibrated voluntary hazards the decisions used vs the truth.

    Per cycle date and tier (in force at the decision): mean predicted vs mean TRUE daily
    voluntary hazard of the same customers, and the correlation of log hazards across
    customers (does the model rank who churns?).
    """
    values_at = _customers(inputs, customer_artifact)
    start = inputs.world.run.start_date
    cycles = []
    for as_of in inputs.cycle_dates(cfg):
        day = (as_of - start).days
        if day not in hazards:
            continue
        true_h, active = hazards[day]
        v = values_at(as_of)
        index = {customer_id(i): i for i in range(len(true_h))}
        idx = np.array([index[c] for c in v.snapshot.ids], dtype=np.int64)
        truth = true_h[idx]
        ok = active[idx]
        tiers = np.array(v.snapshot.tier)
        row: dict[str, Any] = {"as_of": as_of.isoformat(), "customers": int(ok.sum())}
        for t in sorted(set(v.snapshot.tier)):
            m = ok & (tiers == t)
            pred, true = float(v.voluntary_hazard[m].mean()), float(truth[m].mean())
            row[t] = {"predicted": pred, "true": true, "ratio": pred / true if true else None}
        logs = np.log(v.voluntary_hazard[ok]), np.log(truth[ok])
        row["log_correlation"] = (
            float(np.corrcoef(*logs)[0, 1]) if ok.sum() > 2 and logs[0].std() > 0 else None
        )
        row["calibration_factor"] = None if v.calibration is None else v.calibration.factor
        cycles.append(row)
    return {"champion": values_at(inputs.cycle_dates(cfg)[0]).champion, "cycles": cycles}


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The same world decided by two methods (e.g. Phase 6 vs Phase 9 on seed 42).

    For every decision the earlier method CHANGED: what the later method did instead, and the
    true contribution and churn of each choice (valuation-free) and its own true objective.
    """
    after_rows = {(r["as_of"], r["product"]): r for r in after["rows"]}
    rows, altered = [], 0
    for b in before["rows"]:
        if b["status"] != Status.CHANGE.value:
            continue
        a = after_rows.get((b["as_of"], b["product"]))
        if a is None:
            continue
        same = a["chosen_price_micros"] == b["chosen_price_micros"]
        altered += 0 if same else 1
        rows.append(
            {
                "as_of": b["as_of"],
                "product": b["product"],
                "before": _brief(b),
                "after": _brief(a),
                "altered": not same,
            }
        )
    losing = [r for r in rows if r["before"]["true_delta_objective_per_day"] < 0]
    return {
        "is_synthetic": True,
        "before_policy": before.get("policy_version"),
        "after_policy": after.get("policy_version"),
        "seed": after.get("seed"),
        "before_changes": len(rows),
        "before_losing_changes": len(losing),
        "altered": altered,
        "losing_altered": sum(r["altered"] for r in losing),
        "before_true_delta_per_day": sum(r["before"]["true_delta_objective_per_day"] for r in rows),
        "after_true_delta_per_day": sum(r["after"]["true_delta_objective_per_day"] for r in rows),
        "after_status_counts": after.get("status_counts", {}),
        "rows": rows,
    }


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": row["status"],
        "reason_codes": row["reason_codes"],
        "chosen_price_micros": row["chosen_price_micros"],
        "true_delta_objective_per_day": row["true_delta_objective_per_day"],
        "true_delta_contribution_per_day": row["true_delta_contribution_per_day"],
        "true_extra_churn_per_exposed_window": row["true_extra_churn_per_exposed_window"],
    }


def captured_share(chosen: float, oracle: float) -> float:
    """Share of the achievable true improvement the decisions captured.

    With nothing to gain (oracle <= 0) the decisions must not have lost value either.
    """
    if oracle > 0:
        return chosen / oracle
    return 1.0 if chosen >= oracle else 0.0


def _check(name: str, passed: bool, **detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), **detail}


@dataclass(frozen=True)
class Results:
    """Everything the shadow run produced, before acceptance."""

    decisions: list[Decision]
    scored: list[Scored]
    executions: int
    stress: list[dict[str, Any]]
    prefix_version: str


def acceptance_checks(
    res: Results, policy: PricingPolicy, acc: ShadowAcceptance
) -> list[dict[str, Any]]:
    """Gated checks (ADR 0013 amendment: no harm, honest predictions, safety)."""
    records = [d.record() for d in res.decisions]
    violations = {r["decision_id"]: v for r in records if (v := compliance(r, policy))}
    incomplete = {r["decision_id"]: m for r in records if (m := record_complete(r))}
    changes = [s for s in res.scored if s.record["status"] == Status.CHANGE.value]
    positive = [s for s in changes if s.true_delta[s.chosen] > 0]
    total = sum(s.true_delta[s.chosen] for s in changes)  # holds and freezes contribute 0
    pred = sum(s.record["prediction"]["delta_contribution_mean"] for s in changes)
    true = sum(s.true_delta_contribution[s.chosen] for s in changes)
    ratio = pred / true if true else math.inf
    lo, hi = acc.prediction_ratio_range
    judged = len(changes) >= acc.min_changes_for_calibration
    churn_breaches = {
        s.record["decision_id"]: s.true_churn_per_exposed[s.chosen]
        for s in changes
        if s.true_churn_per_exposed[s.chosen] > policy.constraints.max_incremental_churn
    }
    return [
        _check(
            "constraint_compliance",
            len(violations) <= acc.max_constraint_violations,
            violations=violations,
        ),
        _check(
            "no_shadow_execution",
            res.executions <= acc.max_shadow_executions,
            executions=res.executions,
        ),
        _check(
            "complete_records",
            not incomplete or not acc.require_complete_records,
            incomplete=incomplete,
        ),
        # evaluate() refuses to run without an artifact trained on exactly the shared prefix
        _check("forecast_lineage", True, prefix_data_version=res.prefix_version),
        _check(
            "no_harm",
            total >= acc.min_total_true_delta,
            true_delta_per_day=total,
        ),
        _check(
            "direction",
            not changes or len(positive) / len(changes) >= acc.min_direction_rate,
            value=len(positive) / len(changes) if changes else None,
            changes=len(changes),
        ),
        _check(
            "prediction_calibration",
            not judged or lo <= ratio <= hi,
            judged=judged,
            value=ratio if changes else None,
            predicted_delta_contribution_per_day=pred,
            true_delta_contribution_per_day=true,
        ),
        _check(
            "churn_guardrail_truth",
            not churn_breaches or not acc.churn_guardrail_must_hold,
            breaches=churn_breaches,
        ),
        _check(
            "stress_fail_safe",
            all(c["passed"] for c in res.stress) or not acc.stress_must_fail_safe,
            cases={c["case"]: c["passed"] for c in res.stress},
        ),
    ]


def reported_metrics(res: Results) -> dict[str, Any]:
    """Not gated (ADR 0013 amendment): how much the decisions moved and captured."""
    records = [d.record() for d in res.decisions]
    n_changes = sum(r["status"] == Status.CHANGE.value for r in records)
    chosen = sum(
        s.true_delta[s.chosen] for s in res.scored if s.record["status"] == Status.CHANGE.value
    )
    oracle = sum(s.oracle for s in res.scored)
    naive = [s for s in res.scored if s.naive_price is not None]
    return {
        "change_share": n_changes / len(records) if records else 0.0,
        "captured_share": captured_share(chosen, oracle),
        "chosen_true_delta_per_day": chosen,
        "oracle_true_delta_per_day": oracle,
        "missed_true_delta_per_day": oracle - chosen,
        "naive_baseline_true_delta_per_day": sum(
            s.true_delta[s.naive_price] for s in naive if s.naive_price is not None
        ),
        "churn_prediction": churn_prediction(res.scored),
        "binding_constraints": binding_constraints(records),
    }


def predicted_extra_churners(rec: dict[str, Any], price: int) -> float:
    """Extra voluntary churners per day the decision predicted at ``price`` (point estimate)."""
    churn = rec["inputs"]["churn"]
    x = math.log(price / rec["current_price_micros"])
    exposure = sum(g["churn_exposure"] for g in churn["segments"])
    return float(churn["response"] * exposure * x / churn["window_days"])


def churn_prediction(scored: list[Scored]) -> dict[str, Any]:
    """Predicted vs true extra churners of the +max-step candidate of every scored decision.

    Reported, not gated: is the Phase 9 churn input (bill response x calibrated hazard x bill
    share) honest about the churn a raise would cause, whether or not the raise was chosen?
    """
    pred = true = 0.0
    n = 0
    per: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    for s in scored:
        rec = s.record
        hi = max(x["price_micros"] for x in rec["candidates"])
        if hi <= rec["current_price_micros"] or hi not in s.true_extra_churners:
            continue
        p, t = predicted_extra_churners(rec, hi), s.true_extra_churners[hi]
        pred += p
        true += t
        n += 1
        acc = per[rec["product"]]
        churn = rec["inputs"]["churn"]
        acc[0] += p
        acc[1] += t
        acc[2] += churn["response"]
        acc[3] += churn["response_sd"]
        acc[4] += 1
    return {
        "decisions": n,
        "predicted_extra_churners_per_day": pred,
        "true_extra_churners_per_day": true,
        "ratio": pred / true if true > 0 else None,
        "by_product": {k: _response_check(*v) for k, v in sorted(per.items())},
    }


def _response_check(
    pred: float, true: float, response: float, sd: float, n: float
) -> dict[str, float | None]:
    """The response truth implies (extra churners scale linearly with it) vs the posterior.

    ``z`` = (implied true response - posterior mean) / posterior SD: across worlds and products
    it should look standard normal if the churn evidence's uncertainty is honest.
    """
    mean, sd_mean = response / n, sd / n
    implied = mean * true / pred if pred > 0 else None
    return {
        "predicted_extra_churners_per_day": pred,
        "true_extra_churners_per_day": true,
        "posterior_mean": mean,
        "posterior_sd": sd_mean,
        "implied_true_response": implied,
        "z": None if implied is None or sd_mean <= 0 else (implied - mean) / sd_mean,
    }


def binding_constraints(records: list[dict[str, Any]]) -> dict[str, int]:
    """How often each constraint made the +max-step candidate infeasible (reported)."""
    out: dict[str, int] = defaultdict(int)
    for rec in records:
        if not rec["candidates"]:
            continue
        hi = max(rec["candidates"], key=lambda x: x["price_micros"])
        for v in hi["violations"]:
            out[v] += 1
    return dict(sorted(out.items()))


def _decision_row(s: Scored, policy: PricingPolicy) -> dict[str, Any]:
    rec = s.record
    naive = s.naive_price
    anchor = rec["inputs"]["anchor_price_micros"]
    naive_violations: list[str] = []
    if naive is not None:
        prob = problem_from_record(rec)
        grid = np.array(sorted({naive, prob.current_price_micros}), dtype=np.int64)
        ev = evaluate_objective(prob, grid, z=np.zeros(1), risk_aversion=0.0)
        mask = cons.check(prob, policy, ev)
        i = int(np.flatnonzero(ev.prices == naive)[0])
        naive_violations = list(cons.violations(mask, i))
    return {
        "decision_id": rec["decision_id"],
        "as_of": rec["as_of"],
        "product": rec["product"],
        "status": rec["status"],
        "reason_codes": rec["reason_codes"],
        "current_price_micros": rec["current_price_micros"],
        "chosen_price_micros": rec["chosen_price_micros"],
        "predicted_delta_objective_per_day": rec["prediction"]["delta_objective_mean"],
        "predicted_delta_contribution_per_day": rec["prediction"]["delta_contribution_mean"],
        "true_delta_objective_per_day": s.true_delta[s.chosen],
        "true_delta_contribution_per_day": s.true_delta_contribution[s.chosen],
        "true_extra_churn_per_exposed_window": s.true_churn_per_exposed[s.chosen],
        "projected_extra_churn_upper": rec["guardrails"]["projected_incremental_churn"],
        "true_extra_churners_by_segment_per_day": s.true_extra_by_segment.get(s.chosen, {}),
        "oracle_true_delta_per_day": s.oracle,
        "naive_baseline": None
        if naive is None
        else {
            "price_micros": naive,
            "true_delta_objective_per_day": s.true_delta[naive],
            "true_delta_contribution_per_day": s.true_delta_contribution[naive],
            "true_extra_churn_per_exposed_window": s.true_churn_per_exposed[naive],
            "extrapolation": None if anchor is None else abs(math.log(naive / anchor)),
            "violations": naive_violations,
        },
    }


def evaluate(inputs: ShadowInputs, cfg: ShadowConfig) -> tuple[dict[str, Any], list[Decision]]:
    artifact, prefix_version = select_forecast_artifact(inputs, cfg.shadow)
    if artifact is None:
        raise ShadowError(
            f"no forecast artifact in {inputs.forecast_models} was trained on this world's "
            f"first {cfg.shadow.first_cycle_day} days (panel {prefix_version})"
        )
    customer_artifact, customer_version = select_customer_artifact(inputs, cfg.shadow)
    if customer_artifact is None:
        raise ShadowError(
            f"no customer artifact in {inputs.customer_models} was trained on this world's "
            f"first {cfg.shadow.first_cycle_day} days (panel {customer_version})"
        )
    evidence = load_evidence(
        inputs.elasticity_model,
        inputs.elasticity_report,
        inputs.registry,
        inputs.churn_response,
        tau_scale=inputs.policy.evidence.churn_tau_scale,
        models=inputs.policy.evidence.churn_models,
        prior=prior_settings(inputs.policy.evidence, archive_override=inputs.churn_archive),
    )
    decisions, executions = run_cycles(inputs, cfg.shadow, artifact, evidence, customer_artifact)
    stress = run_stress(inputs, cfg.shadow, artifact, evidence, customer_artifact)
    hazards: dict[int, tuple[F64, NDArray[np.bool_]]] = {}
    scored = score(decisions, inputs, cfg.shadow, hazards)
    results = Results(decisions, scored, executions, stress, prefix_version)
    checks = acceptance_checks(results, inputs.policy, cfg.acceptance)
    by_status: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for d in decisions:
        by_status[d.status.value] += 1
        for r in d.reason_codes:
            by_reason[r] += 1
    report = {
        "is_synthetic": True,
        "world_config_hash": inputs.world.config_hash,
        "seed": inputs.seed,
        "n_customers": inputs.world.population.n_customers,
        "policy_version": inputs.policy.version,
        "forecast_artifact": artifact.name,
        "forecast_prefix_data_version": prefix_version,
        "customer_artifact": customer_artifact.name,
        "customer_prefix_data_version": customer_version,
        "churn_response_version": evidence.churn_response_version,
        "churn_archive_version": evidence.churn_archive_version,
        "elasticity_model_version": evidence.model_version,
        "cycles": [d.isoformat() for d in inputs.cycle_dates(cfg.shadow)],
        "decisions": len(decisions),
        "status_counts": dict(sorted(by_status.items())),
        "reason_counts": dict(sorted(by_reason.items())),
        "rows": [_decision_row(s, inputs.policy) for s in scored],
        "stress": stress,
        "checks": checks,
        "reported": reported_metrics(results),
        "customer_model": customer_model_truth(inputs, cfg.shadow, customer_artifact, hazards),
        "passed": all(c["passed"] for c in checks),
    }
    return report, decisions
