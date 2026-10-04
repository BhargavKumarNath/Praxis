"""HTTP contract of /v1/forecasts/demand: success, validation, explicit errors, metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from praxis.api.app import create_app
from praxis.api.forecast import build_forecast_service
from praxis.config import Settings
from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.service import ForecastService
from praxis.tracing import CORRELATION_HEADER, new_correlation_id
from tests.forecasting.helpers import make_plan, write_warehouse
from tests.forecasting.test_service import PANEL, PanelSource, clock

URL = "/v1/forecasts/demand"


def client_for(artifact: ForecastArtifact, lag_days: int = 1) -> TestClient:
    svc = ForecastService(artifact, PanelSource(), clock=lambda: clock(lag_days))
    return TestClient(create_app(Settings(), forecast_service=svc))


@pytest.fixture
def client(artifact: ForecastArtifact) -> TestClient:
    return client_for(artifact)


def test_forecast_response_contract(client: TestClient, artifact: ForecastArtifact) -> None:
    cid = new_correlation_id()
    resp = client.post(URL, json={}, headers={CORRELATION_HEADER: cid})
    assert resp.status_code == 200
    assert resp.headers[CORRELATION_HEADER] == cid
    body = resp.json()
    assert body["is_synthetic"] is True
    assert body["model_version"] == artifact.model_version
    assert body["feature_version"] == artifact.manifest["feature_version"]
    assert body["freshness_status"] == "fresh" and body["source"] == "model"
    assert body["feature_date"] == PANEL.end_date.isoformat()
    assert {"forecast_created_at", "feature_lag_days", "quantile_levels"} <= set(body)
    assert len(body["forecasts"]) == len(PANEL.series) * 7
    point = body["forecasts"][0]
    assert set(point) == {
        "region_id",
        "product",
        "segment",
        "horizon_days",
        "target_date",
        "point",
        "quantiles",
    }
    assert set(point["quantiles"]) == {"0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"}


def test_batch_request_for_selected_series(client: TestClient) -> None:
    s = PANEL.series[1]
    body: dict[str, Any] = {
        "series": [{"region_id": s.region_id, "product": s.product, "segment": s.segment}],
        "horizons": [1, 2],
    }
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert [(f["horizon_days"], f["segment"]) for f in resp.json()["forecasts"]] == [
        (1, s.segment),
        (2, s.segment),
    ]


def test_stale_response_is_flagged(artifact: ForecastArtifact) -> None:
    body = client_for(artifact, lag_days=4).post(URL, json={}).json()
    assert body["freshness_status"] == "stale"
    assert body["source"] == "fallback_baseline"
    assert body["fallback_reason"] == "stale_features"


@pytest.mark.parametrize(
    "payload",
    [
        {"horizons": [9]},
        {"horizons": []},
        {"series": [{"region_id": "R!", "product": "p", "segment": "s"}]},
        {"series": [{"region_id": "r", "product": "p"}]},
        {"planned_prices_micros": {"p_api": -5}},
        {"surprise": True},
        [],
    ],
)
def test_malformed_input_gets_an_explicit_422(client: TestClient, payload: Any) -> None:
    resp = client.post(URL, json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "invalid_request"
    assert body["correlation_id"] == resp.headers[CORRELATION_HEADER]
    assert "Traceback" not in resp.text


def test_unknown_series_is_404_and_bad_price_is_400(client: TestClient) -> None:
    resp = client.post(
        URL, json={"series": [{"region_id": "mars", "product": "p_api", "segment": "s_big"}]}
    )
    assert resp.status_code == 404 and resp.json()["error"] == "unknown_series"
    resp = client.post(URL, json={"planned_prices_micros": {"p_api": 99_999}})
    assert resp.status_code == 400 and resp.json()["error"] == "planned_price_out_of_range"


def test_expired_features_are_503(artifact: ForecastArtifact) -> None:
    resp = client_for(artifact, lag_days=30).post(URL, json={})
    assert resp.status_code == 503
    assert resp.json()["error"] == "features_unavailable"


def test_no_model_loaded_is_503_everywhere() -> None:
    client = TestClient(create_app(Settings()))
    for method, path in (("post", URL), ("get", f"{URL}/model"), ("get", f"{URL}/metrics")):
        resp = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 503
        assert resp.json()["error"] == "forecast_unavailable"


def test_model_info_and_metrics_endpoints(client: TestClient, artifact: ForecastArtifact) -> None:
    info = client.get(f"{URL}/model").json()
    assert info["model_version"] == artifact.model_version
    assert info["data_version"] == artifact.manifest["data_version"]
    assert info["series"] == len(PANEL.series)
    assert info["backtest_acceptance_passed"] is None
    client.post(URL, json={})
    metrics = client.get(f"{URL}/metrics").json()
    assert metrics["model_version"] == artifact.model_version
    assert metrics["counters"]["forecast_requests_total"] == 1


def test_settings_wire_a_real_artifact_and_warehouse(artifact_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    write_warehouse(db, PANEL, make_plan())
    svc = build_forecast_service(Settings(forecast_model_dir=artifact_dir, warehouse_path=db))
    assert svc is not None
    assert build_forecast_service(Settings()) is None
    assert build_forecast_service(Settings(forecast_model_dir=tmp_path / "missing")) is None
