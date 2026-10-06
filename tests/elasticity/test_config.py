"""Pre-registration pins, experiment registry <-> simulated world agreement, config validation."""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from praxis.elasticity.config import (
    ExperimentDesign,
    ExperimentRegistry,
    load_elasticity_config,
    load_registry,
)
from praxis.science.elasticity_recovery import load_acceptance
from praxis.simulator.config import load_config
from tests.elasticity.helpers import design

REPO = Path(__file__).resolve().parents[2]
EVAL_WORLD = REPO / "configs/simulator/scenarios/elasticity_eval.toml"
CONTAMINATION_WORLD = REPO / "configs/simulator/scenarios/elasticity_contamination.toml"

# Pre-registered 2026-10-06 before any experiment world was simulated (ADR 0012); elasticity.toml
# amended once (hierarchical sampler / main-effect priors) before the held-out run. Changing a
# pinned file needs an ADR and the owner's approval; never edit to flatter a result.
PINNED = {
    "configs/elasticity/elasticity.toml": (
        "cf872491d012f438537ad6248a3294fa28c5d4c16ef32577b84a1974b0bf35f9"
    ),
    "configs/elasticity/acceptance.toml": (
        "837c757849cf477d1f427c9b9ff9c89f6d298f3f394fd2c0e6a32a8ed2c2912c"
    ),
    "configs/simulator/scenarios/elasticity_eval.toml": (
        "b45049d0372a2c53b052f5431bbaaa3854ad183d4d017281e1b5af70db38b672"
    ),
    "configs/simulator/scenarios/elasticity_contamination.toml": (
        "43d91465fba1ef40d91d8f9f54ba0ebdf80437e2c71ce00aca87aa526e8493ee"
    ),
    "configs/experiments/elasticity_eval.toml": (
        "1981576b0c170dbb6236566a3d038fd9e2959e4beb841b598079bc21ed3e4ee0"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_unchanged(path: str) -> None:
    assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == PINNED[path]


def test_preregistered_thresholds_have_their_documented_values() -> None:
    cfg = load_elasticity_config()
    acc = load_acceptance()
    assert cfg.eligibility.min_pre_units == 56 and cfg.outcome.min_active_days == 14
    assert cfg.validity.srm_alpha == 0.001 and cfg.validity.max_contamination_rate == 0.0
    assert cfg.diagnostics.max_divergences == 0 and cfg.diagnostics.rhat_max == 1.01
    assert acc.recovery.pooled_max_rel_error == 0.10 and acc.recovery.tier_max_rel_error == 0.25
    assert acc.recovery.cell_min_coverage == 0.70 and acc.contamination.iv_max_rel_error == 0.15


def test_registry_matches_the_simulated_world() -> None:
    """The business-side record and the world that executes it must describe the same tests."""
    world = load_config(scenario=EVAL_WORLD)
    registry = load_registry()
    start = world.run.start_date
    by_id = {iv.id: iv for iv in world.pricing.interventions}
    assert set(by_id) == {d.id for d in registry.experiments}
    for d in registry.experiments:
        iv = by_id[d.id]
        assert (iv.product, iv.salt, iv.treated_fraction) == (d.product, d.salt, d.treated_fraction)
        assert math.isclose(iv.price_multiplier, d.treatment_price_ratio, rel_tol=1e-12)
        assert start + timedelta(days=iv.start_day) == d.assignment_date
        assert start + timedelta(days=iv.end_day - 1) == d.end_date
        assert iv.contamination_fraction == 0.0
        # the pre-period must fit inside the world and be untreated
        assert d.pre_start(load_elasticity_config().eligibility.pre_window_days) >= start


def test_evaluation_worlds_are_valid_and_pinned() -> None:
    world = load_config(scenario=EVAL_WORLD)
    assert world.run.days == 56 and len(world.pricing.interventions) == 5
    for iv in world.pricing.interventions:
        assert abs(math.log(iv.price_multiplier)) == pytest.approx(math.log(1.2), rel=1e-12)
    assert world.config_hash.startswith("840d645b9e7a")
    dirty = load_config(scenario=CONTAMINATION_WORLD)
    fractions = {iv.id: iv.contamination_fraction for iv in dirty.pricing.interventions}
    assert sorted(v for v in fractions.values() if v) == [0.2, 0.2]
    assert dirty.config_hash.startswith("c8dea5be9641")


def test_registry_rejects_overlapping_tests_on_one_product() -> None:
    a = design("p_up")
    b = a.model_copy(update={"id": "other", "salt": "other-salt"})
    with pytest.raises(ValidationError, match="overlapping"):
        ExperimentRegistry(experiments=(a, b))
    later = b.model_copy(
        update={"assignment_date": date(2026, 4, 1), "end_date": date(2026, 4, 28)}
    )
    assert len(ExperimentRegistry(experiments=(a, later)).experiments) == 2


def test_registry_rejects_duplicate_salts_and_ids() -> None:
    a = design("p_up")
    with pytest.raises(ValidationError, match="unique"):
        ExperimentRegistry(experiments=(a, design("p_down").model_copy(update={"salt": a.salt})))


@pytest.mark.parametrize(
    "update",
    [
        {"treatment_price_ratio": 1.0},
        {"end_date": date(2026, 1, 1)},
        {"treated_fraction": 1.0},
        {"assignment_unit": "invoice"},
    ],
)
def test_invalid_designs_are_rejected(update: dict[str, object]) -> None:
    raw = design("p_up").model_dump() | update
    with pytest.raises(ValidationError):
        ExperimentDesign.model_validate(raw)


def test_design_derived_quantities() -> None:
    d = design("p_up")
    assert d.window_days == 28
    assert d.pre_start(28) == date(2026, 1, 5)
    assert d.log_ratio == pytest.approx(math.log(1.2))


def test_ppc_range_is_validated() -> None:
    cfg = load_elasticity_config()
    raw = cfg.diagnostics.model_dump() | {"ppc_p_range": (0.9, 0.1)}
    with pytest.raises(ValidationError, match="ppc_p_range"):
        type(cfg.diagnostics).model_validate(raw)


def test_config_hash_tracks_content() -> None:
    cfg = load_elasticity_config()
    assert cfg.config_hash == load_elasticity_config().config_hash
    assert cfg.with_sampler(draws=100, tune=100).config_hash != cfg.config_hash
