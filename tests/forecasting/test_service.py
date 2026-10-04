"""Serving: version, freshness policy, stale fallback, validation, metrics, latency budget."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.features import WINDOW, FeatureFrame
from praxis.forecasting.models import Prediction
from praxis.forecasting.panel import DemandPanel, SeriesKey
from praxis.forecasting.service import (
    ForecastRequestError,
    ForecastService,
    ForecastUnavailable,
    Freshness,
    Snapshot,
    Source,
    WarehouseFeatureSource,
)
from praxis.forecasting.warehouse import WarehouseUnavailable
from tests.forecasting.conftest import N_DAYS
from tests.forecasting.helpers import make_panel, make_plan, write_warehouse

PANEL = make_panel(n_days=N_DAYS)
FEATURE_DATE = PANEL.end_date


class PanelSource:
    """Feature source over a known panel: the latest WINDOW days up to ``not_after``."""

    def __init__(self, panel: DemandPanel = PANEL, fail: bool = False) -> None:
        self.panel = panel
        self.fail = fail
        self.calls = 0

    def snapshot(self, series: Sequence[SeriesKey], *, not_after: date) -> Snapshot | None:
        self.calls += 1
        if self.fail:
            raise WarehouseUnavailable("warehouse not found: /nowhere")
        last = min(self.panel.day_of(not_after), self.panel.n_days - 1)
        if last < WINDOW - 1:
            return None
        hist = self.panel.history(last)
        start = last - WINDOW + 1
        window = DemandPanel(
            start_date=hist.date_of(start),
            series=hist.series,
            regions=hist.regions,
            demand=hist.demand[:, start:],
            served=hist.served[:, start:],
            active=hist.active[:, start:],
            context={k: v[:, start:] for k, v in hist.context.items()},
        )
        return Snapshot(window, make_plan())


def clock(lag_days: int) -> datetime:
    return datetime.combine(FEATURE_DATE + timedelta(days=lag_days), time(6), tzinfo=UTC)


def service(
    artifact: ForecastArtifact, lag_days: int = 1, source: PanelSource | None = None
) -> ForecastService:
    return ForecastService(artifact, source or PanelSource(), clock=lambda: clock(lag_days))


def test_fresh_features_are_served_by_the_model(artifact: ForecastArtifact) -> None:
    svc = service(artifact, lag_days=1)
    result = svc.forecast()
    assert result.model_version == artifact.model_version
    assert result.freshness is Freshness.FRESH and result.source is Source.MODEL
    assert result.fallback_reason is None
    assert result.feature_date == FEATURE_DATE
    assert result.feature_cutoff == datetime.combine(
        FEATURE_DATE + timedelta(days=1), time(0), tzinfo=UTC
    )
    assert result.feature_lag_days == 1
    assert len(result.points) == len(PANEL.series) * 7
    p = result.points[0]
    assert p.target_date == FEATURE_DATE + timedelta(days=p.horizon_days)
    assert list(p.quantiles) == list(artifact.config.target.quantiles)
    values = list(p.quantiles.values())
    assert values == sorted(values) and min(values) >= 0


def test_model_forecast_matches_offline_prediction(artifact: ForecastArtifact) -> None:
    from praxis.forecasting.features import build_features

    result = service(artifact).forecast(horizons=[2])
    frame = build_features(PANEL, N_DAYS - 1, (2,), make_plan(), artifact.catalogue)
    offline = artifact.model.predict(frame).point
    served = np.array([p.point for p in sorted(result.points, key=lambda p: p.series)])
    order = np.argsort([PANEL.series[i] for i in frame.series_idx.tolist()], kind="stable")
    np.testing.assert_allclose(served, offline[order])


@pytest.mark.parametrize("lag", [2, 7])
def test_stale_features_fall_back_to_the_baseline(artifact: ForecastArtifact, lag: int) -> None:
    svc = service(artifact, lag_days=lag)
    result = svc.forecast()
    assert result.freshness is Freshness.STALE
    assert result.source is Source.FALLBACK
    assert result.fallback_reason == "stale_features"
    assert result.feature_lag_days == lag
    snap = svc.metrics.snapshot()
    assert snap["counters"]["forecast_source_total:fallback_baseline"] == 1
    assert snap["counters"]["forecast_reason_total:stale_features"] == 1


def test_stale_fallback_equals_the_seasonal_moving_average(artifact: ForecastArtifact) -> None:
    result = service(artifact, lag_days=3).forecast(horizons=[1])
    for p in result.points:
        i = PANEL.series.index(p.series)
        d = N_DAYS - 1 + 1
        expected = np.mean([PANEL.demand[i, d - 7 * k] for k in (1, 2, 3, 4)])
        assert p.point == pytest.approx(expected)


def test_expired_features_are_rejected_not_forecast(artifact: ForecastArtifact) -> None:
    svc = service(artifact, lag_days=8)
    with pytest.raises(ForecastUnavailable) as err:
        svc.forecast()
    assert err.value.code == "features_unavailable"
    assert svc.metrics.snapshot()["counters"]["forecast_outcome_total:unavailable"] == 1


def test_no_complete_day_or_no_warehouse_is_unavailable(artifact: ForecastArtifact) -> None:
    early = ForecastService(artifact, PanelSource(), clock=lambda: datetime(2026, 1, 6, tzinfo=UTC))
    with pytest.raises(ForecastUnavailable, match="no complete feature day"):
        early.forecast()
    broken = service(artifact, source=PanelSource(fail=True))
    with pytest.raises(ForecastUnavailable) as err:
        broken.forecast()
    assert err.value.code == "warehouse_unavailable"


def test_batch_subset_and_horizons(artifact: ForecastArtifact) -> None:
    wanted = [PANEL.series[3], PANEL.series[0], PANEL.series[3]]
    result = service(artifact).forecast(series=wanted, horizons=[7, 1])
    assert {p.series for p in result.points} == {PANEL.series[0], PANEL.series[3]}
    assert sorted({p.horizon_days for p in result.points}) == [1, 7]
    assert len(result.points) == 4


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"series": [SeriesKey("mars", "p_api", "s_big")]}, "unknown_series"),
        ({"horizons": [8]}, "invalid_horizon"),
        ({"planned_prices": {"p_nope": 100}}, "unknown_product"),
        ({"planned_prices": {"p_api": 10_000}}, "planned_price_out_of_range"),
        ({"planned_prices": {"p_api": 100}}, "planned_price_out_of_range"),
    ],
)
def test_invalid_requests_are_rejected_and_counted(
    artifact: ForecastArtifact, kwargs: dict[str, object], code: str
) -> None:
    svc = service(artifact)
    with pytest.raises(ForecastRequestError) as err:
        svc.forecast(**kwargs)  # type: ignore[arg-type]
    assert err.value.code == code
    assert svc.metrics.snapshot()["counters"][f"forecast_reason_total:{code}"] == 1


def test_too_many_series_is_rejected(artifact: ForecastArtifact) -> None:
    serving = artifact.config.serving.model_copy(update={"max_series_per_request": 2})
    svc = service(artifact)
    svc._serving = serving  # narrow the bound without retraining
    with pytest.raises(ForecastRequestError, match="at most 2"):
        svc.forecast(series=list(PANEL.series[:3]))


def test_product_without_a_current_price_cannot_be_planned(artifact: ForecastArtifact) -> None:
    class NoPrices(PanelSource):
        def snapshot(self, series: Sequence[SeriesKey], *, not_after: date) -> Snapshot | None:
            snap = super().snapshot(series, not_after=not_after)
            assert snap is not None
            return Snapshot(snap.panel, type(snap.plan)({}))

    svc = service(artifact, source=NoPrices())
    with pytest.raises(ForecastRequestError, match="no current price"):
        svc.forecast(planned_prices={"p_api": 1000})


def test_planned_price_reaches_the_model_features(
    artifact: ForecastArtifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planned price must become the target-day price feature (current p_api = 900)."""
    seen: list[FeatureFrame] = []
    real_predict = artifact.model.predict

    def spy(frame: FeatureFrame) -> Prediction:
        seen.append(frame)
        return real_predict(frame)

    monkeypatch.setattr(artifact.model, "predict", spy)
    svc = service(artifact)
    svc.forecast(series=[PANEL.series[0]], horizons=[3])
    svc.forecast(series=[PANEL.series[0]], horizons=[3], planned_prices={"p_api": 1200})
    base, planned = (f.column("log_price_change_to_target")[0] for f in seen)
    assert PANEL.series[0].product == "p_api"
    assert base == 0.0
    assert planned == pytest.approx(np.log(1200 / 900))


