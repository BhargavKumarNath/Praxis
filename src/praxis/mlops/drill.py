"""Lifecycle drill: the real subsystem run day by day over simulated worlds (Phase 10 gate).

Pre-registered in ``configs/mlops/lifecycle_acceptance.toml``. Three fresh registries:

* steady: champion bootstrapped, then every day probation -> shadow -> drift monitor ->
  retraining; the drill operator (a named approver) approves EVERY version the registry
  reports READY, as an operator running the system continuously would;
* drift: the same champion and the same operator on the drift world (detection, the
  retraining request and the recovery after an approved promotion);
* rollback (development environment): a misconfigured candidate must be rejected by the
  gate; another misconfigured artifact is FORCED into champion and probation must roll it
  back, after which the registry-backed router must serve the previous version.

No ground truth is read: every check is about the lifecycle's own records (SYNTHETIC data).
The three registries are independent, so the worlds can run concurrently, one spawned
PROCESS each (LightGBM training is not safe from several threads of one process), each with
its own candidates directory; the parent then attaches to the registries to evaluate.
"""

from __future__ import annotations

import logging
import multiprocessing
import tomllib
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from praxis.config import Environment
from praxis.control.db import make_engine, migrate
from praxis.domain.model_lifecycle import SERVING_STAGES, ModelStage
from praxis.forecasting.artifact import load_artifact
from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.mlops.config import MlopsConfig, load_mlops_config
from praxis.mlops.lineage import missing_lineage
from praxis.mlops.monitoring import DECISION_SIGNALS
from praxis.mlops.pipeline import PROBATION_ACTOR, DemandLifecycle
from praxis.mlops.registry import AUTOMATION, ModelRegistry
from praxis.mlops.scoring import load_data
from praxis.mlops.serving import RegistryForecaster
from praxis.mlops.store import RegistryStore, VersionRecord
from praxis.mlops.tracking import MlflowTracker
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

    # (world name, fresh) -> registry: fresh=True creates it, False attaches to it. Must be
    # picklable (e.g. a functools.partial of a module-level function) when ``parallel``.
    make_registry: Callable[[str, bool], ModelRegistry]
    policy: MlopsConfig
    candidates_root: Path
    forecast_config: ForecastConfig  # the pinned recipe AND the authoritative acceptance
    bad_config: ForecastConfig  # a deliberately misconfigured recipe
    code_revision: str
    registries: dict[str, ModelRegistry] = field(default_factory=dict)
    parallel: bool = False  # one spawned process per world (independent registries)

    def registry(self, name: str) -> ModelRegistry:
        if name not in self.registries:
            self.registries[name] = self.make_registry(name, True)
        return self.registries[name]


