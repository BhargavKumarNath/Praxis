"""Churn hazard models, the training panel, the champion rule and the artifact."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from praxis.customer.artifact import (
    ArtifactError,
    find_artifact,
    load_artifact,
    save_artifact,
)
from praxis.customer.config import load_config
from praxis.customer.dataset import Panel, snapshot_dates
from praxis.customer.features import FEATURES
from praxis.customer.hazard import (
    FitError,
    PoissonHazard,
    RateTable,
    deviance,
    fit_poisson,
    fit_rate_table,
)
from praxis.customer.train import backtest, calibration, train

CFG = load_config()
START = date(2026, 1, 5)
NOW = datetime(2026, 7, 20, tzinfo=UTC)


def synthetic_panel(*, effect: float, seed: int = 11, n: int = 4000) -> Panel:
    """Five 28-day windows; voluntary churn driven by tier and (if ``effect``) failures."""
    rng = np.random.default_rng(seed)
    dates = tuple(START + timedelta(days=56 + 28 * k) for k in range(5))
    rows = n * len(dates)
    tiers = rng.choice(["starter", "growth", "enterprise"], size=rows, p=[0.5, 0.35, 0.15])
    x = rng.normal(size=(rows, len(FEATURES)))
    x[:, 0] = tiers == "growth"
    x[:, 1] = tiers == "enterprise"
    base = np.where(tiers == "starter", 0.0018, np.where(tiers == "growth", 0.0012, 0.0005))
    hazard = base * np.exp(effect * x[:, 5])  # log_failed_attempts column
    exposure = np.full(rows, 28.0)
    events = (rng.random(rows) < 1 - np.exp(-28 * hazard)).astype(np.float64)
    return Panel(
        dates,
        np.repeat(np.arange(len(dates)), n),
        tuple(f"c{i % n}" for i in range(rows)),
        tuple(tiers.tolist()),
        x,
        exposure,
        events,
        np.zeros(rows),
    )


def test_snapshot_dates_end_at_the_cutoff_and_never_overlap() -> None:
    dates = snapshot_dates(START, date(2026, 7, 20), CFG.label)
    assert dates[-1] + timedelta(days=28) == date(2026, 7, 20)
    assert all((b - a).days == 28 for a, b in pairwise(dates))
    assert dates[0] >= START + timedelta(days=CFG.label.first_snapshot_day)
    assert snapshot_dates(START, START + timedelta(days=60), CFG.label) == []


def test_rate_table_pools_toward_the_overall_rate_by_hand() -> None:
    table = fit_rate_table(
        ["a", "a", "b"], np.array([1.0, 0.0, 3.0]), np.array([10.0, 10.0, 20.0]), prior_weight=40
    )
    overall = 4 / 40
    assert table.overall == pytest.approx(overall)
    assert table.rates["a"] == pytest.approx((1 + 40 * overall) / (20 + 40))
    assert table.rates["b"] == pytest.approx((3 + 40 * overall) / (20 + 40))
    assert table.predict(np.zeros((2, 1)), ["b", "unseen"]).tolist() == pytest.approx(
        [table.rates["b"], overall]
    )
    assert RateTable.from_state(json.loads(json.dumps(table.state()))) == table
    with pytest.raises(FitError):
        fit_rate_table(["a"], np.zeros(1), np.zeros(1), prior_weight=1)


def test_poisson_hazard_recovers_a_real_effect_and_round_trips() -> None:
    panel = synthetic_panel(effect=0.5)
    model = fit_poisson(
        panel.x, panel.voluntary, panel.exposure, FEATURES, ridge=0.0, max_iter=100, tol=1e-10
    )
    se = np.sqrt(np.diag(model.cov))
    slope = model.coef[1 + 5] / model.scale[5]  # back to the raw scale
    assert abs(slope - 0.5) < 4 * se[1 + 5] / model.scale[5]
    again = PoissonHazard.from_state(json.loads(json.dumps(model.state())))
    assert np.array_equal(again.predict(panel.x[:5]), model.predict(panel.x[:5]))
    # missing service data is imputed to the training mean (finite predictions)
    gap = panel.x[:3].copy()
    gap[:, 6] = np.nan
    assert np.all(np.isfinite(model.predict(gap)))
    with pytest.raises(ValueError):
        PoissonHazard.from_state({**model.state(), "name": "other"})
    with pytest.raises(FitError):
        fit_poisson(panel.x[:0], panel.voluntary[:0], panel.exposure[:0], FEATURES,
                    ridge=1, max_iter=5, tol=1e-9)  # fmt: skip


def test_deviance_prefers_the_true_rates() -> None:
    rng = np.random.default_rng(1)
    mu = rng.uniform(0.01, 0.2, size=5000)
    y = rng.poisson(mu).astype(np.float64)
    assert deviance(y, mu) < deviance(y, np.full_like(mu, mu.mean()))
    assert deviance(np.array([2.0]), np.array([2.0])) == pytest.approx(0.0)


def test_champion_rule_follows_the_backtest_deviance() -> None:
    strong = backtest(synthetic_panel(effect=0.8), CFG)
    assert strong.glm_deviance < strong.table_deviance and strong.champion == "poisson_glm"
    assert len(strong.windows) == CFG.hazard.backtest_windows
    for w in strong.windows:
        assert w["train_windows"] >= 1  # every scored window was fitted on earlier windows only
        assert date.fromisoformat(w["snapshot"]) >= START + timedelta(days=56 + 28)
    none = backtest(synthetic_panel(effect=0.0), CFG)
    expected = "poisson_glm" if none.glm_deviance < none.table_deviance else "tier_rate_table"
    assert none.champion == expected


def test_backtest_needs_history() -> None:
    panel = synthetic_panel(effect=0.0)
    one = panel.subset(panel.window == 0)
    with pytest.raises(ValueError, match="too few windows"):
        backtest(replace(one, dates=one.dates[:1]), CFG)


def test_calibration_reports_observed_over_expected_per_segment() -> None:
    panel = synthetic_panel(effect=0.0, n=200)
    mu = np.full(panel.n, float(panel.voluntary.sum()) / panel.n)
    cal = calibration(panel, mu, ("starter", "growth", "enterprise"))
    assert cal["ratio"] == pytest.approx(1.0)
    assert set(cal["segments"]) == {"starter", "growth", "enterprise"}
    assert calibration(panel, np.zeros(panel.n), ("starter",))["ratio"] is None


def test_train_writes_a_complete_lineage_artifact(tmp_path: Path) -> None:
    panel = synthetic_panel(effect=0.8)
    manifest, files, report = train(panel, CFG, cutoff=date(2026, 7, 20), now=NOW)
    for key in (
        "model_version",
        "data_version",
        "feature_version",
        "config_hash",
        "code_revision",
        "parameters",
        "metrics",
        "artifact_checksum",
        "created_at",
        "champion",
    ):
        assert manifest[key], key
    assert manifest["model_version"].startswith("churn-glm-")
    assert report["champion"] == "poisson_glm" and len(report["coefficients"]) == 9
    path = save_artifact(manifest, files, tmp_path)
    art = load_artifact(path)
    assert art.model_version == manifest["model_version"]
    assert art.champion.name == "poisson_glm"
    assert save_artifact(manifest, files, tmp_path) == path  # identical version kept
    assert find_artifact(tmp_path, panel.data_version()) == path
    assert find_artifact(path, panel.data_version()) == path  # an artifact dir works too
    assert find_artifact(tmp_path, "customers-other") is None
    # identical inputs, identical version (content-derived)
    again, _, _ = train(panel, CFG, cutoff=date(2026, 7, 20), now=NOW + timedelta(days=1))
    assert again["model_version"] == manifest["model_version"]


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda p: (p / "glm.json").write_text("{}"), "checksum mismatch"),
        (lambda p: (p / "rate_table.json").unlink(), "missing model file"),
        (lambda p: _edit(p, feature_version="customer_features.v0"), "feature version"),
        (lambda p: _edit(p, artifact_format=99), "format"),
        (lambda p: _edit(p, model_name="other"), "not a customer_churn"),
        (lambda p: _edit(p, metrics={"x": 1}), "manifest checksum"),
        (lambda p: (p / "manifest.json").write_text("{"), "not valid JSON"),
        (lambda p: (p / "manifest.json").unlink(), "no manifest"),
    ],
)
def test_damaged_artifacts_are_refused(tmp_path: Path, damage: object, message: str) -> None:
    manifest, files, _ = train(synthetic_panel(effect=0.8), CFG, cutoff=date(2026, 7, 20), now=NOW)
    path = save_artifact(manifest, files, tmp_path)
    damage(path)  # type: ignore[operator]
    with pytest.raises(ArtifactError, match=message):
        load_artifact(path)


def _edit(path: Path, **fields: object) -> None:
    m = json.loads((path / "manifest.json").read_text())
    m.update(fields)
    (path / "manifest.json").write_text(json.dumps(m))
