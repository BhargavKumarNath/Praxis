"""Pre-registered pricing configuration: pinned files, validation, the shadow world's lineage."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from praxis.pricing.config import Mode, PricingPolicy, load_policy
from praxis.science.pricing_shadow import load_shadow_config
from praxis.simulator.config import load_config
from praxis.simulator.population import generate_population

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "configs/simulator/scenarios"

# Pinned 2026-10-06, before the optimiser ran on any world. A change is a new policy /
# acceptance and needs an ADR (CLAUDE.md s16): never edit these to flatter a result.
PINNED = {
    # re-pinned 2026-10-10 (Phase 9, ADR 0016): [long_term] guardrails added, the CLV proxy cap
    # removed (replaced by the CLV model); then evidence.min_bill_share added (ADR 0016
    # amendment, after seed 44), then churn_tau_scale / churn_models (second amendment, after
    # seed 45); then the meta_analytic churn model + its archive (third amendment, after seed
    # 202); no existing threshold changed (see the test below)
    "configs/pricing/policy.toml": (
        "0931da35a1874478ad28e75251931cd0872da2fd8ef6103b68c065e318c644f1"
    ),
    # ADR 0016 third amendment: development experiment worlds only (see the test below)
    "configs/pricing/churn_archive.json": (
        "7e5ee63fe290d6e45dc13e6fb93baccb2a251a5f02217ba115b7eaec6d1a27a2"
    ),
    # re-pinned: ADR 0013 amendment (before seed 42, owner-approved); then a comment-only edit
    # registering seed 43 as the held-out re-evaluation (second amendment; no threshold change);
    # then comment-only edits registering seeds 44, 45, 201-203 and 301-303 (ADR 0016 and
    # amendments)
    "configs/pricing/shadow_acceptance.toml": (
        "69315859563edc15416e1e8732dc542357e7b539f71e7f059914291bcdaa4c8c"
    ),
    "configs/simulator/scenarios/pricing_shadow.toml": (
        "caca13ea004eeb144c037ae065bdb193411fac6c2eb13f398093fc47d54bb003"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_unchanged(path: str) -> None:
    assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == PINNED[path]


# The Phase 6 thresholds as pre-registered 2026-10-06. Phase 9 may only ADD constraints:
# CLAUDE.md s16 / project_progress "do NOT loosen policy.toml to make prices move".
PHASE6_LIMITS = {
    "constraints": {
        "max_step": 0.05,
        "cooldown_days": 14,
        "min_contribution_margin": 0.20,
        "max_utilization": 0.85,
        "capacity_quantile": 0.9,
        "max_incremental_churn": 0.005,
    },
    "uncertainty": {
        "max_elasticity_sd": 0.25,
        "material_tier_share": 0.05,
        "min_prob_improvement": 0.90,
        "risk_aversion": 0.5,
        "max_forecast_relative_width": 1.0,
        "z_grid": 199,
    },
    "evidence": {
        "max_extrapolation": 0.1823,
        "max_evidence_age_days": 365,
        "churn_upper_z": 1.6449,
        "require_fresh_forecast": True,
    },
}


# Every seed ever used to EVALUATE pricing; the meta-analytic archive may hold none of them.
EVALUATION_SEEDS = {42, 43, 44, 45, 201, 202, 203, 301, 302, 303, 304, 305, 306}


def test_churn_archive_holds_development_experiments_only() -> None:
    archive = json.loads((REPO / "configs/pricing/churn_archive.json").read_text())
    labels = [re.fullmatch(r"eval-seed(\d+)-c8000", e["source"]) for e in archive["experiments"]]
    assert all(labels)
    seeds = {int(m[1]) for m in labels if m}
    assert seeds == {1, *range(101, 125)}
    assert not seeds & EVALUATION_SEEDS
    assert archive["is_synthetic"] is True


def test_phase9_did_not_loosen_any_phase6_threshold() -> None:
    raw = load_policy().model_dump()
    for section, limits in PHASE6_LIMITS.items():
        for name, value in limits.items():
            assert raw[section][name] == value, f"{section}.{name}"
    lt = load_policy().long_term
    assert max(lt.max_segment_incremental_churn.values()) < 0.01
    assert lt.max_segment_incremental_churn["enterprise"] < 0.005  # tighter where value is high


def test_policy_starts_in_shadow_mode_with_execution_disabled() -> None:
    policy = load_policy()
    assert policy.policy.mode is Mode.SHADOW
    assert policy.policy.allow_execute is False
    assert policy.version.startswith("policy-") and len(policy.version) == 19
    assert set(policy.products) == {p.id for p in load_config().products}


def test_policy_version_tracks_content_and_mode() -> None:
    policy = load_policy()
    assert policy.with_mode(Mode.RECOMMEND).version != policy.version
    assert policy.with_mode(Mode.SHADOW).version == policy.version


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("policy", "mode", "execute"),  # execute needs allow_execute
        ("policy", "grid_points", 40),  # even grid
        ("constraints", "max_step", 0.0),
        ("uncertainty", "min_prob_improvement", 0.3),
        ("long_term", "max_segment_incremental_churn", {"growth": 1.5}),
        ("long_term", "max_price_changes_per_customer", 0),
    ],
)
def test_invalid_policies_are_rejected(section: str, field: str, value: object) -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    raw[section][field] = value
    with pytest.raises(ValidationError):
        PricingPolicy.model_validate(raw)


def test_inverted_product_bounds_are_rejected() -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    raw["products"]["api_requests"] = {"floor_micros": 10, "ceiling_micros": 5}
    with pytest.raises(ValidationError):
        PricingPolicy.model_validate(raw)


def test_shadow_acceptance_is_loadable_and_matches_the_world() -> None:
    cfg = load_shadow_config()
    world = load_config(scenario=SCENARIOS / "pricing_shadow.toml")
    last = cfg.shadow.first_cycle_day + cfg.shadow.cycles * cfg.shadow.cycle_days
    assert last == world.run.days  # every cycle is fully observed
    assert world.population.arrival_horizon_days == cfg.shadow.first_cycle_day


def test_shadow_world_is_the_forecast_world_continued() -> None:
    """Same overlay content apart from the run length and the arrival horizon."""
    forecast = tomllib.loads((SCENARIOS / "forecast_eval.toml").read_text())
    shadow = tomllib.loads((SCENARIOS / "pricing_shadow.toml").read_text())
    assert shadow.pop("population") == {"arrival_horizon_days": 196}
    assert shadow.pop("run") == {"days": 252}
    forecast.pop("run")
    assert shadow == forecast


def test_shadow_world_shares_the_forecast_worlds_population() -> None:
    a = load_config(scenario=SCENARIOS / "forecast_eval.toml").with_overrides(n_customers=300)
    b = load_config(scenario=SCENARIOS / "pricing_shadow.toml").with_overrides(n_customers=300)
    pa, pb = generate_population(a, 42), generate_population(b, 42)
    for name in ("created_day", "elasticity", "mix", "base_load", "churn_sens", "tier"):
        assert np.array_equal(getattr(pa, name), getattr(pb, name)), name


def test_arrival_horizon_is_hash_neutral_when_unset() -> None:
    world = load_config()
    assert "arrival_horizon_days" not in world.canonical_json()
    shifted = world.model_copy(
        update={"population": world.population.model_copy(update={"arrival_horizon_days": 30})}
    )
    assert shifted.config_hash != world.config_hash
