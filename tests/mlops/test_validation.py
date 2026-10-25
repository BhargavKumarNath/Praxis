"""Offline validation gate: a known-good candidate passes, every bad kind is rejected."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from praxis.forecasting.artifact import (
    MANIFEST,
    RIDGE_FILE,
    ForecastArtifact,
    _content_checksum,
    _sha,
)
from praxis.forecasting.config import load_forecast_config
from praxis.forecasting.panel import DemandPanel
from praxis.mlops.config import MlopsConfig
from praxis.mlops.training import Candidate
from praxis.mlops.validation import (
    Gate,
    backtest_checks,
    check_champion_skill,
    check_dataset,
    check_feature_contract,
    check_future_invariance,
    check_lineage,
    poisoned,
    training_data,
    validate_candidate,
)
from tests.mlops import helpers as h
from tests.mlops.conftest import World

ALL_CHECKS = {
    "artifact_integrity",
    "lineage_complete",
    "feature_contract",
    "dataset_reference",
    "leakage_training_window",
    "leakage_future_invariance",
    "backtest_acceptance",
    "baseline_comparison",
    "calibration_80",
    "calibration_50",
    "segment_regression_vs_baseline",
    "champion_non_inferiority",
    "latency_budget",
    "memory_budget",
    "reproducibility",
    "serving_parity",
}


def run(
    c: Candidate, world: World, cfg: MlopsConfig, champion: Candidate | None = None, **kw: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "warehouse": world.clean,
        "as_of": world.date(h.TRAIN_END_DAY + 29),
        "backtest": c.backtest,
        "champion": champion.artifact if champion else None,
    }
    acceptance = kw.pop("acceptance", h.forecast_config().acceptance)
    args["gate"] = Gate(kw.pop("policy", cfg), acceptance)
    args.update(kw)
    path = args.pop("path", c.path)
    record = args.pop("record", c.record)
    return validate_candidate(path, record, **args)


def test_known_good_candidate_passes_every_check(
    good: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    report = run(good, world, cfg, champion)
    assert report["passed"], report["failed"]
    assert {c["check"] for c in report["checks"]} == ALL_CHECKS
    by = {c["check"]: c for c in report["checks"]}
    assert by["reproducibility"]["value"] == good.record.model_version[-12:]
    assert by["serving_parity"]["value"] == 0.0
    assert by["leakage_future_invariance"]["value"] == 0.0
    assert by["latency_budget"]["value"] <= cfg.validation.latency_budget_ms_p95
    assert report["champion"] == champion.record.model_version


def test_known_bad_candidate_is_rejected(
    bad: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    report = run(bad, world, cfg, champion)
    assert not report["passed"]
    assert {"backtest_acceptance", "baseline_comparison"} <= set(report["failed"])


def _scaled(backtest: dict[str, Any], name: str, factor: float) -> dict[str, Any]:
    """The same backtest with ``name``'s overall vWAPE multiplied by ``factor``."""
    models = dict(backtest["models"])
    overall = dict(models[name]["overall"], vwape=models[name]["overall"]["vwape"] * factor)
    models[name] = dict(models[name], overall=overall)
    return dict(backtest, models=models)


def _recipe_variant(c: Candidate) -> ForecastArtifact:
    """``c``'s artifact with a different recipe hash but identical fitted behaviour (the
    bootstrap sample count only affects the backtest's confidence intervals)."""
    config = c.artifact.config
    backtest = config.backtest.model_copy(
        update={"bootstrap_samples": config.backtest.bootstrap_samples + 1}
    )
    return replace(c.artifact, config=config.model_copy(update={"backtest": backtest}))


