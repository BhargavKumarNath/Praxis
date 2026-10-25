"""Drift detection, retraining triggers, probation and shadow scoring on known worlds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from praxis.mlops.config import MlopsConfig
from praxis.mlops.monitoring import drift_check, finite_reference, persistent, probation_check
from praxis.mlops.scoring import (
    IncompatibleVersions,
    check_compatible,
    load_data,
    matured_origins,
    observations,
)
from praxis.mlops.shadow import evaluate_shadow
from praxis.mlops.training import Candidate
from tests.mlops import helpers as h
from tests.mlops.conftest import World

DECISION_SIGNALS = {"residual_drift", "segment_drift", "calibration_drift"}


def check_on(
    champion: Candidate,
    warehouse: Path,
    world: World,
    day: int,
    cfg: MlopsConfig,
    history: list[list[str]] | None = None,
) -> dict[str, Any]:
    data = load_data(warehouse, end=world.date(day - 1))
    return drift_check(
        champion.artifact,
        champion.record.reference or {},
        data,
        world.date(day),
        cfg,
        history=history or [],
    )


@pytest.mark.parametrize("day", [119, 126, 133, 140, 147, 154])
def test_no_decision_alarm_without_drift(
    champion: Candidate, world: World, cfg: MlopsConfig, day: int
) -> None:
    report = check_on(champion, world.clean, world, day, cfg)
    assert not DECISION_SIGNALS & set(report["drift_fired"])


def test_injected_level_shift_fires_residual_and_segment_drift(
    champion: Candidate, world: World, cfg: MlopsConfig
) -> None:
    """Shift from day 126 (r_east x1.8): detected within a week, on the right slice."""
    before = check_on(champion, world.drifted, world, h.SHIFT_DAY, cfg)
    assert not DECISION_SIGNALS & set(before["drift_fired"])
    after = check_on(champion, world.drifted, world, h.SHIFT_DAY + 7, cfg)
    fired = set(after["drift_fired"])
    assert {"residual_drift", "segment_drift"} <= fired
    residual = after["residual"]
    assert residual["worst_slice"] == "region=r_east"
    assert residual["vwape_ratio"] > cfg.monitoring.max_vwape_ratio_vs_reference
    # one day over the threshold is a breach, not yet an alarm (persistence_days > 1)
    assert cfg.monitoring.persistence_days > 1
    assert {"residual_drift", "segment_drift"} <= set(after["breaches"])
    assert after["alarms"] == [] and not DECISION_SIGNALS & set(after["retraining_triggers"])
    run = [["residual_drift", "segment_drift"]] * (cfg.monitoring.persistence_days - 1)
    persisted = check_on(champion, world.drifted, world, h.SHIFT_DAY + 7, cfg, history=run)
    assert persisted["alarms"] == ["residual_drift", "segment_drift"]
    assert {"residual_drift", "segment_drift"} <= set(persisted["retraining_triggers"])
    for missing in range(len(run)):  # a gap anywhere inside the window breaks the run
        broken = [[] if i == missing else day for i, day in enumerate(run)]
        gap = check_on(champion, world.drifted, world, h.SHIFT_DAY + 7, cfg, history=broken)
        assert gap["alarms"] == []


def test_persistence_rule() -> None:
    assert persistent(["residual_drift", "feature_drift"], [["residual_drift"]], 2) == [
        "residual_drift"
    ]  # investigation signals never become decision alarms
    assert persistent(["residual_drift"], [], 1) == ["residual_drift"]
    assert persistent(["residual_drift"], [], 2) == []  # not enough evaluated days yet
    assert persistent(["residual_drift"], [["segment_drift"]], 2) == []  # another signal
    assert persistent(["segment_drift"], [["segment_drift"], []], 3) == []  # run broken
    assert persistent(["segment_drift"], [["segment_drift"]] * 3, 3) == ["segment_drift"]


def test_injected_feature_shift_fires_feature_drift(
    champion: Candidate, world: World, cfg: MlopsConfig, tmp_path: Path
) -> None:
    warehouse = h.write(tmp_path / "throttled.duckdb", h.throttled(h.panel()))
    report = check_on(champion, warehouse, world, 134, cfg)
    features = report["distribution"]["feature_psi"]
    assert "feature.served_ratio_7" in features["fired"]
    assert "feature_drift" in report["drift_fired"]


def test_schedule_and_label_triggers(champion: Candidate, world: World, cfg: MlopsConfig) -> None:
    early = check_on(champion, world.clean, world, h.TRAIN_END_DAY + 2, cfg)
    assert early["residual"]["status"] == "insufficient_data"
    assert early["retraining_triggers"] == [] and early["data_age_days"] == 1
    labels = check_on(champion, world.clean, world, h.TRAIN_END_DAY + 22, cfg)
    assert "new_labels" in labels["retraining_triggers"]
    assert labels["new_labels"] == 21 * h.N_SERIES
    late = check_on(champion, world.clean, world, h.TRAIN_END_DAY + 29, cfg)
    assert {"scheduled", "new_labels"} <= set(late["retraining_triggers"])


def test_reference_profile_is_complete(champion: Candidate) -> None:
    ref = champion.record.reference
    assert ref is not None and finite_reference(ref)
    assert set(ref["bins"]) >= {"prediction.scaled_point", "feature.lag_dow_1"}
    assert not finite_reference(None) and not finite_reference({"bins": {}})
    broken = {**ref, "metrics": {"overall": {"vwape": 0.0}}}
    assert not finite_reference(broken)


def test_probation_flags_a_worse_champion(
    champion: Candidate, bad: Candidate, good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    """Promoted on day 141: 7 fully matured origins (140..146) exist from day 154."""
    data = load_data(world.clean, end=world.date(160))
    promoted, as_of = world.date(141), world.date(155)
    worse = probation_check(bad.artifact, champion.artifact, data, promoted, as_of, cfg)
    assert worse["status"] == "evaluated" and worse["fired"], worse
    assert worse["origins"] >= cfg.probation.min_origins
    assert worse["vwape_ratio"] > cfg.probation.max_vwape_ratio_vs_previous
    fine = probation_check(good.artifact, champion.artifact, data, promoted, as_of, cfg)
    assert fine["status"] == "evaluated" and not fine["fired"], fine
    early = probation_check(bad.artifact, champion.artifact, data, promoted, world.date(150), cfg)
    assert early["status"] == "insufficient_data" and not early["fired"]
    expired = probation_check(bad.artifact, champion.artifact, data, world.date(120), as_of, cfg)
    assert not expired["active"] and not expired["fired"]


def test_shadow_scores_both_versions_on_the_same_observations(
    champion: Candidate, good: Candidate, bad: Candidate, world: World, cfg: MlopsConfig
) -> None:
    data = load_data(world.clean, end=world.date(166))
    start = world.date(h.TRAIN_END_DAY + 29)
    pending = evaluate_shadow(champion.artifact, good.artifact, data, start, world.date(146), cfg)
    assert pending["status"] == "pending" and not pending["passed"]
    ok = evaluate_shadow(champion.artifact, good.artifact, data, start, world.date(167), cfg)
    assert ok["status"] == "evaluated" and ok["same_observations"], ok
    assert ok["passed"], [c for c in ok["checks"] if not c["passed"]]
    assert ok["origins"] >= cfg.shadow.min_origins
    worse = evaluate_shadow(champion.artifact, bad.artifact, data, start, world.date(167), cfg)
    assert worse["status"] == "evaluated" and not worse["passed"]
    assert worse["rows"] == ok["rows"]  # identical eligible observations


def test_incompatible_versions_cannot_be_compared(
    champion: Candidate, good: Candidate, world: World, cfg: MlopsConfig
) -> None:
    from dataclasses import replace

    art = good.artifact
    for broken in (
        replace(art, series=art.series[:-1]),
        replace(
            art,
            config=art.config.model_copy(
                update={"target": art.config.target.model_copy(update={"horizons": (1, 2)})}
            ),
        ),
        replace(
            art,
            config=art.config.model_copy(
                update={
                    "target": art.config.target.model_copy(
                        update={"quantiles": (0.1, 0.25, 0.5, 0.75, 0.9)}
                    )
                }
            ),
        ),
        replace(art, manifest={**art.manifest, "feature_version": "demand_features.v0"}),
        replace(art, config=art.config.with_external(True)),
    ):
        with pytest.raises(IncompatibleVersions):
            check_compatible([champion.artifact, broken])
    data = load_data(world.clean, end=world.date(150))
    report = probation_check(
        replace(art, series=art.series[:-1]),
        champion.artifact,
        data,
        world.date(141),
        world.date(151),
        cfg,
    )
    assert report["status"].startswith("incomparable")


def test_observation_helpers(champion: Candidate, world: World) -> None:
    data = load_data(world.clean, end=world.date(150))
    assert observations(champion.artifact, data, [0, 1]) is None  # no 28-day history
    assert observations(champion.artifact, data, [140], target_window=(0, 10)) is None
    full = matured_origins(
        data.panel, first=world.date(140), as_of=world.date(151), max_horizon=7, full=True
    )
    assert full == [140, 141, 142, 143]
