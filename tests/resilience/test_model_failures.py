"""Phase 15 failures 7-9: model artifact missing, stale features, failed candidate.

Real boundary: the Postgres model registry, artifacts on the filesystem, the DuckDB feature
warehouse and the production API (``RegistryForecaster`` behind ``POST /v1/forecasts/demand``).
The MLOps test world (``tests/mlops``) supplies trained, SYNTHETIC candidates.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.api.forecast import MODEL_VERSION_HEADER, build_forecast_service
from praxis.config import Settings
from praxis.domain.model_lifecycle import ModelStage
from praxis.forecasting.service import ForecastService, WarehouseFeatureSource
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.serving import RegistryForecaster, artifact_path
from praxis.mlops.training import Candidate
from praxis.mlops.validation import Gate, validate_candidate
from praxis.telemetry import Telemetry
from tests.mlops import helpers as h
from tests.mlops.conftest import World
from tests.mlops.test_pipeline import FIRST, lifecycle
from tests.mlops.test_pipeline import bootstrap as validated_bootstrap
from tests.mlops.test_registry import passed, shadow_report
from tests.resilience.harness import Measured, OpsProbe, detection, judge, quiet_fired

NAME = "demand_forecaster"


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _router(registry: ModelRegistry, world: World, clock: Clock) -> RegistryForecaster:
    return RegistryForecaster(
        registry.store, NAME, WarehouseFeatureSource(world.clean), refresh_interval_s=0.0,
        service_factory=lambda art, src: ForecastService(art, src, clock=clock),
    )  # fmt: skip


def _promote(registry: ModelRegistry, c: Candidate, world: World, uri: str | None = None) -> str:
    record = c.record if uri is None else replace(c.record, artifact_uri=uri)
    registry.register(record)
    registry.record_validation(record.model_version, passed(record.model_version))
    if registry.store.one_in_stage(NAME, ModelStage.CHAMPION) is not None:
        registry.start_shadow(record.model_version, as_of=world.date(140))
        champion_now = registry.store.one_in_stage(NAME, ModelStage.CHAMPION)
        assert champion_now is not None
        registry.complete_shadow(record.model_version, shadow_report(
            champion_now.model_version, record.model_version, ok=True))  # fmt: skip
    registry.promote(record.model_version, approver="owner", reason="phase 15 drill")
    return record.model_version


def _call(client: TestClient, n: int) -> list[tuple[int, str | None]]:
    out = []
    for _ in range(n):
        r = client.post("/v1/forecasts/demand", json={})
        out.append((r.status_code, r.headers.get(MODEL_VERSION_HEADER)))
    return out


def test_model_missing(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate, tmp_path: Path
) -> None:
    fid = "model_missing"
    clock = Clock(datetime.combine(world.date(150), time(6), tzinfo=UTC))
    v_old = _promote(registry, champion, world)
    r = _router(registry, world, clock)
    client = TestClient(create_app(Settings(), forecast_service=r))
    probe = OpsProbe()
    assert {c for c, _ in _call(client, 3)} == {200}
    probe.tick()

    # Inject A: a new champion is promoted and its artifact directory is then deleted.
    home = tmp_path / "registry-artifacts" / good.record.model_version
    shutil.copytree(artifact_path(good.record.artifact_uri), home)
    v_new = _promote(registry, good, world, uri=home.resolve().as_uri())
    shutil.rmtree(home)
    probe.mark()
    served_a = _call(client, 25)  # the last good version keeps answering
    probe.tick()
    ticks_a, wall_a = detection(probe, ("model_unservable",))

    # Inject B: the API process restarts; no version can be loaded at all.
    r2 = _router(registry, world, clock)
    client2 = TestClient(create_app(Settings(), forecast_service=r2))
    probe.mark()
    served_b = _call(client2, 25)
    # ... and an API configured with a model directory that does not exist
    directory_api = build_forecast_service(Settings(forecast_model_dir=tmp_path / "nowhere"))
    served_dir = _call(TestClient(create_app(Settings(), forecast_service=directory_api)), 5)
    probe.tick()
    ticks_b, wall_b = detection(probe, ("inference_failures",))

    # Recover: the exact artifact is restored (same checksum); both processes load it.
    shutil.copytree(artifact_path(good.record.artifact_uri), home)
    recovered = _call(client, 25) + _call(client2, 25)
    clear = probe.until_clear(("model_unservable", "inference_failures"), max_ticks=5)

    known = {v_old, v_new}
    ok_versions = [v for c, v in served_a + served_b + recovered if c == 200]
    wrong = sum(1 for v in ok_versions if v not in known)
    record = judge(Measured(
        fid,
        actual=(f"after deletion: {len(served_a)} calls -> {sorted({c for c, _ in served_a})} "
                f"served by {sorted({v for _, v in served_a if v})} (registry champion {v_new}); "
                f"restarted API: {sorted({c for c, _ in served_b})}; missing model dir: "
                f"{sorted({c for c, _ in served_dir})}"),
        detection_ticks=ticks_a | ticks_b, detection_wall_ms=wall_a | wall_b,
        quiet_fired=quiet_fired(probe, fid),
        recovered=(all(c == 200 for c, _ in served_a) and {v for _, v in served_a} == {v_old}
                   and {c for c, _ in served_b} == {503} and {c for c, _ in served_dir} == {503}
                   and all(c == 200 and v == v_new for c, v in recovered)),
        recovery_actual=f"after restore: {sorted({(c, v) for c, v in recovered})}",
        clear_ticks=clear, data_loss=wrong,
        # exactly two champion moves happened (old, new); any other is an unrequested one
        duplicate_side_effects=sum(1 for e in registry.store.events(model_name=NAME)
                                   if e.to_stage == ModelStage.CHAMPION) - 2,
        evidence={"refused_counter": dict(r.counters), "restarted_counter": dict(r2.counters),
                  "incidents_notified": [(e, i.rule_id) for e, i in probe.notifier.sent]},
    ))  # fmt: skip
    assert record.passed, record.reasons


def test_feature_stale(
    registry: ModelRegistry, world: World, champion: Candidate, tel: Telemetry
) -> None:
    fid = "feature_stale"
    _promote(registry, champion, world)
    # The world's warehouse ends on its last day; serving after it makes the features age.
    clock = Clock(
        datetime.combine(world.date(h.N_DAYS - 1) + timedelta(days=1), time(6), tzinfo=UTC)
    )
    r = _router(registry, world, clock)
    client = TestClient(create_app(Settings(), forecast_service=r))
    serving = r.artifact.config.serving
    feature_day = r._require().champion.fetch_snapshot().feature_date
    at_lag: Callable[[int], datetime] = lambda lag: datetime.combine(  # noqa: E731
        feature_day + timedelta(days=lag), time(6), tzinfo=UTC)  # fmt: skip
    probe = OpsProbe()
    clock.at = at_lag(serving.fresh_max_lag_days)
    fresh = client.post("/v1/forecasts/demand", json={}).json()
    probe.tick()

    # Inject 1: the newest complete feature day is older than the fresh limit.
    clock.at = at_lag(serving.fresh_max_lag_days + 1)
    probe.mark()
    stale = [client.post("/v1/forecasts/demand", json={}) for _ in range(25)]
    probe.tick()
    ticks_1, wall_1 = detection(probe, ("forecast_stale",))
    first_stale_tick_alerts = probe.firing()

    # Inject 2: older than the stale limit.
    clock.at = at_lag(serving.stale_max_lag_days + 1)
    probe.mark()
    gone = [client.post("/v1/forecasts/demand", json={}) for _ in range(25)]
    probe.tick()
    ticks_2, wall_2 = detection(probe, ("inference_failures",))

    # Recover: fresh features again.
    clock.at = at_lag(serving.fresh_max_lag_days)
    back = [client.post("/v1/forecasts/demand", json={}) for _ in range(25)]
    clear = probe.until_clear(("forecast_stale", "inference_failures"), max_ticks=5)

    stale_ok = all(s.status_code == 200 and s.json()["freshness_status"] == "stale"
                   and s.json()["source"] == "fallback_baseline" for s in stale)  # fmt: skip
    record = judge(Measured(
        fid,
        actual=(f"fresh: {fresh['freshness_status']}/{fresh['source']}; lag "
                f"{serving.fresh_max_lag_days + 1} d: {sorted({s.status_code for s in stale})} "
                f"{sorted({s.json().get('source', '') for s in stale})}, alerts on the next tick "
                f"{first_stale_tick_alerts}; lag {serving.stale_max_lag_days + 1} d: "
                f"{sorted({g.status_code for g in gone})} "
                f"{sorted({g.json().get('error', '') for g in gone})}"),
        detection_ticks=ticks_1 | ticks_2, detection_wall_ms=wall_1 | wall_2,
        quiet_fired=quiet_fired(probe, fid),
        recovered=(stale_ok and {g.status_code for g in gone} == {503}
                   and all(b.status_code == 200 and b.json()["freshness_status"] == "fresh"
                           for b in back)),
        recovery_actual=(
            f"fresh again: {sorted({(b.status_code, b.json().get('source')) for b in back})}"),
        clear_ticks=clear, data_loss=0, duplicate_side_effects=0,
        evidence={"feature_date": feature_day.isoformat(),
                  "fresh_max_lag_days": serving.fresh_max_lag_days,
                  "stale_max_lag_days": serving.stale_max_lag_days,
                  "forecast_freshness_slo_max_h": next(
                      s.max_value for s in probe.cfg.slos if s.id == "forecast_freshness"),
                  "forecast_age_hours_at_first_stale_day": (serving.fresh_max_lag_days + 1) * 24,
                  "alert_history": [(t.isoformat(), a) for t, a in probe.history]},
    ))  # fmt: skip
    assert record.passed, record.reasons


def test_failed_candidate(
    registry: ModelRegistry, world: World, champion: Candidate, good: Candidate, tmp_path: Path
) -> None:
    fid = "failed_candidate"
    lc = lifecycle(registry, world.clean, tmp_path)
    validated_bootstrap(lc, champion, world)
    clock = Clock(datetime.combine(world.date(150), time(6), tzinfo=UTC))
    r = _router(registry, world, clock)
    client = TestClient(create_app(Settings(), forecast_service=r))
    before = _call(client, 3)
    probe = OpsProbe()
    probe.tick()
    probe.mark()

    # Candidate 1: an artifact corrupted on disk (one model file byte flipped).
    corrupt = tmp_path / "corrupt" / good.record.model_version
    shutil.copytree(good.path, corrupt)
    victim = next(p for p in sorted(corrupt.iterdir()) if p.name != "manifest.json" and p.is_file())
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))
    rec = replace(good.record, artifact_uri=corrupt.resolve().as_uri())
    registry.register(rec)
    report = validate_candidate(
        corrupt, rec, warehouse=world.clean, as_of=world.date(FIRST + 28),
        gate=Gate(registry.config, h.forecast_config().acceptance),
        backtest=good.backtest, champion=None,
    )  # fmt: skip
    row_1 = registry.record_validation(rec.model_version, report)

    # Candidate 2: a bad recipe that fails the acceptance metrics.
    lc.request(world.date(140), "phase 15 failed candidate drill")
    out = lc.retrain(world.date(140), forecast_config=h.bad_forecast_config())
    assert out is not None
    probe.tick()

    after = _call(client, 3)
    champion_row = registry.store.one_in_stage(NAME, ModelStage.CHAMPION)
    rejected = [e for e in registry.store.events(model_name=NAME)
                if e.to_stage == ModelStage.REJECTED]  # fmt: skip
    served = {v for _, v in before + after}
    record = judge(Measured(
        fid,
        actual=(f"corrupted artifact -> stage {row_1.stage.value} "
                f"(failed {report['failed']}); "
                f"bad recipe -> stage {out['stage']} (failed {out['failed_checks']}); "
                f"served versions before/after {sorted(str(v) for v in served)}"),
        detection_ticks={}, detection_wall_ms={}, quiet_fired=quiet_fired(probe, fid),
        recovered=(champion_row is not None
                   and champion_row.model_version == champion.record.model_version
                   and served == {champion.record.model_version}
                   and all(c == 200 for c, _ in before + after)),
        recovery_actual="champion pointer and served version unchanged",
        clear_ticks=None, data_loss=0,
        duplicate_side_effects=sum(1 for e in registry.store.events(model_name=NAME)
                                   if e.to_stage == ModelStage.CHAMPION) - 1,
        signal_observed=len(rejected), signal_expected=2,
        evidence={"rejected": [(e.model_version, e.actor, e.reason) for e in rejected]},
    ))  # fmt: skip
    assert record.passed, record.reasons
