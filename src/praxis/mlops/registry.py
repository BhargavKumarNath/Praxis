"""Model registry service: registration, gated stage progression, promotion, rollback.

Policy (ADR 0005, ADR 0017):

* Registration refuses incomplete lineage. Every version starts as CANDIDATE.
* A validation report moves CANDIDATE -> CHALLENGER (all checks passed) or -> REJECTED.
  Nothing else can make a challenger.
* Automation may advance a version only up to ``lifecycle.auto_advance_to``. Promotion to
  champion needs a named approver when ``champion_requires_approval`` and always re-checks
  that the version's latest shadow (or canary) report passed.
* A promotion retires the old champion and installs the new one in ONE transaction; a
  rollback marks the current champion ROLLED_BACK and restores the champion it replaced.
* ``force_promote`` exists for development drills only (environment allow-list, audited).

The tracker (MLflow) is a mirror: it is updated after the registry commits, and a tracker
failure is logged and repaired by ``sync_tracker``; it never changes a decision.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from praxis.config import Environment
from praxis.domain.model_lifecycle import FORCEABLE_STAGES, ModelStage
from praxis.mlops.config import MlopsConfig
from praxis.mlops.lineage import LineageError, missing_lineage
from praxis.mlops.store import (
    Audit,
    MonitoringReport,
    Move,
    RegistryError,
    RegistryStore,
    StageEvent,
    VersionRecord,
    VersionRow,
    content_id,
)
from praxis.mlops.tracking import ALIASED_STAGES, ExperimentTracker, NullTracker
from praxis.tracing import current_trace_id, new_trace_id

logger = logging.getLogger(__name__)

AUTOMATION = "mlops-automation"


class PromotionRefused(RegistryError):
    """The policy does not allow this promotion (missing approval, gate not passed, ...)."""


def _dated(evidence: dict[str, Any], as_of: date | None) -> dict[str, Any]:
    return evidence if as_of is None else {**evidence, "as_of": as_of.isoformat()}


def decision_id(kind: str) -> str:
    return f"{kind}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    events: list[StageEvent]


class ModelRegistry:
    def __init__(
        self,
        store: RegistryStore,
        config: MlopsConfig,
        *,
        tracker: ExperimentTracker | None = None,
        environment: Environment = Environment.LOCAL,
    ) -> None:
        self.store = store
        self.config = config
        self.tracker = tracker or NullTracker()
        self.environment = environment
        self.model_name = config.model_name

    # ----------------------------------------------------------- registration
    def register(
        self,
        record: VersionRecord,
        *,
        files: Mapping[str, Path] | None = None,
        actor: str = AUTOMATION,
        reason: str = "candidate trained",
    ) -> VersionRow:
        missing = missing_lineage(record)
        if missing:
            raise LineageError(f"{record.model_version}: incomplete lineage {missing}")
        if record.model_name != self.model_name:
            raise RegistryError(f"registry manages {self.model_name}, not {record.model_name}")
        row, created = self.store.register(record, self._audit(actor, reason))
        if created:
            self._track(row, files or {})
            logger.info(
                "mlops.registered",
                extra={
                    "model_name": self.model_name,
                    "model_version": row.model_version,
                    "data_version": record.data_version,
                },
            )
        return self.store.require(row.model_version)

    def _track(self, row: VersionRow, files: Mapping[str, Path]) -> None:
        try:
            run_id = self.tracker.log_version(row, files)
        except Exception:  # tracker is a mirror: never let it decide or break a decision
            logger.exception("mlops.tracking_failed", extra={"model_version": row.model_version})
            return
        if run_id is not None:
            self.store.set_tracking_run(row.model_version, run_id)

    # -------------------------------------------------------------- lifecycle
    def record_validation(self, model_version: str, report: Mapping[str, Any]) -> VersionRow:
        """Store the report and move CANDIDATE -> CHALLENGER or REJECTED (automatic)."""
        row = self.store.require(model_version)
        failed = [c["check"] for c in report["checks"] if not c["passed"]]
        report_id = self.store.add_validation_report(model_version, failed, report)
        if row.stage != ModelStage.CANDIDATE:
            raise RegistryError(f"{model_version} is {row.stage.value}, not candidate")
        trigger = "validation.failed" if failed else "validation.passed"
        reason = f"failed checks: {', '.join(failed)}" if failed else "all offline checks passed"
        self._move(
            [Move(model_version, trigger)],
            "validate",
            AUTOMATION,
            reason,
            {"validation_report_id": report_id, "failed_checks": failed},
        )
        if row.tracking_run_id is not None:
            try:
                self.tracker.log_validation(row.tracking_run_id, report)
            except Exception:  # mirror only
                logger.exception("mlops.tracking_failed", extra={"model_version": model_version})
        return self.store.require(model_version)

    def start_shadow(
        self, model_version: str, *, as_of: date, actor: str = AUTOMATION
    ) -> VersionRow:
        row = self.store.require(model_version)
        self._check_allowed(ModelStage.SHADOW, actor)
        if self.store.one_in_stage(self.model_name, ModelStage.SHADOW) is not None:
            raise RegistryError("another version is already in shadow")
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        if champion is None:
            raise RegistryError("shadow needs a champion to compare against")
        self._move(
            [Move(row.model_version, "shadow.started")],
            "shadow",
            actor,
            "challenger scores the champion's observations",
            {"as_of": as_of.isoformat(), "champion": champion.model_version},
        )
        return self.store.require(model_version)

    def shadow_started(self, model_version: str) -> date:
        """Data date on which the version entered shadow."""
        events = [
            e
            for e in self.store.events(model_version=model_version)
            if e.to_stage == ModelStage.SHADOW
        ]
        if not events:
            raise RegistryError(f"{model_version} never entered shadow")
        return date.fromisoformat(str(events[-1].evidence["as_of"]))

    def complete_shadow(self, model_version: str, report: Mapping[str, Any]) -> VersionRow:
        """Store an evaluated shadow report; failed -> REJECTED, passed -> ready (or promoted
        if the policy made champion automatic, which ADR 0005 forbids by default)."""
        row = self.store.require(model_version)
        if row.stage != ModelStage.SHADOW:
            raise RegistryError(f"{model_version} is {row.stage.value}, not shadow")
        if report.get("status") != "evaluated":
            raise RegistryError("only an evaluated shadow report can decide")
        as_of = date.fromisoformat(str(report["as_of"]))
        stored = MonitoringReport(
            report_id=content_id("shadow", model_version, as_of),
            model_name=self.model_name,
            model_version=model_version,
            kind="shadow",
            as_of=as_of,
            window_start=date.fromisoformat(str(report["started_as_of"])),
            window_end=as_of,
            fired=not report["passed"],
            signals=dict(report),
        )
        self.store.add_monitoring_report(stored)
        if not report["passed"]:
            failed = [c["check"] for c in report["checks"] if not c["passed"]]
            self._move(
                [Move(model_version, "shadow.failed")],
                "shadow",
                AUTOMATION,
                f"shadow checks failed: {', '.join(failed)}",
                {"shadow_report_id": stored.report_id, "as_of": as_of.isoformat()},
            )
        elif self.config.lifecycle.automatic(ModelStage.CHAMPION):  # pragma: no cover - policy
            self.promote(model_version, approver=AUTOMATION, reason="shadow passed", as_of=as_of)
        return self.store.require(model_version)

    def ready_for_promotion(self, model_version: str) -> tuple[bool, str]:
        """Has this version passed shadow against the CURRENT champion? (reason when not)."""
        row = self.store.require(model_version)
        if row.stage != ModelStage.SHADOW:
            return False, f"stage {row.stage.value} is not shadow"
        reports = self.store.monitoring_reports(model_version, "shadow")
        if not reports or reports[-1].fired:
            return False, "no passing shadow report"
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        if champion is None or reports[-1].signals.get("champion") != champion.model_version:
            return False, "the shadow report compared against another champion"
        return True, reports[-1].report_id

    def promote(
        self, model_version: str, *, approver: str, reason: str, as_of: date | None = None
    ) -> Decision:
        row = self.store.require(model_version)
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        self._check_allowed(ModelStage.CHAMPION, approver)
        if champion is None:
            if row.stage != ModelStage.CHALLENGER:
                raise PromotionRefused("bootstrap promotes a validated challenger only")
            moves = [Move(model_version, "promote.bootstrap")]
            evidence: dict[str, Any] = {"bootstrap": True}
        else:
            ready, why = self.ready_for_promotion(model_version)
            if not ready:
                raise PromotionRefused(f"{model_version} is not ready: {why}")
            moves = [Move(champion.model_version, "replaced"), Move(model_version, "promote")]
            evidence = {"previous_champion": champion.model_version, "shadow_report_id": why}
        return self._move(moves, "promote", approver, reason, _dated(evidence, as_of))

    def force_promote(
        self, model_version: str, *, actor: str, reason: str, as_of: date | None = None
    ) -> Decision:
        """Development drill: make any pre-champion version champion, skipping the gates."""
        if self.environment not in self.config.lifecycle.forced_promotion_environments:
            raise PromotionRefused(f"forced promotion is not allowed in {self.environment.value}")
        if actor == AUTOMATION:
            raise PromotionRefused("forced promotion needs a named operator")
        row = self.store.require(model_version)
        if row.stage not in FORCEABLE_STAGES:
            raise PromotionRefused(f"cannot force a {row.stage.value} version")
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        moves = [Move(model_version, "promote.forced")]
        if champion is not None:
            moves.insert(0, Move(champion.model_version, "replaced"))
        evidence = {
            "forced": True,
            "environment": self.environment.value,
            "previous_champion": champion.model_version if champion else None,
        }
        return self._move(moves, "promote", actor, reason, _dated(evidence, as_of))

    def promoted_as_of(self, model_version: str) -> date | None:
        """Data date of the version's latest move into champion (None if not recorded)."""
        events = [
            e
            for e in self.store.events(model_version=model_version)
            if e.to_stage == ModelStage.CHAMPION
        ]
        if not events or "as_of" not in events[-1].evidence:
            return None
        return date.fromisoformat(str(events[-1].evidence["as_of"]))

    def rollback(
        self,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        as_of: date | None = None,
    ) -> Decision:
        """Current champion -> ROLLED_BACK; the champion it replaced -> CHAMPION again."""
        champion = self.store.one_in_stage(self.model_name, ModelStage.CHAMPION)
        if champion is None:
            raise RegistryError("no champion to roll back")
        previous = self.store.replaced_by(champion.model_version)
        if previous is None:
            raise RegistryError(
                f"{champion.model_version} replaced no champion: no rollback target"
            )
        target = self.store.require(previous)
        if target.stage != ModelStage.RETIRED:
            raise RegistryError(f"rollback target {previous} is {target.stage.value}")
        moves = [Move(champion.model_version, "rollback"), Move(previous, "restore")]
        details = {"rolled_back": champion.model_version, "restored": previous, **(evidence or {})}
        return self._move(moves, "rollback", actor, reason, _dated(details, as_of))

    def reject(self, model_version: str, *, actor: str, reason: str) -> Decision:
        return self._move([Move(model_version, "rejected")], "reject", actor, reason, {})

    # ---------------------------------------------------------------- helpers
    def _check_allowed(self, stage: ModelStage, actor: str) -> None:
        lifecycle = self.config.lifecycle
        if stage not in lifecycle.stages:
            raise PromotionRefused(f"stage {stage.value} is not in this model's lifecycle")
        if actor == AUTOMATION and not lifecycle.automatic(stage):
            raise PromotionRefused(f"{stage.value} needs a named approver (ADR 0005)")

    def _audit(self, actor: str, reason: str, evidence: Mapping[str, Any] | None = None) -> Audit:
        details = {"policy_version": self.config.policy_version, **(evidence or {})}
        return Audit(actor, reason, current_trace_id() or new_trace_id(), details)

    def _move(
        self,
        moves: list[Move],
        kind: str,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> Decision:
        did = decision_id(kind)
        events = self.store.move(self.model_name, moves, did, self._audit(actor, reason, evidence))
        for e in events:
            logger.info(
                "mlops.stage_changed",
                extra={
                    "model_name": self.model_name,
                    "model_version": e.model_version,
                    "from_stage": e.from_stage,
                    "to_stage": e.to_stage.value,
                    "decision_id": did,
                    "actor": actor,
                    "trigger": e.trigger,
                },
            )
        if any(e.to_stage in ALIASED_STAGES or e.from_stage in ALIASED_STAGES for e in events):
            self.sync_tracker()
        return Decision(did, events)

    def sync_tracker(self) -> None:
        """Re-derive every MLflow alias from the registry (idempotent repair)."""
        aliases: dict[str, str | None] = {}
        for stage in ALIASED_STAGES:
            row = self.store.one_in_stage(self.model_name, stage)
            aliases[stage.value] = row.tracking_run_id if row else None
        try:
            self.tracker.set_aliases(self.model_name, aliases)
        except Exception:  # mirror only; repaired by the next sync
            logger.exception("mlops.tracking_sync_failed", extra={"model_name": self.model_name})
