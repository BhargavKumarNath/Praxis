"""The running lifecycle for the demand forecaster: one data day at a time.

``run_day(as_of)`` (actuals known through ``as_of - 1``), in this order:

1. probation: a recently promoted champion vs the champion it replaced; a clear regression
   rolls back automatically (restoring the last known-good version is the only automatic
   champion change this subsystem makes);
2. shadow: the version in shadow vs the champion on the same matured observations; a failed
   shadow rejects it, a passed one waits for a named approver (ADR 0005);
3. drift monitoring of the champion; drift, schedule or new labels open (or join) a
   retraining request;
4. retraining: an open request trains a candidate, registers it, validates it, and moves a
   validated challenger into shadow when the shadow slot is free.

Every step writes its report to the control plane; nothing here promotes a new champion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from praxis.domain.model_lifecycle import ModelStage, RetrainingStatus
from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.config import ForecastConfig
from praxis.mlops.config import MlopsConfig
from praxis.mlops.monitoring import (
    DECISION_SIGNALS,
    drift_check,
    finite_reference,
    probation_check,
)
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.scoring import WarehouseData, load_data
from praxis.mlops.serving import load_registered
from praxis.mlops.shadow import evaluate_shadow
from praxis.mlops.store import (
    MonitoringReport,
    RegistryStore,
    RetrainingRequest,
    VersionRow,
    content_id,
)
from praxis.mlops.training import Candidate, train_candidate, utc_midnight
from praxis.mlops.validation import Gate, validate_candidate

logger = logging.getLogger(__name__)
PROBATION_ACTOR = "mlops-probation"


@dataclass
class DemandLifecycle:
    registry: ModelRegistry
    warehouse: Path
    candidates_root: Path
    forecast_config: ForecastConfig
    code_revision: str
    _artifacts: dict[str, ForecastArtifact] = field(default_factory=dict)
    _data: dict[date, WarehouseData] = field(default_factory=dict)

    @property
    def store(self) -> RegistryStore:
        return self.registry.store

    @property
    def cfg(self) -> MlopsConfig:
        return self.registry.config

    def artifact(self, row: VersionRow) -> ForecastArtifact:
        if row.model_version not in self._artifacts:
            self._artifacts[row.model_version] = load_registered(row)
        return self._artifacts[row.model_version]

    def data(self, as_of: date) -> WarehouseData:
        """Everything observed through ``as_of - 1`` (the decision's information set)."""
        if as_of not in self._data:
            self._data = {as_of: load_data(self.warehouse, end=as_of - timedelta(days=1))}
        return self._data[as_of]

    def _stage(self, stage: ModelStage) -> VersionRow | None:
        return self.store.one_in_stage(self.registry.model_name, stage)

    def _store_report(
        self,
        row: VersionRow,
        kind: str,
        as_of: date,
        start: date,
        fired: bool,
        signals: dict[str, Any],
    ) -> str:
        report = MonitoringReport(
            report_id=content_id(kind, row.model_version, as_of),
            model_name=self.registry.model_name,
            model_version=row.model_version,
            kind=kind,
            as_of=as_of,
            window_start=min(start, as_of),
            window_end=as_of,
            fired=fired,
            signals=signals,
        )
        self.store.add_monitoring_report(report)
        return report.report_id

    # --------------------------------------------------------------- 1. probation
    def probation(self, as_of: date) -> dict[str, Any] | None:
        champion = self._stage(ModelStage.CHAMPION)
        if champion is None:
            return None
        promoted = self.registry.promoted_as_of(champion.model_version)
        previous = self.store.replaced_by(champion.model_version)
        if promoted is None or previous is None:
            return None
        if as_of > promoted + timedelta(days=self.cfg.probation.days):
            return None
        prev_row = self.store.require(previous)
        report = probation_check(
            self.artifact(champion),
            self.artifact(prev_row),
            self.data(as_of),
            promoted,
            as_of,
            self.cfg,
        )
        report_id = self._store_report(
            champion, "probation", as_of, promoted, bool(report["fired"]), report
        )
        report["report_id"] = report_id
        if report["fired"] and self.cfg.probation.auto_rollback:
            decision = self.registry.rollback(
                actor=PROBATION_ACTOR,
                reason=(
                    f"probation: vWAPE {report['vwape_ratio']:.3f} x the previous champion "
                    f"(limit {report['threshold']})"
                ),
                evidence={"probation_report_id": report_id},
                as_of=as_of,
            )
            report["rolled_back"] = decision.decision_id
        return report

    # ------------------------------------------------------------------ 2. shadow
    def shadow(self, as_of: date) -> dict[str, Any] | None:
        shadow, champion = self._stage(ModelStage.SHADOW), self._stage(ModelStage.CHAMPION)
        if champion is None:
            return None
        if shadow is None:
            return self._start_waiting_challenger(as_of)
        started = self.registry.shadow_started(shadow.model_version)
        report = evaluate_shadow(
            self.artifact(champion),
            self.artifact(shadow),
            self.data(as_of),
            started,
            as_of,
            self.cfg,
        )
        if report["status"] == "evaluated":
            row = self.registry.complete_shadow(shadow.model_version, report)
            report["stage_after"] = row.stage.value
            report["ready_for_promotion"] = self.registry.ready_for_promotion(row.model_version)[0]
        return report

    def _start_waiting_challenger(self, as_of: date) -> dict[str, Any] | None:
        """The shadow slot is free: the newest validated challenger takes it (automatic)."""
        waiting = self.store.in_stage(self.registry.model_name, ModelStage.CHALLENGER)
        if not waiting or not self.cfg.lifecycle.automatic(ModelStage.SHADOW):
            return None
        newest = max(waiting, key=lambda r: (r.record.trained_at, r.model_version))
        self.registry.start_shadow(newest.model_version, as_of=as_of)
        return {"status": "started", "challenger": newest.model_version, "as_of": as_of.isoformat()}

    # ----------------------------------------------------------------- 3. monitor
    def monitor(self, as_of: date) -> dict[str, Any] | None:
        champion = self._stage(ModelStage.CHAMPION)
        if champion is None:
            return None
        reference = champion.record.reference
        if reference is None or not finite_reference(reference):
            return {"model_version": champion.model_version, "status": "no_reference"}
        report = drift_check(self.artifact(champion), reference, self.data(as_of), as_of, self.cfg)
        start = as_of - timedelta(days=self.cfg.monitoring.window_days)
        report_id = self._store_report(
            champion, "drift", as_of, start, bool(report["drift_fired"]), report
        )
        report["report_id"] = report_id
        decision_drift = bool(DECISION_SIGNALS & set(report["drift_fired"]))
        blocked = (
            "no new labels since the champion's training data"
            if report["new_labels"] <= 0
            else self._retraining_blocked(as_of, decision_drift=decision_drift)
        )
        if report["retraining_triggers"] and blocked:
            report["retraining_suppressed"] = blocked
        elif report["retraining_triggers"]:
            request, created = self.store.request_retraining(
                self.registry.model_name,
                report["retraining_triggers"],
                {
                    "drift_report_id": report_id,
                    "drift_fired": report["drift_fired"],
                    "data_age_days": report["data_age_days"],
                    "new_labels": report["new_labels"],
                },
                as_of,
                report_id,
                champion.model_version,
            )
            report["retraining_request"] = {"request_id": request.request_id, "created": created}
        return report

    def _retraining_blocked(self, as_of: date, *, decision_drift: bool) -> str | None:
        """Why a new request must wait (None = it may be opened).

        Investigation-only triggers (schedule, labels, PSI) wait while a retrained version
        is still being evaluated. Decision signals (the champion is measurably worse) do
        not: the version under evaluation may predate the change. Both respect the
        cooldown and the one-live-request rule."""
        for stage in (ModelStage.CANDIDATE, ModelStage.CHALLENGER, ModelStage.SHADOW):
            pending = self._stage(stage)
            if pending is not None and not decision_drift:
                return f"{pending.model_version} is still {stage.value}"
        requests = self.store.requests(self.registry.model_name)
        cooldown = self.cfg.retraining.cooldown_days
        if requests and (as_of - max(r.as_of for r in requests)).days < cooldown:
            return f"cooldown: previous request within {cooldown} days"
        return None

    # --------------------------------------------------------------- 4. retrain
    def request(self, as_of: date, reason: str) -> tuple[RetrainingRequest, bool]:
        champion = self._stage(ModelStage.CHAMPION)
        return self.store.request_retraining(
            self.registry.model_name,
            ["manual"],
            {"reason": reason},
            as_of,
            None,
            champion.model_version if champion else None,
        )

    def retrain(
        self, as_of: date, *, forecast_config: ForecastConfig | None = None
    ) -> dict[str, Any] | None:
        request = self.store.live_request(self.registry.model_name)
        if request is None or request.status != RetrainingStatus.OPEN:
            return None
        self.store.advance_request(request.request_id, "started")
        try:
            candidate = self._train(as_of, forecast_config or self.forecast_config)
            existing = self.store.get(candidate.record.model_version)
            outcome = (
                {
                    "candidate": existing.model_version,
                    "duplicate": True,
                    "validation_passed": None,
                    "failed_checks": [],
                    "stage": existing.stage.value,
                    "shadow_started": False,
                }
                if existing is not None
                else self._register_and_validate(candidate, as_of)
            )
        except Exception as exc:
            self.store.advance_request(
                request.request_id, "failed", failure_reason=f"{type(exc).__name__}: {exc}"
            )
            raise
        self.store.advance_request(
            request.request_id, "completed", candidate_version=candidate.record.model_version
        )
        return {"request_id": request.request_id, "triggers": list(request.triggers), **outcome}

    def _train(self, as_of: date, cfg: ForecastConfig) -> Candidate:
        end = as_of - timedelta(days=1)
        return train_candidate(
            load_data(self.warehouse, end=end),
            cfg,
            self.cfg.monitoring,
            self.candidates_root,
            code_revision=self.code_revision,
            now=utc_midnight(end),
        )

    def _register_and_validate(self, candidate: Candidate, as_of: date) -> dict[str, Any]:
        row = self.registry.register(
            candidate.record, files={"artifact": candidate.path}, reason="retraining request"
        )
        champion = self._stage(ModelStage.CHAMPION)
        report = validate_candidate(
            candidate.path,
            candidate.record,
            warehouse=self.warehouse,
            as_of=as_of,
            gate=Gate(self.cfg, self.forecast_config.acceptance),
            backtest=candidate.backtest,
            champion=self.artifact(champion) if champion else None,
        )
        row = self.registry.record_validation(row.model_version, report)
        shadow_started = False
        if (
            row.stage == ModelStage.CHALLENGER
            and champion is not None
            and self.cfg.lifecycle.automatic(ModelStage.SHADOW)
            and self._stage(ModelStage.SHADOW) is None
        ):
            self.registry.start_shadow(row.model_version, as_of=as_of)
            shadow_started = True
        return {
            "candidate": row.model_version,
            "validation_passed": report["passed"],
            "failed_checks": report["failed"],
            "stage": self.store.require(row.model_version).stage.value,
            "shadow_started": shadow_started,
        }

    # ---------------------------------------------------------------- one day
    def run_day(self, as_of: date) -> dict[str, Any]:
        summary: dict[str, Any] = {"as_of": as_of.isoformat()}
        summary["probation"] = self.probation(as_of)
        summary["shadow"] = self.shadow(as_of)
        summary["monitor"] = self.monitor(as_of)
        summary["retraining"] = self.retrain(as_of)
        champion = self._stage(ModelStage.CHAMPION)
        shadow = self._stage(ModelStage.SHADOW)
        summary["champion"] = champion.model_version if champion else None
        summary["shadow_version"] = shadow.model_version if shadow else None
        logger.info(
            "mlops.day", extra={"as_of": as_of.isoformat(), "model_version": summary["champion"]}
        )
        return summary
