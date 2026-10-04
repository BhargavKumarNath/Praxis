"""Demand-forecast HTTP API: ``/v1/forecasts/demand`` (read-only; SYNTHETIC data)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from praxis.config import Settings
from praxis.forecasting.artifact import ArtifactError, load_artifact
from praxis.forecasting.panel import SeriesKey
from praxis.forecasting.service import (
    ForecastRequestError,
    ForecastResult,
    ForecastService,
    ForecastUnavailable,
    WarehouseFeatureSource,
)
from praxis.tracing import current_correlation_id

logger = logging.getLogger(__name__)

Ident = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeriesRef(_Strict):
    region_id: Ident
    product: Ident
    segment: Ident


class DemandForecastRequest(_Strict):
    series: list[SeriesRef] | None = Field(default=None, min_length=1, max_length=1000)
    horizons: list[Annotated[int, Field(ge=1, le=7)]] | None = Field(
        default=None, min_length=1, max_length=7
    )
    planned_prices_micros: dict[Ident, Annotated[int, Field(gt=0, le=10**12)]] | None = Field(
        default=None, max_length=50
    )


class ForecastPointOut(BaseModel):
    region_id: str
    product: str
    segment: str
    horizon_days: int
    target_date: date
    point: float
    quantiles: dict[str, float]


class DemandForecastResponse(BaseModel):
    is_synthetic: bool = True
    units: str = "requested units per UTC day (served + throttled)"
    model_name: str
    model_version: str
    feature_version: str
    forecast_created_at: datetime
    feature_date: date
    feature_timestamp: datetime
    feature_lag_days: int
    freshness_status: str
    source: str
    fallback_reason: str | None
    quantile_levels: list[float]
    forecasts: list[ForecastPointOut]


class ModelInfo(BaseModel):
    model_name: str
    model_version: str
    champion: dict[str, str]
    feature_version: str
    data_version: str
    code_revision: str
    created_at: datetime
    quantiles: list[float]
    horizons: list[int]
    series: int
    backtest_acceptance_passed: bool | None


def _error(status: int, code: str, detail: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code, "correlation_id": current_correlation_id()}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def _to_response(result: ForecastResult) -> DemandForecastResponse:
    return DemandForecastResponse(
        model_name=result.model_name,
        model_version=result.model_version,
        feature_version=result.feature_version,
        forecast_created_at=result.created_at,
        feature_date=result.feature_date,
        feature_timestamp=result.feature_cutoff,
        feature_lag_days=result.feature_lag_days,
        freshness_status=result.freshness.value,
        source=result.source.value,
        fallback_reason=result.fallback_reason,
        quantile_levels=list(result.quantile_levels),
        forecasts=[
            ForecastPointOut(
                region_id=p.series.region_id,
                product=p.series.product,
                segment=p.series.segment,
                horizon_days=p.horizon_days,
                target_date=p.target_date,
                point=round(p.point, 4),
                quantiles={f"{q:g}": round(v, 4) for q, v in p.quantiles.items()},
            )
            for p in result.points
        ],
    )


def build_forecast_service(settings: Settings) -> ForecastService | None:
    """Load the configured artifact; ``None`` (endpoints answer 503) if absent or invalid."""
    if settings.forecast_model_dir is None:
        return None
    try:
        artifact = load_artifact(settings.forecast_model_dir)
    except ArtifactError:
        logger.exception("forecast artifact rejected; forecasting disabled")
        return None
    return ForecastService(artifact, WarehouseFeatureSource(settings.warehouse_path))


def forecast_router(service: ForecastService | None) -> APIRouter:
    router = APIRouter(prefix="/v1/forecasts/demand", tags=["forecasting"])

    @router.post(
        "",
        response_model=DemandForecastResponse,
        responses={400: {}, 404: {}, 422: {}, 503: {}},
    )
    def forecast(body: DemandForecastRequest) -> Any:
        if service is None:
            return _error(503, "forecast_unavailable", "no forecast model is loaded")
        series = (
            [SeriesKey(s.region_id, s.product, s.segment) for s in body.series]
            if body.series
            else None
        )
        try:
            result = service.forecast(series, body.horizons, body.planned_prices_micros)
        except ForecastRequestError as exc:
            status = 404 if exc.code == "unknown_series" else 400
            return _error(status, exc.code, str(exc))
        except ForecastUnavailable as exc:
            return _error(503, exc.code, str(exc))
        return _to_response(result)

    @router.get("/model", response_model=ModelInfo, responses={503: {}})
    def model_info() -> Any:
        if service is None:
            return _error(503, "forecast_unavailable", "no forecast model is loaded")
        m = service.artifact.manifest
        backtest = m.get("backtest") or {}
        acceptance = backtest.get("acceptance") or {}
        return ModelInfo(
            model_name=m["model_name"],
            model_version=m["model_version"],
            champion=m["champion"],
            feature_version=m["feature_version"],
            data_version=m["data_version"],
            code_revision=m["code_revision"],
            created_at=m["created_at"],
            quantiles=m["quantiles"],
            horizons=m["horizons"],
            series=len(m["series"]),
            backtest_acceptance_passed=acceptance.get("passed"),
        )

    @router.get("/metrics")
    def metrics() -> Any:
        if service is None:
            return _error(503, "forecast_unavailable", "no forecast model is loaded")
        return {"model_version": service.model_version, **service.metrics.snapshot()}

    return router
