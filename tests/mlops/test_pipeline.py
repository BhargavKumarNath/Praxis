"""The running lifecycle, end to end on known worlds (required_test.md s16).

* drift -> detector fires -> retraining request -> candidate -> shadow, and NO promotion;
* a good candidate passes validation, enters shadow, passes it and is promoted by a named
  approver (configured stages: challenger -> shadow -> champion);
* a bad candidate is rejected automatically;
* rollback drill: a controlled bad candidate is forced into champion (development), the
  probation check rolls it back, the router serves the previous version again, the audit
  records the rollback, and no incompatible feature contract is served.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest

from praxis.config import Environment
from praxis.domain.model_lifecycle import ModelStage, RetrainingStatus
from praxis.forecasting.features import FEATURE_VERSION
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.mlops.pipeline import PROBATION_ACTOR, DemandLifecycle
from praxis.mlops.registry import AUTOMATION, ModelRegistry
from praxis.mlops.serving import RegistryForecaster
from praxis.mlops.training import Candidate
from tests.mlops import helpers as h
from tests.mlops.conftest import World

NAME = "demand_forecaster"
FIRST = h.TRAIN_END_DAY + 1  # first data day the champion serves


def lifecycle(registry: ModelRegistry, warehouse: Path, tmp: Path) -> DemandLifecycle:
    return DemandLifecycle(registry, warehouse, tmp / "candidates", h.forecast_config(), "test-rev")


def bootstrap(lc: DemandLifecycle, champion: Candidate, world: World) -> None:
    from praxis.mlops.validation import Gate, validate_candidate

    reg = lc.registry
    reg.register(champion.record)
    report = validate_candidate(
        champion.path,
        champion.record,
        warehouse=lc.warehouse,
        as_of=world.date(FIRST),
        gate=Gate(reg.config, lc.forecast_config.acceptance),
        backtest=champion.backtest,
        champion=None,
    )
    assert reg.record_validation(champion.record.model_version, report).stage == (
        ModelStage.CHALLENGER
    )
    reg.promote(
        champion.record.model_version, approver="owner", reason="bootstrap", as_of=world.date(FIRST)
    )


def run_days(lc: DemandLifecycle, world: World, first: int, last: int) -> list[dict[str, Any]]:
    return [lc.run_day(world.date(d)) for d in range(first, last + 1)]


def champion_moves_are_approved(lc: DemandLifecycle) -> bool:
    into = [e for e in lc.store.events(model_name=NAME) if e.to_stage == ModelStage.CHAMPION]
    return all(e.actor != AUTOMATION for e in into)


def test_drift_requests_retraining_and_never_promotes(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    lc = lifecycle(registry, world.drifted, tmp_path)
    bootstrap(lc, champion, world)
    days = run_days(lc, world, FIRST, h.SHIFT_DAY + 10)
    fired = [
        d
        for d in days
        if d["monitor"] and {"residual_drift", "segment_drift"} & set(d["monitor"]["drift_fired"])
    ]
    assert fired, "the injected shift was not detected"
    first_day = fired[0]["as_of"]
    assert first_day <= world.date(h.SHIFT_DAY + 7).isoformat()
    requests = lc.store.requests(NAME)
    drift_requests = [r for r in requests if {"residual_drift", "segment_drift"} & set(r.triggers)]
    assert drift_requests and drift_requests[0].as_of.isoformat() == first_day
    retrained = [d["retraining"] for d in days if d["retraining"]]
    assert retrained, "the request did not produce a candidate"
    # the champion never changed: drift is evidence, not a promotion
    assert {d["champion"] for d in days} == {champion.record.model_version}
    assert champion_moves_are_approved(lc)
    reports = lc.store.monitoring_reports(champion.record.model_version, "drift")
    assert len(reports) == len(days) and any(r.fired for r in reports)
    # re-running a day is idempotent (same report id, request joined, no new candidate)
    again = lc.run_day(world.date(h.SHIFT_DAY + 10))
    assert len(lc.store.monitoring_reports(champion.record.model_version, "drift")) == len(days)
    assert again["retraining"] is None or again["retraining"]["candidate"]


def test_good_candidate_progresses_through_configured_stages(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    lc = lifecycle(registry, world.clean, tmp_path)
    bootstrap(lc, champion, world)
    days = run_days(lc, world, FIRST, 152)
    trained = [d["retraining"] for d in days if d["retraining"]]
    assert trained and trained[0]["validation_passed"] and trained[0]["shadow_started"]
    assert trained[0]["triggers"]  # the toy world trends: PSI or labels may come first
    candidate = trained[0]["candidate"]
    shadows = [d["shadow"] for d in days if d["shadow"] and d["shadow"]["status"] == "evaluated"]
    assert shadows and shadows[0]["passed"] and shadows[0]["ready_for_promotion"]
    assert registry.store.require(candidate).stage == ModelStage.SHADOW  # waits for approval
    assert shadows[-1]["challenger"] == candidate and shadows[-1]["passed"]
    decision = registry.promote(
        candidate, approver="owner", reason="shadow passed", as_of=world.date(153)
    )
    assert registry.store.require(candidate).stage == ModelStage.CHAMPION
    stages = [e.to_stage for e in registry.store.events(model_version=candidate)]
    assert stages == [
        ModelStage.CANDIDATE,
        ModelStage.CHALLENGER,
        ModelStage.SHADOW,
        ModelStage.CHAMPION,
    ]
    assert decision.events[0].model_version == champion.record.model_version
    after = run_days(lc, world, 153, 167)
    probation = [d["probation"] for d in after if d["probation"]]
    assert probation and not any(p["fired"] for p in probation)
    assert all(d["champion"] == candidate for d in after)
    assert champion_moves_are_approved(lc)


def test_bad_candidate_is_rejected_automatically(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    lc = lifecycle(registry, world.clean, tmp_path)
    bootstrap(lc, champion, world)
    request, created = lc.request(world.date(140), "bad recipe drill")
    assert created and request.triggers == ("manual",)
    out = lc.retrain(world.date(140), forecast_config=h.bad_forecast_config())
    assert out is not None and not out["validation_passed"]
    assert out["stage"] == ModelStage.REJECTED.value and not out["shadow_started"]
    assert "backtest_acceptance" in out["failed_checks"]
    done = lc.store.get_request(request.request_id)
    assert done is not None and done.status == RetrainingStatus.COMPLETED
    assert lc.retrain(world.date(141)) is None  # nothing open


def test_retraining_failure_is_recorded(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    lc = lifecycle(registry, world.clean, tmp_path / "x")
    bootstrap(lc, champion, world)
    request, _ = lc.request(world.date(140), "failure drill")
    lc.warehouse = tmp_path / "missing.duckdb"
    with pytest.raises(Exception, match="warehouse not found"):
        lc.retrain(world.date(140))
    failed = lc.store.get_request(request.request_id)
    assert failed is not None and failed.status == RetrainingStatus.FAILED
    assert failed.failure_reason and "warehouse not found" in failed.failure_reason


def test_rollback_drill(
    make_registry: Callable[..., ModelRegistry],
    world: World,
    champion: Candidate,
    bad: Candidate,
    tmp_path: Path,
) -> None:
    registry = make_registry(environment=Environment.DEV)
    lc = lifecycle(registry, world.clean, tmp_path)
    bootstrap(lc, champion, world)
    clock = datetime.combine(world.date(150), time(6), tzinfo=UTC)
    router = RegistryForecaster(
        registry.store,
        NAME,
        WarehouseFeatureSource(world.clean),
        service_factory=lambda art, src: ForecastService(art, src, clock=lambda: clock),
    )
    assert router.forecast().model_version == champion.record.model_version

    registry.register(bad.record, reason="controlled bad candidate (rollback drill)")
    drill_day = h.TRAIN_END_DAY + 29
    registry.force_promote(
        bad.record.model_version,
        actor="drill-operator",
        reason="rollback drill",
        as_of=world.date(drill_day),
    )
    router.refresh(force=True)
    assert router.forecast().model_version == bad.record.model_version

    days = run_days(lc, world, drill_day + 1, drill_day + 16)
    rolled = [d for d in days if d["probation"] and d["probation"].get("rolled_back")]
    assert rolled, [d["probation"] for d in days]
    # champion pointer restored
    assert registry.store.require(champion.record.model_version).stage == ModelStage.CHAMPION
    assert registry.store.require(bad.record.model_version).stage == ModelStage.ROLLED_BACK
    # service uses the old version
    router.refresh(force=True)
    served = router.forecast()
    assert served.model_version == champion.record.model_version
    # decision audit records the rollback
    decision = rolled[0]["probation"]["rolled_back"]
    audit = registry.store.events(decision_id=decision)
    assert [(e.trigger, e.actor) for e in audit] == [
        ("rollback", PROBATION_ACTOR),
        ("restore", PROBATION_ACTOR),
    ]
    assert audit[0].evidence["probation_report_id"].startswith("probation-")
    # no incompatible feature contract remains in a serving stage
    assert served.feature_version == FEATURE_VERSION
    for stage in (ModelStage.CHAMPION, ModelStage.SHADOW, ModelStage.CANARY):
        for row in registry.store.in_stage(NAME, stage):
            assert row.record.feature_version == FEATURE_VERSION
    later = run_days(lc, world, drill_day + 17, drill_day + 18)
    assert all(d["champion"] == champion.record.model_version for d in later)


def test_monitor_without_reference_reports_it(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    from dataclasses import replace

    lc = lifecycle(registry, world.clean, tmp_path)
    record = replace(champion.record, reference=None)
    registry.register(record)
    registry.record_validation(
        record.model_version, {"checks": [{"check": "x", "passed": True}], "passed": True}
    )
    registry.promote(record.model_version, approver="owner", reason="bootstrap")
    day = lc.run_day(world.date(FIRST + 1))
    assert day["monitor"] == {"model_version": record.model_version, "status": "no_reference"}
    assert day["probation"] is None and day["shadow"] is None


def test_empty_registry_does_nothing(registry: ModelRegistry, world: World, tmp_path: Path) -> None:
    day = lifecycle(registry, world.clean, tmp_path).run_day(world.date(FIRST))
    assert day["champion"] is None and day["monitor"] is None and day["retraining"] is None


def test_retraining_on_unchanged_data_is_a_no_op(
    registry: ModelRegistry, world: World, champion: Candidate, tmp_path: Path
) -> None:
    """Found on the development world: a PSI alarm on the first serving day asked for a
    retrain with no new labels, which reproduced the champion exactly."""
    lc = lifecycle(registry, world.clean, tmp_path)
    bootstrap(lc, champion, world)
    day = lc.run_day(world.date(FIRST))
    assert day["monitor"]["new_labels"] == 0
    assert day["monitor"].get("retraining_request") is None
    if day["monitor"]["retraining_triggers"]:
        assert "no new labels" in day["monitor"]["retraining_suppressed"]
    lc.request(world.date(FIRST), "operator asks anyway")
    out = lc.retrain(world.date(FIRST))
    assert (
        out is not None and out["duplicate"] and out["candidate"] == (champion.record.model_version)
    )
    assert registry.store.require(champion.record.model_version).stage == ModelStage.CHAMPION
