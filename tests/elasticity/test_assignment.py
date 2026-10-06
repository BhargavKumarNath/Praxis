"""Deterministic experiment assignment (domain) and its use by the simulator.

required_test.md s11: deterministic assignment with fixed salt, mutually exclusive assignment,
exposure logging (the simulator logs exactly the arm the domain function assigns).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats

from praxis.domain.experiments import ARMS, assign_arm, assignment_uniform
from praxis.simulator.config import Intervention, load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import generate_population

REPO = Path(__file__).resolve().parents[2]
IDS = [f"cust_{i:08d}" for i in range(20_000)]


def test_assignment_is_deterministic_for_a_fixed_salt() -> None:
    first = [assign_arm("salt-a", cid, 0.5) for cid in IDS[:500]]
    assert first == [assign_arm("salt-a", cid, 0.5) for cid in IDS[:500]]
    assert assignment_uniform("salt-a", "cust_00000001") == assignment_uniform(
        "salt-a", "cust_00000001"
    )


def test_assignment_value_is_pinned() -> None:
    """Changing the hash would silently re-randomise every logged experiment."""
    assert assignment_uniform("px-2026-02-api-requests-v1", "cust_00000000") == 0.14731036116370955


@settings(max_examples=200, deadline=None)
@given(
    salt=st.text(min_size=1, max_size=20),
    unit=st.text(max_size=20),
    fraction=st.floats(min_value=0.01, max_value=0.99),
)
def test_every_unit_gets_exactly_one_arm(salt: str, unit: str, fraction: float) -> None:
    u = assignment_uniform(salt, unit)
    assert 0.0 <= u < 1.0
    arm = assign_arm(salt, unit, fraction)
    assert arm in ARMS
    assert (arm == "treatment") == (u < fraction)


def test_realised_split_matches_the_design_fraction() -> None:
    for fraction in (0.1, 0.5, 0.8):
        treated = sum(assign_arm("split", cid, fraction) == "treatment" for cid in IDS)
        p = stats.binomtest(treated, len(IDS), fraction).pvalue
        assert p > 1e-4, (fraction, treated)


def test_different_salts_assign_independently() -> None:
    a = np.array([assign_arm("salt-a", cid, 0.5) == "treatment" for cid in IDS])
    b = np.array([assign_arm("salt-b", cid, 0.5) == "treatment" for cid in IDS])
    table = [[int((a & b).sum()), int((a & ~b).sum())], [int((~a & b).sum()), int((~a & ~b).sum())]]
    assert stats.chi2_contingency(table).pvalue > 1e-4


@pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
def test_invalid_fraction_is_rejected(fraction: float) -> None:
    with pytest.raises(ValueError, match="treated_fraction"):
        assign_arm("s", "u", fraction)


def test_empty_salt_is_rejected() -> None:
    with pytest.raises(ValueError, match="salt"):
        assignment_uniform("", "u")


def _world(contamination: float = 0.0) -> tuple[Engine, Intervention]:
    iv = Intervention(
        id="px-test",
        product="api_requests",
        start_day=3,
        end_day=6,
        treated_fraction=0.5,
        price_multiplier=1.2,
        salt="px-test-v1",
        contamination_fraction=contamination,
    )
    base = load_config().with_overrides(n_customers=300, days=6)
    cfg = base.model_copy(
        update={"pricing": base.pricing.model_copy(update={"interventions": (iv,)})}
    )
    return Engine(cfg, 3, generate_population(cfg, 3)), iv


def _start_exposures(engine: Engine, iv: Intervention) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ev in engine.run():
        p = ev["payload"]
        if ev["event_type"] == "price.exposed" and p["experiment_id"] == iv.id:
            out.setdefault(ev["entity_id"], p)
    return out


def test_simulator_logs_the_arm_the_domain_function_assigns() -> None:
    engine, iv = _world()
    logged = _start_exposures(engine, iv)
    assert len(logged) > 100
    for cid, payload in logged.items():
        assert payload["arm"] == assign_arm(iv.salt, cid, iv.treated_fraction)
        expected = round(
            payload["list_price_micros"] * (1.2 if payload["arm"] == "treatment" else 1)
        )
        assert abs(payload["unit_price_micros"] - expected) <= 1


def test_contamination_charges_control_units_the_treatment_price_and_logs_it() -> None:
    engine, iv = _world(contamination=0.3)
    logged = _start_exposures(engine, iv)
    control = [p for p in logged.values() if p["arm"] == "control"]
    charged = [p for p in control if p["unit_price_micros"] != p["list_price_micros"]]
    assert 0.15 < len(charged) / len(control) < 0.45
    treated = [p for p in logged.values() if p["arm"] == "treatment"]
    assert all(p["unit_price_micros"] != p["list_price_micros"] for p in treated)


def test_contamination_field_is_hash_neutral_at_zero() -> None:
    iv = _world()[1]
    assert "contamination_fraction" not in json.loads(iv.model_dump_json())
    assert "contamination_fraction" in json.loads(
        iv.model_copy(update={"contamination_fraction": 0.1}).model_dump_json()
    )
