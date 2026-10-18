"""Control-plane registry: idempotent registration, DB-enforced lifecycle, audit, requests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, exc, text

from praxis.domain.model_lifecycle import ModelStage, RetrainingStatus
from praxis.domain.states import InvalidTransition
from praxis.mlops.store import (
    Audit,
    MonitoringReport,
    Move,
    RegistryError,
    RegistryStore,
    VersionRecord,
    content_id,
)

NAME = "demand_forecaster"
AUDIT = Audit("tester", "test", "0" * 32)


def record(version: str = "demand-hybrid-aaaaaaaaaaaa", checksum: str = "a" * 64) -> VersionRecord:
    return VersionRecord(
        model_name=NAME,
        model_version=version,
        artifact_uri=f"file:///models/{version}",
        artifact_checksum=checksum,
        data_version="panel-1",
        feature_version="demand_features.v1",
        code_revision="abc123",
        params={"ridge": {"alpha": 1.0}},
        metrics={"backtest": {"hybrid": {"vwape": 0.12}}},
        lineage={
            "training_start": "2026-01-05",
            "training_end": "2026-07-19",
            "rows": 9,
            "config_hash": "c",
        },
        trained_at=datetime(2026, 7, 20, tzinfo=UTC),
        is_synthetic=True,
        reference={"bins": {}},
    )


def champion_of(store: RegistryStore) -> str | None:
    row = store.one_in_stage(NAME, ModelStage.CHAMPION)
    return row.model_version if row else None


def test_register_is_idempotent_and_audited(registry_store: RegistryStore) -> None:
    row, created = registry_store.register(record(), AUDIT)
    again, created_again = registry_store.register(record(), AUDIT)
    assert created and not created_again
    assert row.stage == again.stage == ModelStage.CANDIDATE
    assert row.record == record()
    events = registry_store.events(model_version=row.model_version)
    assert [(e.from_stage, e.to_stage, e.trigger) for e in events] == [
        (None, ModelStage.CANDIDATE, "registered")
    ]
    with pytest.raises(RegistryError, match="different checksum"):
        registry_store.register(record(checksum="b" * 64), AUDIT)
    assert registry_store.get("missing") is None
    with pytest.raises(RegistryError, match="unknown"):
        registry_store.require("missing")


def test_lineage_is_immutable_and_versions_cannot_be_deleted(
    registry_store: RegistryStore, pg_engine: Engine
) -> None:
    registry_store.register(record(), AUDIT)
    for sql in (
        "UPDATE model_versions SET data_version = 'other'",
        "UPDATE model_versions SET metrics = '{}'::jsonb",
        "DELETE FROM model_versions",
        "UPDATE model_stage_events SET actor = 'someone else'",
        "DELETE FROM model_stage_events",
    ):
        with pytest.raises(exc.DBAPIError), pg_engine.begin() as conn:
            conn.execute(text(sql))


@pytest.mark.parametrize("target", ["canary", "shadow", "retired", "rolled_back"])
def test_database_refuses_moves_outside_the_machine(
    registry_store: RegistryStore, pg_engine: Engine, target: str
) -> None:
    registry_store.register(record(), AUDIT)
    with pytest.raises(exc.DBAPIError, match="forbidden model move"), pg_engine.begin() as conn:
        conn.execute(text("UPDATE model_versions SET stage = :s"), {"s": target})


def test_moves_are_atomic_and_one_champion_is_enforced(
    registry_store: RegistryStore, pg_engine: Engine
) -> None:
    a, b = "demand-hybrid-aaaaaaaaaaaa", "demand-hybrid-bbbbbbbbbbbb"
    registry_store.register(record(a), AUDIT)
    registry_store.register(record(b, "b" * 64), AUDIT)
    for v in (a, b):
        registry_store.move(NAME, [Move(v, "validation.passed")], content_id("d", v), AUDIT)
    registry_store.move(NAME, [Move(a, "promote.bootstrap")], "d-boot", AUDIT)
    # a second champion is unrepresentable (partial unique index) and the move rolls back
    with pytest.raises(RegistryError, match="conflicts"):
        registry_store.move(NAME, [Move(b, "promote.bootstrap")], "d-bad", AUDIT)
    assert registry_store.require(b).stage == ModelStage.CHALLENGER
    # an invalid second step rolls back the valid first one (one transaction)
    with pytest.raises(InvalidTransition):
        registry_store.move(
            NAME, [Move(b, "shadow.started"), Move(a, "validation.passed")], "d-mixed", AUDIT
        )
    assert registry_store.require(b).stage == ModelStage.CHALLENGER
    assert registry_store.events(decision_id="d-mixed") == []
    with pytest.raises(RegistryError, match="unknown"):
        registry_store.move("other_model", [Move(a, "replaced")], "d-x", AUDIT)
    assert champion_of(registry_store) == a
    with pg_engine.begin() as conn:
        version = conn.execute(
            text("SELECT version FROM model_versions WHERE model_version = :v"), {"v": a}
        ).scalar_one()
    assert version == 3  # registered (1), validated (2), promoted (3)


def test_replaced_by_follows_the_promotion_decision(registry_store: RegistryStore) -> None:
    a, b = "demand-hybrid-aaaaaaaaaaaa", "demand-hybrid-bbbbbbbbbbbb"
    registry_store.register(record(a), AUDIT)
    registry_store.register(record(b, "b" * 64), AUDIT)
    assert registry_store.replaced_by(a) is None
    registry_store.move(
        NAME, [Move(a, "validation.passed"), Move(a, "promote.bootstrap")], "boot", AUDIT
    )
    assert registry_store.replaced_by(a) is None  # bootstrap replaced nobody
    registry_store.move(
        NAME, [Move(b, "validation.passed"), Move(b, "shadow.started")], "sh", AUDIT
    )
    registry_store.move(NAME, [Move(a, "replaced"), Move(b, "promote")], "promo", AUDIT)
    assert registry_store.replaced_by(b) == a
    assert [e.decision_id for e in registry_store.events(decision_id="promo")] == ["promo"] * 2


def test_retraining_requests_join_the_live_one(registry_store: RegistryStore) -> None:
    first, created = registry_store.request_retraining(
        NAME, ["residual_drift"], {"x": 1}, date(2026, 8, 1), None, None
    )
    second, created2 = registry_store.request_retraining(
        NAME, ["scheduled"], {}, date(2026, 8, 2), None, None
    )
    assert created and not created2 and second.request_id == first.request_id
    assert registry_store.live_request(NAME) == first
    training = registry_store.advance_request(first.request_id, "started")
    assert training.status == RetrainingStatus.TRAINING
    with pytest.raises(InvalidTransition):
        registry_store.advance_request(first.request_id, "started")
    failed = registry_store.advance_request(first.request_id, "failed", failure_reason="boom")
    assert failed.status == RetrainingStatus.FAILED and failed.failure_reason == "boom"
    assert registry_store.live_request(NAME) is None
    third, created3 = registry_store.request_retraining(
        NAME, ["new_labels"], {}, date(2026, 8, 3), None, None
    )
    assert created3 and [r.request_id for r in registry_store.requests(NAME)] == [
        first.request_id,
        third.request_id,
    ]
    assert registry_store.get_request(third.request_id) == third
    # re-running the day that made the (now finished) first request does not duplicate it
    rerun, created4 = registry_store.request_retraining(
        NAME, ["residual_drift"], {"x": 1}, date(2026, 8, 1), None, None
    )
    assert not created4 and rerun.request_id == first.request_id
    with pytest.raises(RegistryError, match="unknown"):
        registry_store.advance_request("rt-missing", "started")


def test_completed_request_must_name_a_registered_candidate(
    registry_store: RegistryStore,
) -> None:
    registry_store.register(record(), AUDIT)
    req, _ = registry_store.request_retraining(NAME, ["manual"], {}, date(2026, 8, 1), None, None)
    registry_store.advance_request(req.request_id, "started")
    with pytest.raises(exc.DBAPIError):
        registry_store.advance_request(req.request_id, "completed")  # no candidate: CHECK
    done = registry_store.advance_request(
        req.request_id, "completed", candidate_version=record().model_version
    )
    assert done.candidate_version == record().model_version


def test_reports_are_idempotent_and_append_only(registry_store: RegistryStore) -> None:
    v = record().model_version
    registry_store.register(record(), AUDIT)
    rid = registry_store.add_validation_report(v, ["latency_budget"], {"checks": []})
    assert rid == registry_store.add_validation_report(v, ["latency_budget"], {"checks": []})
    assert registry_store.validation_reports(v) == [{"checks": []}]
    report = MonitoringReport(
        "drift-1",
        NAME,
        v,
        "drift",
        date(2026, 8, 1),
        date(2026, 7, 18),
        date(2026, 8, 1),
        True,
        {"drift_fired": ["residual_drift"]},
    )
    assert registry_store.add_monitoring_report(report)
    assert not registry_store.add_monitoring_report(report)
    assert registry_store.monitoring_reports(v, "drift") == [report]
    assert registry_store.monitoring_reports(v, "shadow") == []
    registry_store.set_tracking_run(v, "run-1")
    assert registry_store.require(v).tracking_run_id == "run-1"
    assert replace(registry_store.require(v).record) == record()
