"""Registry-backed serving: champion pointer, rollback swap, refusals, shadow isolation, API."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from praxis.api.app import create_app
from praxis.api.forecast import MODEL_VERSION_HEADER, build_forecast_service
from praxis.config import Settings
from praxis.domain.model_lifecycle import ModelStage
from praxis.forecasting.artifact import ArtifactError, ForecastArtifact
from praxis.forecasting.features import FEATURE_VERSION
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.serving import RegistryForecaster, ServingUnavailable, artifact_path
from praxis.mlops.shadow import ShadowRecorder
from praxis.mlops.store import RegistryStore, VersionRecord
from praxis.mlops.training import Candidate
from tests.mlops.conftest import World
from tests.mlops.test_registry import passed, shadow_report

NAME = "demand_forecaster"


def at(world: World, day: int) -> Callable[[], datetime]:
    moment = datetime.combine(world.date(day), time(6), tzinfo=UTC)
    return lambda: moment


def router(store: RegistryStore, world: World, **kw: Any) -> RegistryForecaster:
    clock = at(world, 150)
    src = WarehouseFeatureSource(world.clean)
    return RegistryForecaster(
        store, NAME, src, service_factory=lambda art, s: ForecastService(art, s, clock=clock), **kw
    )


def bootstrap(registry: ModelRegistry, c: Candidate) -> None:
    registry.register(c.record)
    registry.record_validation(c.record.model_version, passed(c.record.model_version))
    registry.promote(c.record.model_version, approver="owner", reason="bootstrap")


def to_shadow(registry: ModelRegistry, c: Candidate, world: World) -> None:
    registry.register(c.record)
    registry.record_validation(c.record.model_version, passed(c.record.model_version))
    registry.start_shadow(c.record.model_version, as_of=world.date(140))


def test_no_champion_means_unavailable(registry: ModelRegistry, world: World) -> None:
    r = router(registry.store, world)
    with pytest.raises(ServingUnavailable):
        r.forecast()
    assert r.status()["champion"] is None
    assert r.counters["refresh_no_champion_total"] == 1


def test_serves_the_champion_and_respects_the_refresh_interval(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate
) -> None:
    now = [0.0]
    r = router(registry.store, world, refresh_interval_s=30, monotonic=lambda: now[0])
    bootstrap(registry, champion)
    result = r.forecast()
    assert result.model_version == champion.record.model_version == r.model_version
    # a new champion appears; within the interval the router does not look
    to_shadow(registry, good, world)
    registry.complete_shadow(
        good.record.model_version,
        shadow_report(champion.record.model_version, good.record.model_version, ok=True),
    )
    registry.promote(good.record.model_version, approver="owner", reason="ok")
    now[0] = 10.0
    assert r.forecast().model_version == champion.record.model_version
    now[0] = 31.0
    assert r.forecast().model_version == good.record.model_version
    assert r.counters["refresh_swapped_total"] == 2


def test_rollback_restores_the_previous_version_in_serving(
    registry: ModelRegistry, world: World, champion: Candidate, bad: Candidate
) -> None:
    r = router(registry.store, world)
    bootstrap(registry, champion)
    registry.register(bad.record)
    registry.force_promote(bad.record.model_version, actor="drill", reason="controlled bad")
    r.refresh(force=True)
    assert r.forecast().model_version == bad.record.model_version
    registry.rollback(actor="oncall", reason="probation regression")
    assert r.refresh(force=True)
    served = r.forecast()
    assert served.model_version == champion.record.model_version
    assert served.feature_version == FEATURE_VERSION
    # no serving stage still holds the rolled-back version
    live = {
        row.model_version
        for stage in (ModelStage.CHAMPION, ModelStage.SHADOW, ModelStage.CANARY)
        for row in registry.store.in_stage(NAME, stage)
    }
    assert live == {champion.record.model_version}


def _resealed(c: Candidate, tmp: Path, feature_version: str | None = None) -> VersionRecord:
    """A copy whose registry row does not match what is on disk."""
    dst = tmp / c.path.name
    shutil.copytree(c.path, dst)
    if feature_version is not None:
        manifest = json.loads((dst / "manifest.json").read_text())
        manifest["feature_version"] = feature_version
        (dst / "manifest.json").write_text(json.dumps(manifest))
    return replace(c.record, artifact_uri=dst.resolve().as_uri())


@pytest.mark.parametrize("problem", ["checksum", "feature_contract", "missing"])
def test_unservable_champion_is_refused_and_last_good_keeps_serving(
    make_registry: Callable[..., ModelRegistry],
    world: World,
    champion: Candidate,
    good: Candidate,
    tmp_path: Path,
    problem: str,
) -> None:
    registry = make_registry()
    r = router(registry.store, world)
    bootstrap(registry, champion)
    r.forecast()
    record = {
        "checksum": replace(good.record, artifact_checksum="f" * 64),
        "feature_contract": _resealed(good, tmp_path, feature_version="demand_features.v0"),
        "missing": replace(good.record, artifact_uri=(tmp_path / "gone").as_uri()),
    }[problem]
    registry.register(record)
    registry.record_validation(record.model_version, passed(record.model_version))
    registry.start_shadow(record.model_version, as_of=world.date(140))
    registry.complete_shadow(
        record.model_version,
        shadow_report(champion.record.model_version, record.model_version, ok=True),
    )
    registry.promote(record.model_version, approver="owner", reason="test")
    assert not r.refresh(force=True)
    assert r.forecast().model_version == champion.record.model_version
    assert r.counters["refresh_refused_total:champion"] == 1


def test_shadow_sees_the_same_snapshot_and_never_changes_the_answer(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate
) -> None:
    recorder = ShadowRecorder(capacity=2)
    r = router(registry.store, world, recorder=recorder)
    bootstrap(registry, champion)
    alone = r.forecast()
    to_shadow(registry, good, world)
    r.refresh(force=True)
    assert r.shadow_version == good.record.model_version
    with_shadow = [r.forecast() for _ in range(3)]
    r.drain()
    for result in with_shadow:
        assert result.model_version == champion.record.model_version
        assert [p.point for p in result.points] == [p.point for p in alone.points]
    snap = recorder.snapshot()
    assert snap["counters"]["shadow_pairs_total"] == 3
    assert all(
        p["same_inputs"] and p["shadow"] == good.record.model_version for p in snap["recent"]
    )
    assert len(recorder.pairs) == 2  # bounded
    assert r.status()["shadow"] == good.record.model_version
    r.close()


def test_failing_shadow_is_counted_not_raised(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate
) -> None:
    clock = at(world, 150)
    calls = {"n": 0}

    class Exploding(ForecastService):
        def forecast(self, *a: Any, **kw: Any) -> Any:
            calls["n"] += 1
            raise RuntimeError("shadow model crashed")

    def factory(art: ForecastArtifact, src: Any) -> ForecastService:
        cls = Exploding if art.model_version == good.record.model_version else ForecastService
        return cls(art, src, clock=clock)

    r = RegistryForecaster(
        registry.store, NAME, WarehouseFeatureSource(world.clean), service_factory=factory
    )
    bootstrap(registry, champion)
    to_shadow(registry, good, world)
    assert r.forecast().model_version == champion.record.model_version
    r.drain()
    assert calls["n"] == 1
    assert r.recorder.snapshot()["counters"] == {"shadow_failures_total:RuntimeError": 1}


def test_artifact_uri_must_be_local() -> None:
    assert artifact_path("file:///models/a%20b") == Path("/models/a b")
    with pytest.raises(ArtifactError, match="unsupported"):
        artifact_path("gs://bucket/model")


def test_api_exposes_the_served_registry_version(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate
) -> None:
    r = router(registry.store, world)
    client = TestClient(create_app(Settings(), forecast_service=r))
    assert client.get("/v1/forecasts/demand/model").status_code == 503
    assert client.post("/v1/forecasts/demand", json={}).status_code == 503
    assert client.get("/v1/forecasts/demand/metrics").status_code == 503
    bootstrap(registry, champion)
    to_shadow(registry, good, world)
    r.refresh(force=True)
    info = client.get("/v1/forecasts/demand/model")
    assert info.status_code == 200
    body = info.json()
    assert body["model_version"] == champion.record.model_version
    assert info.headers[MODEL_VERSION_HEADER] == champion.record.model_version
    assert body["served_from"] == "registry" and body["stage"] == "champion"
    assert body["shadow_version"] == good.record.model_version
    resp = client.post("/v1/forecasts/demand", json={"horizons": [1]})
    assert resp.status_code == 200
    assert resp.headers[MODEL_VERSION_HEADER] == champion.record.model_version
    assert resp.json()["model_version"] == champion.record.model_version
    metrics = client.get("/v1/forecasts/demand/metrics").json()
    assert metrics["registry"]["champion"] == champion.record.model_version
    r.drain()


def test_api_from_directory_reports_no_stage(champion: Candidate, world: World) -> None:
    svc = ForecastService(
        champion.artifact, WarehouseFeatureSource(world.clean), clock=at(world, 150)
    )
    body = (
        TestClient(create_app(Settings(), forecast_service=svc))
        .get("/v1/forecasts/demand/model")
        .json()
    )
    assert body["served_from"] == "directory" and body["stage"] is None


def test_settings_select_the_registry_provider(
    pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    provider = build_forecast_service(
        Settings(forecast_from_registry=True, database_url=SecretStr(pg_url))
    )
    assert isinstance(provider, RegistryForecaster)
    assert build_forecast_service(Settings(forecast_from_registry=True)) is None
    assert "needs PRAXIS_DATABASE_URL" in caplog.text