def test_champion_non_inferiority_same_recipe_is_the_identical_backtest(
    good: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    acc = h.forecast_config().acceptance
    data = training_data(good.artifact, world.clean)
    same = check_champion_skill(good.backtest, good.artifact, champion.artifact, data, cfg, acc)
    assert same["passed"] and same["value"] == 1.0
    assert same["detail"]["method"] == "same_recipe"
    assert same["detail"]["champion"] == champion.record.model_version
    # The shortcut is exact: re-running an equivalent recipe on the same folds reproduces it.
    rerun = check_champion_skill(
        good.backtest, good.artifact, _recipe_variant(champion), data, cfg, acc
    )
    assert rerun["detail"]["method"] == "champion_recipe_rerun"
    assert rerun["value"] == pytest.approx(1.0, abs=1e-12)
    assert rerun["detail"]["origins"] == len(good.backtest["origins"])


def test_champion_non_inferiority_rejects_a_worse_recipe_on_the_same_origins(
    good: Candidate, bad: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    acc = h.forecast_config().acceptance
    limit = cfg.validation.max_skill_ratio_vs_champion
    data = training_data(bad.artifact, world.clean)
    out = check_champion_skill(bad.backtest, bad.artifact, champion.artifact, data, cfg, acc)
    assert out["detail"]["method"] == "champion_recipe_rerun"
    assert not out["passed"] and out["value"] > limit
    # the champion's recipe on these origins scores exactly what the good retrain scored
    good_skill = (
        good.backtest["models"][acc.candidate]["overall"]["vwape"]
        / (good.backtest["models"]["seasonal_naive"]["overall"]["vwape"])
    )
    assert out["detail"]["champion_recipe_skill"] == pytest.approx(good_skill, abs=1e-6)
    variant = _recipe_variant(champion)
    worse = _scaled(good.backtest, acc.candidate, limit * 1.01)
    edge = _scaled(good.backtest, acc.candidate, limit * 0.999)
    gdata = training_data(good.artifact, world.clean)
    assert not check_champion_skill(worse, good.artifact, variant, gdata, cfg, acc)["passed"]
    assert check_champion_skill(edge, good.artifact, variant, gdata, cfg, acc)["passed"]


def test_champion_non_inferiority_without_a_comparison(
    good: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    acc = h.forecast_config().acceptance
    data = training_data(good.artifact, world.clean)
    art = good.artifact
    assert check_champion_skill(good.backtest, art, None, data, cfg, acc)["passed"]  # bootstrap
    assert not check_champion_skill(None, art, champion.artifact, data, cfg, acc)["passed"]
    lenient = cfg.model_copy(
        update={
            "validation": cfg.validation.model_copy(update={"require_backtest_acceptance": False})
        }
    )
    assert check_champion_skill(None, art, champion.artifact, data, lenient, acc)["passed"]
    # a backtest that is not on this panel's folds cannot be compared: fail closed
    moved = dict(good.backtest, origins=good.backtest["origins"][1:])
    out = check_champion_skill(moved, art, _recipe_variant(champion), data, cfg, acc)
    assert not out["passed"] and out["detail"]["note"]


def _copy(c: Candidate, tmp: Path) -> Path:
    dst = tmp / c.path.name
    shutil.copytree(c.path, dst)
    return dst


def test_tampered_artifact_fails_integrity_only(
    good: Candidate, world: World, cfg: MlopsConfig, tmp_path: Path
) -> None:
    path = _copy(good, tmp_path)
    (path / RIDGE_FILE).write_text((path / RIDGE_FILE).read_text().replace("1", "2", 1))
    report = run(good, world, cfg, path=path)
    assert report["failed"] == ["artifact_integrity"]
    assert "checksum mismatch" in report["checks"][0]["detail"]["error"]


def test_non_reproducible_artifact_is_rejected(
    good: Candidate, world: World, cfg: MlopsConfig, tmp_path: Path
) -> None:
    """Consistently resealed (checksums valid) but not what the recipe produces."""
    path = _copy(good, tmp_path)
    state = json.loads((path / RIDGE_FILE).read_text())
    state["beta"] = [b * 1.01 for b in state["beta"]]
    text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    (path / RIDGE_FILE).write_text(text)
    manifest = json.loads((path / MANIFEST).read_text())
    manifest["files"][RIDGE_FILE] = _sha(text.encode())
    manifest["artifact_checksum"] = _content_checksum(manifest)
    (path / MANIFEST).write_text(json.dumps(manifest))
    record = replace(good.record, artifact_checksum=manifest["artifact_checksum"])
    report = run(good, world, cfg, path=path, record=record)
    assert "reproducibility" in report["failed"]
    assert "artifact_integrity" not in report["failed"]


def test_incompatible_feature_contract_is_rejected(
    good: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    other = replace(champion.artifact, series=champion.artifact.series[:-1])
    report = validate_candidate(
        good.path,
        good.record,
        warehouse=world.clean,
        as_of=world.date(140),
        gate=Gate(cfg, h.forecast_config().acceptance),
        backtest=good.backtest,
        champion=other,
    )
    assert report["failed"] == ["feature_contract"]


def test_dataset_and_lineage_mismatches_are_rejected(
    good: Candidate, world: World, cfg: MlopsConfig, tmp_path: Path
) -> None:
    other = h.write(tmp_path / "other.duckdb", h.panel(seed=99))
    report = run(good, world, cfg, warehouse=other)
    assert "dataset_reference" in report["failed"]
    lying = replace(good.record, data_version="panel-somewhere-else")
    assert "lineage_complete" in run(good, world, cfg, record=lying)["failed"]
    strict = cfg.model_copy(
        update={
            "validation": cfg.validation.model_copy(update={"require_clean_code_revision": True})
        }
    )
    dirty = replace(good.record, code_revision="abc-dirty")
    assert "lineage_complete" in run(good, world, strict, record=dirty)["failed"]


def test_training_window_leakage_is_rejected(
    good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    report = run(good, world, cfg, as_of=world.date(h.TRAIN_END_DAY + 28))
    assert report["failed"] == ["leakage_training_window"]


def test_backtest_report_must_match_and_be_present(
    good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    assert run(good, world, cfg, backtest=None)["failed"] == [
        "backtest_acceptance",
        "reproducibility",
    ]
    other = dict(good.backtest, data_version="panel-elsewhere")
    assert "backtest_acceptance" in run(good, world, cfg, backtest=other)["failed"]
    lenient = cfg.model_copy(
        update={
            "validation": cfg.validation.model_copy(update={"require_backtest_acceptance": False})
        }
    )
    assert "backtest_acceptance" not in run(good, world, lenient, backtest=None)["failed"]


def test_budgets_are_enforced(good: Candidate, world: World, cfg: MlopsConfig) -> None:
    tight = cfg.model_copy(
        update={
            "validation": cfg.validation.model_copy(
                update={"latency_budget_ms_p95": 1e-6, "memory_budget_mb": 1e-6}
            )
        }
    )
    assert {"latency_budget", "memory_budget"} <= set(run(good, world, tight)["failed"])


def test_future_invariance_catches_a_leaky_model() -> None:
    panel = h.panel()

    def honest(p: DemandPanel, origin: int) -> np.ndarray[Any, Any]:
        return p.demand[:, : origin + 1].mean(axis=1)

    def leaky(p: DemandPanel, origin: int) -> np.ndarray[Any, Any]:
        return p.demand[:, origin + 1]  # reads tomorrow

    assert check_future_invariance(honest, panel, 10.0, 7)["passed"]
    out = check_future_invariance(leaky, panel, 10.0, 7)
    assert not out["passed"] and out["value"] > 0
    dirty = poisoned(panel, 100, 10.0)
    assert np.array_equal(dirty.demand[:, :101], panel.demand[:, :101])
    assert np.allclose(dirty.demand[:, 101:], panel.demand[:, 101:] * 10.0)


def test_unreadable_artifact_is_rejected(
    good: Candidate, world: World, cfg: MlopsConfig, tmp_path: Path
) -> None:
    report = run(good, world, cfg, path=tmp_path / "missing")
    assert report["failed"] == ["artifact_integrity"]
    assert report["as_of"] == world.date(h.TRAIN_END_DAY + 29).isoformat()


def test_candidate_cannot_bring_its_own_lenient_acceptance(
    good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    """The report's verdict used the candidate's own [acceptance]; the gate re-evaluates the
    metrics against the authoritative, pinned acceptance (configs/forecast/demand.toml)."""
    assert good.backtest["acceptance"]["passed"]  # lenient fixture acceptance said yes
    pinned = load_forecast_config().acceptance
    report = run(good, world, cfg, acceptance=pinned)
    by = {c["check"]: c for c in report["checks"]}
    assert not by["backtest_acceptance"]["passed"]
    assert by["backtest_acceptance"]["detail"]["failed"]


def test_each_check_names_its_problem(
    good: Candidate, champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    art = good.artifact
    wrong = replace(good.record, artifact_checksum="0" * 64, model_version="demand-hybrid-x")
    lineage = check_lineage(wrong, art, cfg)
    assert {"artifact_checksum != manifest", "model_version != manifest"} <= set(
        lineage["detail"]["problems"]
    )
    old = replace(art, manifest={**art.manifest, "feature_version": "demand_features.v0"})
    assert "feature_version" in check_feature_contract(old, None)["detail"]["problems"][0]
    strict = cfg.model_copy(
        update={
            "validation": cfg.validation.model_copy(
                update={"min_training_days": 10_000, "min_training_rows": 10**9}
            )
        }
    )
    dataset = check_dataset(art, training_data(art, world.clean), strict)
    assert len(dataset["detail"]["problems"]) == 2 and not dataset["passed"]
    other_cfg = dict(good.backtest, config_hash="elsewhere")
    out = backtest_checks(art, other_cfg, cfg, h.forecast_config().acceptance)
    assert "backtest config_hash != artifact config_hash" in out[0]["detail"]["problems"]


def test_naive_training_time_and_short_reference_are_refused(
    good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    from datetime import datetime

    from praxis.mlops.lineage import missing_lineage
    from praxis.mlops.scoring import load_data
    from praxis.mlops.training import reference_profile

    naive = replace(good.record, trained_at=datetime(2026, 5, 1))  # noqa: DTZ001 - the defect
    assert "trained_at (timezone)" in missing_lineage(naive)
    tiny = load_data(world.clean, end=world.date(30))
    short = cfg.monitoring.model_copy(update={"reference_days": 7})
    early = replace(
        good.artifact,
        manifest={
            **good.artifact.manifest,
            "training": {
                **good.artifact.manifest["training"],
                "last_target_date": world.date(20).isoformat(),
            },
        },
    )
    with pytest.raises(ValueError, match="too short"):
        reference_profile(early, tiny, short)
