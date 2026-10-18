"""Lifecycle drill: the real subsystem run day by day over simulated worlds (Phase 10 gate).

Pre-registered in ``configs/mlops/lifecycle_acceptance.toml``. Three fresh registries:

* steady: champion bootstrapped, then every day probation -> shadow -> drift monitor ->
  retraining; the drill operator approves the first version the registry reports READY;
* drift: the same champion on the drift world; nobody ever approves anything;
* rollback (development environment): a misconfigured candidate must be rejected by the
  gate; another misconfigured artifact is FORCED into champion and probation must roll it
  back, after which the registry-backed router must serve the previous version.

No ground truth is read: every check is about the lifecycle's own records (SYNTHETIC data).
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from praxis.domain.model_lifecycle import SERVING_STAGES, ModelStage
from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.mlops.config import MlopsConfig
from praxis.mlops.lineage import missing_lineage
from praxis.mlops.monitoring import DECISION_SIGNALS
from praxis.mlops.pipeline import PROBATION_ACTOR, DemandLifecycle
from praxis.mlops.registry import AUTOMATION, ModelRegistry
from praxis.mlops.scoring import load_data
from praxis.mlops.serving import RegistryForecaster
from praxis.mlops.training import Candidate, train_candidate, utc_midnight
from praxis.mlops.validation import Gate, validate_candidate

logger = logging.getLogger(__name__)
OPERATOR = "drill-operator"
DEFAULT_ACCEPTANCE = (
    Path(__file__).resolve().parents[3] / "configs" / "mlops" / "lifecycle_acceptance.toml"
)


@dataclass(frozen=True)
class DrillPlan:
    start_date: date  # world day 0
    champion_end_day: int
    first_as_of: int
    last_as_of: int
    drift_onset: int
    detect_within_days: int
    max_steady_alarm_days: int

    def day(self, i: int) -> date:
        return self.start_date + timedelta(days=i)

    @property
    def force_day(self) -> int:
        """Forced bad champion: three weeks into the serving period (labels exist)."""
        return self.first_as_of + 21

    @classmethod
    def from_acceptance(cls, start_date: date, path: Path = DEFAULT_ACCEPTANCE) -> DrillPlan:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        w, c = raw["worlds"], raw["checks"]
        return cls(
            start_date=start_date,
            champion_end_day=int(w["champion_training_end_day"]),
            first_as_of=int(w["first_as_of_day"]),
            last_as_of=int(w["last_as_of_day"]),
            drift_onset=int(w["drift_onset_day"]),
            detect_within_days=int(c["drift_detected_within_days"]),
            max_steady_alarm_days=int(c["steady_max_decision_alarm_days"]),
        )


@dataclass
class DrillEnv:
    """Everything the drill needs that is not a world: registries, recipes, outputs."""

    make_registry: Callable[[str], ModelRegistry]  # world name -> fresh registry
    policy: MlopsConfig
    candidates_root: Path
    forecast_config: ForecastConfig  # the pinned recipe AND the authoritative acceptance
    bad_config: ForecastConfig  # a deliberately misconfigured recipe
    code_revision: str
    registries: dict[str, ModelRegistry] = field(default_factory=dict)

    def registry(self, name: str) -> ModelRegistry:
        if name not in self.registries:
            self.registries[name] = self.make_registry(name)
        return self.registries[name]


def check(name: str, passed: bool, **detail: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), **detail}


# ----------------------------------------------------------------------------- helpers
def _train(env: DrillEnv, warehouse: Path, end: date, cfg: ForecastConfig) -> Candidate:
    return train_candidate(
        load_data(warehouse, end=end),
        cfg,
        env.policy.monitoring,
        env.candidates_root,
        code_revision=env.code_revision,
        now=utc_midnight(end),
    )


def _validate(
    env: DrillEnv, reg: ModelRegistry, c: Candidate, warehouse: Path, as_of: date
) -> dict[str, Any]:
    champion = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    lc = _lifecycle(env, reg, warehouse)
    report = validate_candidate(
        c.path,
        c.record,
        warehouse=warehouse,
        as_of=as_of,
        gate=Gate(reg.config, env.forecast_config.acceptance),
        backtest=c.backtest,
        champion=lc.artifact(champion) if champion else None,
    )
    reg.register(c.record, files={"artifact": c.path}, reason="lifecycle drill")
    reg.record_validation(c.record.model_version, report)
    return report


def _lifecycle(env: DrillEnv, reg: ModelRegistry, warehouse: Path) -> DemandLifecycle:
    return DemandLifecycle(
        reg, warehouse, env.candidates_root, env.forecast_config, env.code_revision
    )


def _bootstrap(
    env: DrillEnv, reg: ModelRegistry, champion: Candidate, warehouse: Path, plan: DrillPlan
) -> dict[str, Any]:
    report = _validate(env, reg, champion, warehouse, plan.day(plan.first_as_of))
    if report["passed"]:
        reg.promote(
            champion.record.model_version,
            approver=OPERATOR,
            reason="bootstrap: Phase 4 recipe on days 0..195",
            as_of=plan.day(plan.first_as_of),
        )
    return report


def _decision_alarms(day: dict[str, Any]) -> list[str]:
    monitor = day.get("monitor") or {}
    return sorted(DECISION_SIGNALS & set(monitor.get("drift_fired", [])))


def _summary(day: dict[str, Any]) -> dict[str, Any]:
    monitor = day.get("monitor") or {}
    shadow = day.get("shadow") or {}
    probation = day.get("probation") or {}
    retraining = day.get("retraining") or {}
    return {
        "as_of": day["as_of"],
        "champion": day["champion"],
        "shadow": day["shadow_version"],
        "drift_fired": monitor.get("drift_fired", []),
        "triggers": monitor.get("retraining_triggers", []),
        "suppressed": monitor.get("retraining_suppressed"),
        "request": monitor.get("retraining_request"),
        "vwape_ratio": (monitor.get("residual") or {}).get("vwape_ratio"),
        "worst_slice": (monitor.get("residual") or {}).get("worst_slice"),
        "worst_slice_ratio": (monitor.get("residual") or {}).get("worst_slice_ratio"),
        "coverage_80": (monitor.get("residual") or {}).get("coverage_80"),
        "shadow_status": shadow.get("status"),
        "shadow_passed": shadow.get("passed"),
        "shadow_failed": [c["check"] for c in shadow.get("checks", []) if not c["passed"]],
        "probation": {k: probation.get(k) for k in ("status", "vwape_ratio", "fired")}
        if probation
        else None,
        "retrained": {
            k: retraining.get(k)
            for k in ("candidate", "validation_passed", "failed_checks", "stage", "triggers")
        }
        if retraining
        else None,
    }


def _served(reg: ModelRegistry, warehouse: Path, moment: date) -> str | None:
    clock = datetime.combine(moment, time(6), tzinfo=UTC)
    router = RegistryForecaster(
        reg.store,
        reg.model_name,
        WarehouseFeatureSource(warehouse),
        service_factory=lambda art, src: ForecastService(art, src, clock=lambda: clock),
    )
    try:
        return router.forecast().model_version
    finally:
        router.close()


def _champion(reg: ModelRegistry) -> str | None:
    row = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    return row.model_version if row else None


# ------------------------------------------------------------------------------ worlds
def steady_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    reg = env.registry("steady")
    _bootstrap(env, reg, champion, warehouse, plan)
    lc = _lifecycle(env, reg, warehouse)
    days, promoted = [], None
    for i in range(plan.first_as_of, plan.last_as_of + 1):
        day = lc.run_day(plan.day(i))
        shadow = day.get("shadow") or {}
        if promoted is None and shadow.get("ready_for_promotion"):
            promoted = {"version": shadow["challenger"], "as_of": day["as_of"]}
            reg.promote(
                shadow["challenger"],
                approver=OPERATOR,
                reason="drill: registry reported READY (shadow passed)",
                as_of=plan.day(i),
            )
            day["champion"] = _champion(reg)
        days.append(_summary(day))
    return {
        "days": days,
        "promoted": promoted,
        "served": _served(reg, warehouse, plan.day(plan.last_as_of)),
        "champion": _champion(reg),
    }


def drift_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    reg = env.registry("drift")
    prefix = load_data(warehouse, end=plan.day(plan.champion_end_day)).panel.data_version()
    _bootstrap(env, reg, champion, warehouse, plan)
    lc = _lifecycle(env, reg, warehouse)
    days = [_summary(lc.run_day(plan.day(i))) for i in range(plan.first_as_of, plan.last_as_of + 1)]
    return {
        "days": days,
        "prefix_data_version": prefix,
        "served": _served(reg, warehouse, plan.day(plan.last_as_of)),
        "champion": _champion(reg),
    }


def rollback_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    reg = env.registry("rollback")
    _bootstrap(env, reg, champion, warehouse, plan)
    labelled = plan.day(plan.force_day - 1)
    rejected = _train(env, warehouse, labelled, env.bad_config)
    rejected_report = _validate(env, reg, rejected, warehouse, plan.day(plan.force_day))
    forced = _train(env, warehouse, plan.day(plan.force_day - 8), env.bad_config)
    reg.register(
        forced.record,
        files={"artifact": forced.path},
        reason="controlled bad candidate (rollback drill)",
    )
    reg.force_promote(
        forced.record.model_version,
        actor=OPERATOR,
        reason="rollback drill",
        as_of=plan.day(plan.force_day),
    )
    served_forced = _served(reg, warehouse, plan.day(plan.force_day))
    lc = _lifecycle(env, reg, warehouse)
    days, rolled = [], None
    last = min(plan.force_day + reg.config.probation.days, plan.last_as_of)
    for i in range(plan.force_day + 1, last + 1):
        day = lc.run_day(plan.day(i))
        days.append(_summary(day))
        if (day.get("probation") or {}).get("rolled_back"):
            rolled = {
                "decision_id": day["probation"]["rolled_back"],
                "as_of": day["as_of"],
                "report": day["probation"],
            }
            break
    audit = reg.store.events(decision_id=rolled["decision_id"]) if rolled else []
    return {
        "rejected": {
            "version": rejected.record.model_version,
            "stage": reg.store.require(rejected.record.model_version).stage.value,
            "failed": rejected_report["failed"],
        },
        "forced": forced.record.model_version,
        "served_while_forced": served_forced,
        "rollback": rolled,
        "audit": [
            {
                "trigger": e.trigger,
                "actor": e.actor,
                "version": e.model_version,
                "to": e.to_stage.value,
            }
            for e in audit
        ],
        "days": days,
        "served_after": _served(reg, warehouse, plan.day(last)),
        "champion": _champion(reg),
    }


# ------------------------------------------------------------------------------ checks
def lineage_report(env: DrillEnv) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, reg in env.registries.items():
        rows = [r for s in ModelStage for r in reg.store.in_stage(reg.model_name, s)]
        out[name] = {
            r.model_version: {
                "stage": r.stage.value,
                "missing": missing_lineage(r.record),
                "tracking_run_id": r.tracking_run_id,
            }
            for r in rows
        }
    return out


def approvals_only(env: DrillEnv) -> list[dict[str, Any]]:
    """Every move INTO champion, with who made it (never the automation)."""
    moves = []
    for name, reg in env.registries.items():
        for e in reg.store.events(model_name=reg.model_name):
            if e.to_stage == ModelStage.CHAMPION:
                moves.append(
                    {
                        "registry": name,
                        "version": e.model_version,
                        "trigger": e.trigger,
                        "actor": e.actor,
                    }
                )
    return moves


def _first(days: list[dict[str, Any]], pred: Callable[[dict[str, Any]], bool]) -> str | None:
    return next((d["as_of"] for d in days if pred(d)), None)


def evaluate(
    plan: DrillPlan,
    champion: Candidate,
    champion_report: dict[str, Any],
    steady: dict[str, Any],
    drift: dict[str, Any],
    rollback: dict[str, Any],
    env: DrillEnv,
) -> list[dict[str, Any]]:
    original = champion.record.model_version
    steady_alarms = [d["as_of"] for d in steady["days"] if DECISION_SIGNALS & set(d["drift_fired"])]
    detect = _first(drift["days"], lambda d: bool(DECISION_SIGNALS & set(d["drift_fired"])))
    deadline = plan.day(plan.drift_onset + plan.detect_within_days).isoformat()
    requests = env.registry("drift").store.requests(env.registry("drift").model_name)
    drift_requests = [r for r in requests if DECISION_SIGNALS & set(r.triggers)]
    moves = approvals_only(env)
    lineage = lineage_report(env)
    restored = [a for a in rollback["audit"] if a["trigger"] == "restore"]
    return [
        check(
            "champion_validation_passes",
            champion_report["passed"],
            failed=champion_report["failed"],
        ),
        check(
            "bad_candidate_rejected",
            rollback["rejected"]["stage"] == "rejected",
            **rollback["rejected"],
        ),
        check("steady_candidate_reaches_ready", steady["promoted"] is not None),
        check(
            "steady_candidate_promoted_by_approver",
            steady["promoted"] is not None and steady["champion"] == steady["promoted"]["version"],
        ),
        check(
            "drift_detected_within_days",
            detect is not None and detect <= deadline,
            first_detection=detect,
            deadline=deadline,
        ),
        check(
            "drift_opens_retraining_request",
            bool(drift_requests),
            requests=[
                {
                    "as_of": r.as_of.isoformat(),
                    "triggers": list(r.triggers),
                    "status": r.status.value,
                }
                for r in drift_requests
            ],
        ),
        check(
            "steady_max_decision_alarm_days",
            len(steady_alarms) <= plan.max_steady_alarm_days,
            alarm_days=steady_alarms,
        ),
        check(
            "no_champion_change_without_approver",
            all(m["actor"] != AUTOMATION for m in moves)
            and drift["champion"] == original
            and all(m["actor"] in (OPERATOR, PROBATION_ACTOR) for m in moves),
            moves=moves,
        ),
        check(
            "rollback_drill",
            rollback["rollback"] is not None
            and rollback["served_while_forced"] == rollback["forced"]
            and rollback["champion"] == original
            and rollback["served_after"] == original
            and bool(restored)
            and restored[0]["actor"] == PROBATION_ACTOR,
            rollback=rollback["rollback"],
            served_after=rollback["served_after"],
        ),
        check(
            "lineage_complete_for_every_version",
            all(not v["missing"] for reg in lineage.values() for v in reg.values()),
            versions=sum(len(v) for v in lineage.values()),
        ),
        check(
            "served_version_equals_registry_champion",
            steady["served"] == steady["champion"]
            and drift["served"] == drift["champion"]
            and rollback["served_after"] == rollback["champion"],
        ),
        check(
            "serving_stages_hold_compatible_contracts",
            all(
                row.record.feature_version == champion.record.feature_version
                for reg in env.registries.values()
                for s in SERVING_STAGES
                for row in reg.store.in_stage(reg.model_name, s)
            ),
        ),
    ]


def run_drill(plan: DrillPlan, steady_db: Path, drift_db: Path, env: DrillEnv) -> dict[str, Any]:
    champion = _train(env, steady_db, plan.day(plan.champion_end_day), env.forecast_config)
    steady = steady_world(env, plan, steady_db, champion)
    champion_report = env.registry("steady").store.validation_reports(
        champion.record.model_version
    )[0]
    drift = drift_world(env, plan, drift_db, champion)
    rollback = rollback_world(env, plan, steady_db, champion)
    checks = evaluate(plan, champion, champion_report, steady, drift, rollback, env)
    return {
        "is_synthetic": True,
        "champion": champion.record.model_version,
        "champion_data_version": champion.record.data_version,
        "drift_prefix_data_version": drift["prefix_data_version"],
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "steady": steady,
        "drift": drift,
        "rollback": rollback,
        "lineage": lineage_report(env),
        "policy_version": env.policy.policy_version,
    }
