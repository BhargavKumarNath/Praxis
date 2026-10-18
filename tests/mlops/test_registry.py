"""Registry policy: lineage, automatic validation moves, approval, rollback, tracker isolation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from praxis.config import Environment
from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.lineage import LineageError, missing_lineage
from praxis.mlops.registry import AUTOMATION, ModelRegistry, PromotionRefused
from praxis.mlops.store import RegistryError, VersionRecord, VersionRow
from tests.mlops.test_store import record

A, B, C = (f"demand-hybrid-{c * 12}" for c in "abc")
DAY = date(2026, 8, 1)


def rec(version: str) -> VersionRecord:
    return record(version, version[-1] * 64)


def passed(version: str) -> dict[str, Any]:
    return {"model_version": version, "checks": [{"check": "x", "passed": True}], "passed": True}


def failed(version: str) -> dict[str, Any]:
    return {
        "model_version": version,
        "checks": [{"check": "latency_budget", "passed": False}],
        "passed": False,
    }


def shadow_report(champion: str, challenger: str, ok: bool, as_of: date = DAY) -> dict[str, Any]:
    return {
        "status": "evaluated",
        "champion": champion,
        "challenger": challenger,
        "started_as_of": "2026-07-20",
        "as_of": as_of.isoformat(),
        "passed": ok,
        "checks": [{"check": "shadow_vwape_vs_champion", "passed": ok}],
    }


def with_champion(registry: ModelRegistry, version: str = A) -> None:
    registry.register(rec(version))
    registry.record_validation(version, passed(version))
    registry.promote(version, approver="owner", reason="bootstrap", as_of=date(2026, 7, 20))


def stage(registry: ModelRegistry, version: str) -> ModelStage:
    return registry.store.require(version).stage


def test_incomplete_lineage_is_refused(registry: ModelRegistry) -> None:
    broken = replace(
        rec(A),
        data_version="",
        artifact_checksum="nope",
        params={},
        lineage={"training_start": "2026-01-05"},
    )
    assert set(missing_lineage(broken)) >= {
        "data_version",
        "artifact_checksum",
        "params",
        "lineage.training_end",
        "lineage.rows",
        "lineage.config_hash",
    }
    with pytest.raises(LineageError, match="incomplete lineage"):
        registry.register(broken)
    with pytest.raises(RegistryError, match="manages"):
        registry.register(replace(rec(A), model_name="other"))
    assert missing_lineage(rec(A)) == []


def test_validation_moves_candidates_automatically(registry: ModelRegistry) -> None:
    registry.register(rec(A))
    registry.register(rec(B))
    assert registry.record_validation(A, passed(A)).stage == ModelStage.CHALLENGER
    assert registry.record_validation(B, failed(B)).stage == ModelStage.REJECTED
    events = registry.store.events(model_version=B)
    assert events[-1].actor == AUTOMATION and "latency_budget" in events[-1].reason
    assert registry.store.validation_reports(B) == [failed(B)]
    with pytest.raises(RegistryError, match="not candidate"):
        registry.record_validation(B, passed(B))


def test_bootstrap_needs_a_validated_challenger_and_an_approver(registry: ModelRegistry) -> None:
    registry.register(rec(A))
    with pytest.raises(PromotionRefused, match="validated challenger"):
        registry.promote(A, approver="owner", reason="r")
    registry.record_validation(A, passed(A))
    with pytest.raises(PromotionRefused, match="named approver"):
        registry.promote(A, approver=AUTOMATION, reason="r")
    decision = registry.promote(A, approver="owner", reason="bootstrap", as_of=DAY)
    assert stage(registry, A) == ModelStage.CHAMPION
    assert decision.events[0].evidence["bootstrap"] is True
    assert decision.events[0].evidence["as_of"] == DAY.isoformat()
    assert registry.promoted_as_of(A) == DAY


def test_good_candidate_progresses_only_with_shadow_and_approval(registry: ModelRegistry) -> None:
    with_champion(registry)
    registry.register(rec(B))
    registry.record_validation(B, passed(B))
    with pytest.raises(PromotionRefused, match="not ready"):
        registry.promote(B, approver="owner", reason="skip shadow")
    registry.start_shadow(B, as_of=DAY)
    assert registry.shadow_started(B) == DAY
    assert registry.ready_for_promotion(B) == (False, "no passing shadow report")
    with pytest.raises(RegistryError, match="only an evaluated"):
        registry.complete_shadow(B, {"status": "pending"})
    row = registry.complete_shadow(B, shadow_report(A, B, ok=True))
    assert row.stage == ModelStage.SHADOW  # ADR 0005: passing shadow does not promote
    ready, report_id = registry.ready_for_promotion(B)
    assert ready and report_id.startswith("shadow-")
    with pytest.raises(PromotionRefused, match="named approver"):
        registry.promote(B, approver=AUTOMATION, reason="auto")
    decision = registry.promote(B, approver="owner", reason="shadow passed", as_of=DAY)
    assert [(e.model_version, e.to_stage) for e in decision.events] == [
        (A, ModelStage.RETIRED),
        (B, ModelStage.CHAMPION),
    ]
    assert {e.decision_id for e in decision.events} == {decision.decision_id}
    assert decision.events[1].evidence["previous_champion"] == A
    assert registry.store.replaced_by(B) == A


def test_failed_shadow_rejects_and_stale_reports_do_not_count(registry: ModelRegistry) -> None:
    with_champion(registry)
    for v in (B, C):
        registry.register(rec(v))
        registry.record_validation(v, passed(v))
    registry.start_shadow(B, as_of=DAY)
    with pytest.raises(RegistryError, match="already in shadow"):
        registry.start_shadow(C, as_of=DAY)
    assert registry.complete_shadow(B, shadow_report(A, B, ok=False)).stage == ModelStage.REJECTED
    with pytest.raises(RegistryError, match="not shadow"):
        registry.complete_shadow(B, shadow_report(A, B, ok=True))
    registry.start_shadow(C, as_of=DAY)
    registry.complete_shadow(C, shadow_report("demand-hybrid-someoneelse", C, ok=True))
    assert registry.ready_for_promotion(C) == (
        False,
        "the shadow report compared against another champion",
    )
    assert registry.ready_for_promotion(A)[0] is False


def test_shadow_needs_a_champion_and_allowed_stages(make_registry: Callable[..., Any]) -> None:
    registry: ModelRegistry = make_registry()
    registry.register(rec(A))
    registry.record_validation(A, passed(A))
    with pytest.raises(RegistryError, match="needs a champion"):
        registry.start_shadow(A, as_of=DAY)
    with pytest.raises(RegistryError, match="never entered shadow"):
        registry.shadow_started(A)
    with pytest.raises(PromotionRefused, match="not in this model's lifecycle"):
        registry._check_allowed(ModelStage.CANARY, "owner")


def test_rollback_restores_the_replaced_champion(registry: ModelRegistry) -> None:
    with pytest.raises(RegistryError, match="no champion"):
        registry.rollback(actor="owner", reason="r")
    with_champion(registry)
    with pytest.raises(RegistryError, match="no rollback target"):
        registry.rollback(actor="owner", reason="r")
    registry.register(rec(B))
    registry.record_validation(B, passed(B))
    registry.start_shadow(B, as_of=DAY)
    registry.complete_shadow(B, shadow_report(A, B, ok=True))
    registry.promote(B, approver="owner", reason="ok")
    decision = registry.rollback(
        actor="oncall", reason="regression", as_of=DAY, evidence={"probation_report_id": "p-1"}
    )
    assert (
        stage(registry, B) == ModelStage.ROLLED_BACK and stage(registry, A) == ModelStage.CHAMPION
    )
    audit = registry.store.events(decision_id=decision.decision_id)
    assert [(e.trigger, e.actor) for e in audit] == [("rollback", "oncall"), ("restore", "oncall")]
    assert audit[0].evidence["rolled_back"] == B and audit[0].evidence["restored"] == A
    assert audit[0].evidence["probation_report_id"] == "p-1"
    # A was restored (not promoted): it replaced nobody, so a second rollback has no target
    with pytest.raises(RegistryError, match="no rollback target"):
        registry.rollback(actor="oncall", reason="again")


def test_forced_promotion_is_a_development_drill_only(
    make_registry: Callable[..., ModelRegistry],
) -> None:
    prod = make_registry(environment=Environment.PROD)
    with_champion(prod)
    prod.register(rec(B))
    with pytest.raises(PromotionRefused, match="not allowed in prod"):
        prod.force_promote(B, actor="owner", reason="drill")
    dev = make_registry(environment=Environment.DEV)
    with pytest.raises(PromotionRefused, match="named operator"):
        dev.force_promote(B, actor=AUTOMATION, reason="drill")
    decision = dev.force_promote(B, actor="drill-operator", reason="rollback drill", as_of=DAY)
    assert decision.events[-1].evidence["forced"] is True
    assert decision.events[-1].trigger == "promote.forced"
    assert dev.store.replaced_by(B) == A
    with pytest.raises(PromotionRefused, match="cannot force"):
        dev.force_promote(A, actor="drill-operator", reason="retired")

    registry_reject = make_registry()
    registry_reject.register(rec(C))
    registry_reject.record_validation(C, passed(C))
    registry_reject.reject(C, actor="owner", reason="withdrawn")
    assert stage(registry_reject, C) == ModelStage.REJECTED


class RecordingTracker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.logged: list[str] = []
        self.validations: list[str] = []
        self.aliases: list[dict[str, str | None]] = []

    def log_version(self, row: VersionRow, files: Mapping[str, Path]) -> str | None:
        if self.fail:
            raise RuntimeError("tracking server down")
        self.logged.append(row.model_version)
        return f"run-{row.model_version[-4:]}"

    def log_validation(self, run_id: str, report: Mapping[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("tracking server down")
        self.validations.append(run_id)

    def set_aliases(self, model_name: str, aliases: Mapping[str, str | None]) -> None:
        if self.fail:
            raise RuntimeError("tracking server down")
        self.aliases.append(dict(aliases))


def test_tracker_mirrors_without_ever_deciding(make_registry: Callable[..., ModelRegistry]) -> None:
    tracker = RecordingTracker()
    registry = make_registry(tracker=tracker)
    with_champion(registry)
    assert tracker.logged == [A] and tracker.validations == [f"run-{A[-4:]}"]
    assert tracker.aliases[-1] == {"champion": f"run-{A[-4:]}", "shadow": None, "canary": None}
    assert registry.store.require(A).tracking_run_id == f"run-{A[-4:]}"


def test_tracker_failure_never_blocks_a_decision(
    make_registry: Callable[..., ModelRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    registry = make_registry(tracker=RecordingTracker(fail=True))
    with_champion(registry)
    assert stage(registry, A) == ModelStage.CHAMPION
    assert registry.store.require(A).tracking_run_id is None
    assert "mlops.tracking_failed" in caplog.text and "mlops.tracking_sync_failed" in caplog.text
    registry.store.set_tracking_run(A, "run-x")  # e.g. repaired later
    registry.register(rec(B))
    registry.record_validation(B, passed(B))  # run id absent -> nothing to attach
    registry.sync_tracker()


def test_validation_tracking_failure_is_isolated(
    make_registry: Callable[..., ModelRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    tracker = RecordingTracker()
    registry = make_registry(tracker=tracker)
    registry.register(rec(A))
    tracker.fail = True
    assert registry.record_validation(A, passed(A)).stage == ModelStage.CHALLENGER
    assert "mlops.tracking_failed" in caplog.text
