"""Experiment-validity gates: each passes on a clean design and catches its defect.

required_test.md s11: sample-ratio mismatch detector, exposure logging, missing outcomes,
treatment contamination simulation, pre-treatment balance, assignment correctness.
"""

from __future__ import annotations

import numpy as np
import pytest

from praxis.elasticity.config import ExperimentDesign, load_elasticity_config
from praxis.elasticity.dataset import UnitTable, build_units
from praxis.elasticity.validity import (
    Check,
    balance_check,
    experiment_checks,
    guardrails,
    interference_check,
    ols_cluster,
    srm_check,
)
from tests.elasticity.helpers import Defects, make_extract

CFG = load_elasticity_config()
V = CFG.validity


def _checks(defects: Defects | None = None, n: int = 2000, seed: int = 0) -> dict[str, Check]:
    units, designs, census = build_units(make_extract(n, seed=seed, defects=defects), CFG)
    return {c.name: c for c in experiment_checks(units, 0, designs, census[0], V)}


@pytest.fixture(scope="module")
def clean() -> dict[str, Check]:
    return _checks()


def test_clean_design_passes_every_gate(clean: dict[str, Check]) -> None:
    gates = {k: c.passed for k, c in clean.items() if c.gate}
    assert set(gates) == {
        "srm",
        "assignment",
        "exposure_logging",
        "contamination",
        "balance",
        "missing_outcomes",
    }
    assert all(gates.values()), gates
    assert {c.name for c in clean.values() if not c.gate} == {"late_arrival_srm", "interference"}


def test_srm_detects_lost_treated_units() -> None:
    # 15% of treated units lost (e.g. a logging outage in one arm) is detectable at n = 4000
    c = _checks(Defects(drop_treated_share=0.15), n=4000)["srm"]
    assert not c.passed and c.detail["p"] < V.srm_alpha


def test_srm_statistic() -> None:
    assert srm_check(500, 500, 0.5, 0.001, "x").detail["p"] == pytest.approx(1.0)
    assert not srm_check(430, 570, 0.5, 0.001, "x").passed
    assert srm_check(800, 200, 0.2, 0.001, "x").passed
    assert not srm_check(0, 0, 0.5, 0.001, "x").passed


def _eligible_ids(k: int) -> frozenset[str]:
    """``k`` customers that are eligible units of test 0 in the clean synthetic world."""
    units = build_units(make_extract(2000, seed=0), CFG)[0]
    return frozenset(units.customer[units.experiment == 0][:k].tolist())


def test_assignment_audit_catches_wrong_logged_arms() -> None:
    c = _checks(Defects(wrong_arm=_eligible_ids(40)))["assignment"]
    assert not c.passed and c.detail["arm_mismatches"] == 40


def test_exposure_logging_gap_is_detected() -> None:
    c = _checks(Defects(drop_exposure=_eligible_ids(25)))["exposure_logging"]
    assert not c.passed and c.detail["missing"] == 25


def test_contamination_is_detected_and_measured() -> None:
    c = _checks(Defects(contamination=0.2), n=3000)["contamination"]
    assert not c.passed
    assert c.detail["by_arm"]["control"]["rate"] == pytest.approx(0.2, abs=0.03)
    assert c.detail["by_arm"]["treatment"]["rate"] == 0.0


def test_price_errors_fail_the_contamination_gate() -> None:
    c = _checks(Defects(price_error=frozenset({"cust_00000003"})))["contamination"]
    assert not c.passed and c.detail["price_errors"] == 1


def test_missing_outcomes_gate() -> None:
    churners = frozenset(f"cust_{i:08d}" for i in range(0, 400))
    c = _checks(Defects(churn_mid_window=churners))["missing_outcomes"]
    assert not c.passed
    assert c.detail["rate_treatment"] > V.max_missing_rate or c.detail["rate_control"] > (
        V.max_missing_rate
    )


def _units(n: int = 2000, seed: int = 0) -> UnitTable:
    units = build_units(make_extract(n, seed=seed), CFG)[0]
    return units.subset(units.experiment == 0)


def test_balance_passes_under_randomisation_and_fails_on_selection() -> None:
    units = _units()
    assert balance_check(units, V.balance_alpha, V.smd_flag, "x").passed
    # negative control: treated units selected on pre-period demand (a broken randomiser)
    keep = ~units.assigned_treatment | (units.pre_requested > np.median(units.pre_requested))
    skewed = balance_check(units.subset(keep), V.balance_alpha, V.smd_flag, "x")
    assert not skewed.passed
    assert "pre_log_rate" in skewed.detail["smd_flagged"]
    tiny = units.subset(np.arange(units.n) < 2)
    assert not balance_check(tiny, V.balance_alpha, V.smd_flag, "x").passed


def _x_mean(designs: tuple[ExperimentDesign, ...]) -> dict[int, float]:
    return {i: d.treated_fraction * d.log_ratio for i, d in enumerate(designs)}


def test_interference_is_flagged_when_tests_interact() -> None:
    units, designs, _ = build_units(make_extract(3000), CFG)
    x_mean = _x_mean(designs)
    ok = interference_check(units, 0, x_mean, V.interference_alpha, "x")
    assert ok.passed and not ok.gate
    # inject a strong cross-test interaction into test 0's outcome
    other = units.experiment == 1
    z = dict(zip(units.customer[other].tolist(), units.x_assigned[other].tolist(), strict=True))
    own = units.experiment == 0
    zz = np.array([z.get(c, 0.0) for c in units.customer.tolist()])
    delta = units.delta.copy()
    delta[own] += 30.0 * units.x_assigned[own] * zz[own]
    bad = interference_check(
        UnitTable(**{**units.__dict__, "delta": delta}), 0, x_mean, V.interference_alpha, "x"
    )
    assert not bad.passed and bad.detail["interaction_p"] < V.interference_alpha
    single, one_design, _ = build_units(make_extract(200, products=("p_up",)), CFG)
    detail = interference_check(single, 0, _x_mean(one_design), V.interference_alpha, "x").detail
    assert "reason" in detail


def test_product_mix_heterogeneity_is_not_mistaken_for_interference() -> None:
    """Regression (Phase 5 worlds): only starters (the most elastic tier) use p_down. The
    UNcentred assignment sum of the other test then has a tier-dependent mean, and x * z picks
    up the elasticity difference; centring on the design mean removes the false flag."""
    extract = make_extract(6000, seed=3, product_tiers={"p_down": ("starter",)})
    units, designs, _ = build_units(extract, CFG)
    centred = interference_check(units, 0, _x_mean(designs), V.interference_alpha, "x")
    assert centred.passed, centred.detail
    uncentred = interference_check(units, 0, {0: 0.0, 1: 0.0}, V.interference_alpha, "x")
    assert not uncentred.passed


def test_guardrails_report_churn_and_revenue_by_arm() -> None:
    g = guardrails(_units())
    assert set(g) == {"control", "treatment", "churn_rate_difference", "revenue_ratio"}
    assert g["treatment"]["units"] + g["control"]["units"] == _units().n
    assert g["revenue_ratio"] > 0.0


def test_ols_cluster_matches_plain_ols_coefficients() -> None:
    rng = np.random.default_rng(0)
    x = np.stack([np.ones(500), rng.normal(size=500)], axis=1)
    y = 1.0 + 2.0 * x[:, 1] + rng.normal(size=500)
    beta, cov = ols_cluster(y, x, np.arange(500))
    assert beta == pytest.approx(np.linalg.lstsq(x, y, rcond=None)[0])
    assert np.all(np.diag(cov) > 0)