def test_metrics_and_structured_log_carry_the_model_version(
    artifact: ForecastArtifact, caplog: pytest.LogCaptureFixture
) -> None:
    svc = service(artifact)
    with caplog.at_level(logging.INFO, logger="praxis.forecasting.service"):
        svc.forecast(horizons=[1])
    snap = svc.metrics.snapshot()
    assert snap["counters"]["forecast_requests_total"] == 1
    assert snap["counters"]["forecast_outcome_total:served"] == 1
    assert snap["counters"]["forecast_points_total"] == len(PANEL.series)
    assert snap["latency"]["count"] == 1 and snap["latency"]["p50_ms"] > 0
    assert snap["last"]["model_version"] == artifact.model_version
    record = next(r for r in caplog.records if r.getMessage() == "forecast.served")
    assert record.__dict__["model_version"] == artifact.model_version
    assert record.__dict__["freshness"] == "fresh"


def test_latency_budget_over_the_real_warehouse_path(
    artifact: ForecastArtifact, tmp_path: Path
) -> None:
    """All series x 7 horizons from a DuckDB file, measured end to end, p95 within budget."""
    db = tmp_path / "wh.duckdb"
    write_warehouse(db, PANEL, make_plan())
    svc = ForecastService(artifact, WarehouseFeatureSource(db), clock=lambda: clock(1))
    for _ in range(20):
        result = svc.forecast()
    assert result.source is Source.MODEL and len(result.points) == len(PANEL.series) * 7
    p95 = svc.metrics.snapshot()["latency"]["p95_ms"]
    assert p95 < artifact.config.serving.latency_budget_ms_p95, p95