@dataclass(frozen=True)
class DrillRegistries:
    """Picklable registry factory for the CLI drill: one throwaway database per world on a
    local server (``admin_url`` = an ADMIN url), each with its own MLflow store."""

    admin_url: str
    seed: int
    out: Path
    config: Path | None = None

    def __call__(self, name: str, fresh: bool = True) -> ModelRegistry:
        """(Re)create the world's registry database when ``fresh``, else attach to it."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        db = f"praxis_mlops_drill_{name}_s{self.seed}"
        url = make_url(self.admin_url).set(database=db).render_as_string(hide_password=False)
        if fresh:
            admin = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
                conn.execute(text(f'CREATE DATABASE "{db}"'))
            admin.dispose()
            migrate(url)
        tracker = MlflowTracker(
            f"sqlite:///{(self.out / f'mlflow-{name}.db').resolve()}",  # one writer each
            self.out / "mlflow-artifacts",
            experiment=f"praxis-drill-{name}-s{self.seed}",
        )
        return ModelRegistry(
            RegistryStore(make_engine(url)),
            load_mlops_config(self.config),
            tracker=tracker,
            environment=Environment.DEV,
        )


def check(name: str, passed: bool, **detail: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), **detail}


# ----------------------------------------------------------------------------- helpers
def _train(
    env: DrillEnv, warehouse: Path, end: date, cfg: ForecastConfig, world: str | None = None
) -> Candidate:
    return train_candidate(
        load_data(warehouse, end=end),
        cfg,
        env.policy.monitoring,
        env.candidates_root / world if world else env.candidates_root,
        code_revision=env.code_revision,
        now=utc_midnight(end),
    )


def _validate(
    env: DrillEnv, reg: ModelRegistry, c: Candidate, warehouse: Path, as_of: date
) -> dict[str, Any]:
    champion = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    lc = _lifecycle(env, reg, warehouse, "validation")
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


def _lifecycle(env: DrillEnv, reg: ModelRegistry, warehouse: Path, world: str) -> DemandLifecycle:
    return DemandLifecycle(
        reg, warehouse, env.candidates_root / world, env.forecast_config, env.code_revision
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
        "alarms": monitor.get("alarms", []),
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
def _approve_ready(reg: ModelRegistry, day: dict[str, Any], as_of: date) -> dict[str, Any] | None:
    """The drill operator approves a version only when the registry reports it READY (it
    passed shadow against the CURRENT champion); the registry re-checks that itself."""
    shadow = day.get("shadow") or {}
    if not shadow.get("ready_for_promotion"):
        return None
    reg.promote(
        shadow["challenger"],
        approver=OPERATOR,
        reason="drill: registry reported READY (shadow passed against the current champion)",
        as_of=as_of,
    )
    day["champion"] = _champion(reg)
    return {"version": shadow["challenger"], "as_of": day["as_of"]}


def _operate(
    env: DrillEnv, plan: DrillPlan, name: str, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    """Bootstrap the champion, then run the lifecycle every day with the operator on duty."""
    reg = env.registry(name)
    _bootstrap(env, reg, champion, warehouse, plan)
    lc = _lifecycle(env, reg, warehouse, name)
    days, promotions = [], []
    for i in range(plan.first_as_of, plan.last_as_of + 1):
        day = lc.run_day(plan.day(i))
        promoted = _approve_ready(reg, day, plan.day(i))
        if promoted is not None:
            promotions.append(promoted)
        days.append(_summary(day))
    return {
        "days": days,
        "promotions": promotions,
        "rollbacks": _restores(reg),
        "served": _served(reg, warehouse, plan.day(plan.last_as_of)),
        "champion": _champion(reg),
    }


def _restores(reg: ModelRegistry) -> list[dict[str, Any]]:
    return [
        {"version": e.model_version, "actor": e.actor, "as_of": e.evidence.get("as_of")}
        for e in reg.store.events(model_name=reg.model_name)
        if e.trigger == "restore"
    ]


def steady_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    return _operate(env, plan, "steady", warehouse, champion)


def drift_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    prefix = load_data(warehouse, end=plan.day(plan.champion_end_day)).panel.data_version()
    return {
        **_operate(env, plan, "drift", warehouse, champion),
        "prefix_data_version": prefix,
    }


def rollback_world(
    env: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Candidate
) -> dict[str, Any]:
    reg = env.registry("rollback")
    _bootstrap(env, reg, champion, warehouse, plan)
    labelled = plan.day(plan.force_day - 1)
    rejected = _train(env, warehouse, labelled, env.bad_config, "rollback")
    rejected_report = _validate(env, reg, rejected, warehouse, plan.day(plan.force_day))
    forced = _train(env, warehouse, plan.day(plan.force_day - 8), env.bad_config, "rollback")
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
    lc = _lifecycle(env, reg, warehouse, "rollback")
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


def _moves_are_authorised(moves: list[dict[str, Any]]) -> bool:
    """Every move into champion: an approval by the named operator (the registry required
    READY), a forced promotion only in the rollback drill, a restore only by probation."""
    by_trigger = {
        "promote": (OPERATOR,),
        "promote.bootstrap": (OPERATOR,),
        "promote.forced": (OPERATOR,),
        "restore": (PROBATION_ACTOR,),
    }
    return all(
        m["actor"] != AUTOMATION
        and m["actor"] in by_trigger.get(m["trigger"], ())
        and (m["trigger"] != "promote.forced" or m["registry"] == "rollback")
        for m in moves
    )


def _final_champion_is_approved(world: dict[str, Any], original: str) -> bool:
    """The champion at the end is the last version the operator approved (or the original
    when none was), unless probation restored a later one's predecessor afterwards."""
    approved = [p["version"] for p in world["promotions"]]
    expected = approved[-1] if approved else original
    if world["rollbacks"]:
        expected = world["rollbacks"][-1]["version"]
    return bool(world["champion"] == expected)


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
    steady_alarms = [d["as_of"] for d in steady["days"] if d["alarms"]]
    onset = plan.day(plan.drift_onset).isoformat()
    detect = _first(drift["days"], lambda d: bool(d["alarms"]) and d["as_of"] >= onset)
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
        check(
            "steady_candidate_reaches_ready",
            bool(steady["promotions"]),
            promotions=steady["promotions"],
        ),
        check(
            "steady_candidate_promoted_by_approver",
            bool(steady["promotions"]) and _final_champion_is_approved(steady, original),
            champion=steady["champion"],
        ),
        check(
            "drift_detected_within_days",
            detect is not None and detect <= deadline,
            first_detection=detect,
            first_alarm=_first(drift["days"], lambda d: bool(d["alarms"])),
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
            limit=plan.max_steady_alarm_days,
        ),
        check(
            "no_champion_change_without_approver",
            _moves_are_authorised(moves)
            and _final_champion_is_approved(steady, original)
            and _final_champion_is_approved(drift, original),
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


WORLDS: dict[str, Callable[[DrillEnv, DrillPlan, Path, Candidate], dict[str, Any]]] = {
    "steady": steady_world,
    "drift": drift_world,
    "rollback": rollback_world,
}


def _world_job(
    env: DrillEnv,
    name: str,
    plan: DrillPlan,
    warehouse: Path,
    champion: tuple[Path, dict[str, Any], VersionRecord],
) -> dict[str, Any]:
    """One world in a spawned process: the champion is re-loaded from its saved artifact."""
    path, backtest, record = champion
    return WORLDS[name](
        env, plan, warehouse, Candidate(path, load_artifact(path), backtest, record)
    )


def _run_worlds(
    env: DrillEnv, plan: DrillPlan, warehouses: dict[str, Path], champion: Candidate
) -> dict[str, dict[str, Any]]:
    if not env.parallel:
        return {n: f(env, plan, warehouses[n], champion) for n, f in WORLDS.items()}
    portable = replace(env, registries={})
    saved = (champion.path, champion.backtest, champion.record)
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(WORLDS), mp_context=spawn) as pool:
        futures = {
            n: pool.submit(_world_job, portable, n, plan, warehouses[n], saved) for n in WORLDS
        }
        out = {n: fut.result() for n, fut in futures.items()}
    for name in WORLDS:  # the parent evaluates from the registries the workers wrote
        env.registries[name] = env.make_registry(name, False)
    return out


def run_drill(plan: DrillPlan, steady_db: Path, drift_db: Path, env: DrillEnv) -> dict[str, Any]:
    champion = _train(env, steady_db, plan.day(plan.champion_end_day), env.forecast_config)
    worlds = _run_worlds(
        env, plan, {"steady": steady_db, "drift": drift_db, "rollback": steady_db}, champion
    )
    steady, drift, rollback = worlds["steady"], worlds["drift"], worlds["rollback"]
    champion_report = env.registry("steady").store.validation_reports(
        champion.record.model_version
    )[0]
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
