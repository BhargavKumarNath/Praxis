"""Causal churn response to the bill (praxis.elasticity.churn): units, estimator, document."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from praxis.domain.experiments import assign_arm
from praxis.elasticity import churn
from praxis.elasticity.__main__ import main
from praxis.elasticity.config import load_elasticity_config, load_registry

REGISTRY = load_registry()
CFG = load_elasticity_config()
START, END = churn.concurrent_window(REGISTRY)
PRODUCTS = [d.product for d in REGISTRY.experiments]
LOG_RATIOS = np.array([d.log_ratio for d in REGISTRY.experiments])


def synthetic_units(gamma: float, n: int = 60_000, seed: int = 5) -> churn.BillUnits:
    """Randomised factorial with KNOWN bill response ``gamma`` (Poisson person-time)."""
    rng = np.random.default_rng(seed)
    shares = rng.dirichlet(np.ones(len(PRODUCTS)), size=n)
    treated = rng.random((n, len(PRODUCTS))) < 0.5
    tiers = rng.choice(churn.TIERS, size=n)
    h0 = np.select([tiers == "starter", tiers == "growth"], [0.002, 0.0012], 0.0005)
    shock = np.log1p(shares * treated * np.expm1(LOG_RATIOS)[None, :]).sum(axis=1)
    exposure = np.full(n, 28.0)
    events = rng.poisson(exposure * h0 * np.exp(gamma * shock)).clip(0, 1).astype(float)
    return churn.BillUnits(
        tuple(f"c{i}" for i in range(n)),
        tuple(tiers.tolist()),
        shares,
        treated,
        rng.normal(size=n),
        exposure,
        events,
        np.zeros(n),
    )


def test_joint_and_pooled_fits_recover_a_known_bill_response() -> None:
    units = synthetic_units(3.0)
    joint = churn.fit(units, LOG_RATIOS)
    k0 = len(joint.names) - len(PRODUCTS)
    gamma, se = joint.coef[k0:], joint.se[k0:]
    assert np.all(np.abs(gamma - 3.0) < 4 * se)
    pooled = churn.fit(units, LOG_RATIOS, pooled=True)
    assert pooled.names[-1] == "gamma"
    assert abs(pooled.coef[-1] - 3.0) < 4 * pooled.se[-1]
    assert pooled.se[-1] < se.min()  # pooling uses every test's variation
    # SEs are the larger of model-based and sandwich
    assert np.all(joint.se >= joint.se_model - 1e-15) and np.all(
        joint.se >= joint.se_robust - 1e-15
    )
    het = churn.heterogeneity(gamma, se)
    assert het["df"] == 4.0 and het["p_value"] > 0.001  # a common response, as simulated


def test_no_response_is_estimated_as_none() -> None:
    pooled = churn.fit(synthetic_units(0.0), LOG_RATIOS, pooled=True)
    assert abs(pooled.coef[-1]) < 4 * pooled.se[-1]


def test_heterogeneity_detects_products_that_disagree() -> None:
    het = churn.heterogeneity(np.array([0.0, 5.0, 10.0]), np.array([0.5, 0.5, 0.5]))
    assert het["p_value"] < 1e-6
    assert churn.heterogeneity(np.array([1.0]), np.array([1.0]))["p_value"] == 1.0


def test_fit_failure_is_explicit() -> None:
    units = synthetic_units(0.0, n=10)
    empty = churn.BillUnits(
        units.ids, units.tiers, units.shares, units.treated, units.log_spend,
        np.zeros(10), units.voluntary, units.involuntary,
    )  # fmt: skip
    with pytest.raises(churn.ChurnResponseError, match="fit failed"):
        churn.fit(empty, LOG_RATIOS)


def test_the_design_must_be_concurrent_with_one_test_per_product() -> None:
    first = REGISTRY.experiments[0]
    late = first.model_copy(update={"assignment_date": START + timedelta(days=7)})
    with pytest.raises(churn.ChurnResponseError, match="concurrent"):
        churn.concurrent_window(
            REGISTRY.model_copy(update={"experiments": (late, *REGISTRY.experiments[1:])})
        )
    twice = REGISTRY.model_copy(
        update={"experiments": (first, first.model_copy(update={"id": "x"}))}
    )
    with pytest.raises(churn.ChurnResponseError, match="one test per product"):
        churn.concurrent_window(twice)


def ts(d: date) -> str:
    return f"{d.isoformat()} 12:00:00+00"


def build_db(path: Path) -> Path:
    """Six customers around the eligibility rules; pre-period spend on two products."""
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    con.execute(
        "CREATE TABLE marts.dim_customer (customer_id VARCHAR, initial_tier VARCHAR, "
        "created_at TIMESTAMPTZ, churned_at TIMESTAMPTZ, churn_reason VARCHAR)"
    )
    pre_start = START - timedelta(days=28)
    rows = [
        ("a", "growth", pre_start - timedelta(days=30), None, None),
        ("b", "starter", pre_start - timedelta(days=30), START + timedelta(days=9), "voluntary"),
        ("c", "growth", pre_start - timedelta(days=30), START + timedelta(days=3),
         "involuntary_payment"),
        ("d", "growth", pre_start + timedelta(days=5), None, None),  # joined in the pre-period
        ("e", "growth", pre_start - timedelta(days=30), START - timedelta(days=2), "voluntary"),
        ("f", "growth", pre_start - timedelta(days=30), None, None),  # no pre-period spend
        ("g", "unknown", pre_start - timedelta(days=30), None, None),
    ]  # fmt: skip
    for cid, tier, created, churned, reason in rows:
        con.execute(
            "INSERT INTO marts.dim_customer VALUES (?, ?, ?, ?, ?)",
            [cid, tier, ts(created), None if churned is None else ts(churned), reason],
        )
    con.execute(
        "CREATE TABLE marts.fct_usage_daily (event_date DATE, customer_id VARCHAR, "
        "product VARCHAR, usage_value_micros BIGINT)"
    )
    for cid in "abcdeg":
        con.execute(
            "INSERT INTO marts.fct_usage_daily VALUES (?, ?, 'api_requests', 3000000), "
            "(?, ?, 'gpu_minutes', 1000000), (?, ?, 'other_product', 99000000), "
            "(?, ?, 'api_requests', 77000000)",
            [pre_start, cid, pre_start, cid, pre_start, cid, START, cid],  # START: not pre-period
        )
    con.close()
    return path


def test_units_follow_the_preregistered_eligibility_and_outcome_rules(tmp_path: Path) -> None:
    con = duckdb.connect(str(build_db(tmp_path / "wh.duckdb")), read_only=True)
    units = churn.build_units(con, REGISTRY, CFG)
    con.close()
    # d joined after the pre-period start, e churned before assignment, f has no spend,
    # g has no known tier
    assert units.ids == ("a", "b", "c")
    api, gpu = PRODUCTS.index("api_requests"), PRODUCTS.index("gpu_minutes")
    assert units.shares[0, api] == pytest.approx(0.75) and units.shares[0, gpu] == 0.25
    assert units.log_spend[0] == pytest.approx(math.log(4.0 / 28))  # GBP per pre-period day
    window = (END - START).days + 1
    assert units.exposure.tolist() == [float(window), 10.0, 4.0]
    assert units.voluntary.tolist() == [0.0, 1.0, 0.0]
    assert units.involuntary.tolist() == [0.0, 0.0, 1.0]
    expected = [[assign_arm(d.salt, c, 0.5) == "treatment" for d in REGISTRY.experiments]
                for c in "abc"]  # fmt: skip
    assert units.treated.tolist() == expected  # recomputed from the salt, never from the log
    assert units.data_version() == units.data_version()


def test_no_eligible_units_or_missing_marts_are_refused(tmp_path: Path) -> None:
    duckdb.connect(str(tmp_path / "empty.duckdb")).close()
    con = duckdb.connect(str(tmp_path / "empty.duckdb"), read_only=True)
    with pytest.raises(churn.ChurnResponseError, match="marts"):
        churn.build_units(con, REGISTRY, CFG)
    con.close()
    path = build_db(tmp_path / "wh.duckdb")
    con = duckdb.connect(str(path))
    con.execute("DELETE FROM marts.fct_usage_daily")
    con.close()
    con = duckdb.connect(str(path), read_only=True)
    with pytest.raises(churn.ChurnResponseError, match="no eligible"):
        churn.build_units(con, REGISTRY, CFG)
    con.close()


def test_cli_refuses_an_ungated_or_foreign_report(tmp_path: Path) -> None:
    db = build_db(tmp_path / "wh.duckdb")
    report = tmp_path / "report.json"
    args = [
        "--db",
        str(db),
        "churn-response",
        "--report",
        str(report),
        "--out",
        str(tmp_path / "o.json"),
    ]
    report.write_text(json.dumps({"validity": {"passed": False}}))
    assert main(args) == 1
    report.write_text(json.dumps({"validity": {"passed": True}, "registry_hash": "other"}))
    assert main(args) == 1
    assert not (tmp_path / "o.json").exists()


def test_heterogeneity_recovers_a_known_variance_of_individual_elasticities() -> None:
    """Var(delta | treated) - Var(delta | control) = x^2 Var(e_i), per tier, pooled over tests."""
    from praxis.elasticity.dataset import EXPOSED_OK
    from praxis.elasticity.heterogeneity import heterogeneity
    from tests.elasticity.helpers import make_units

    rng = np.random.default_rng(3)
    n, x = 40_000, math.log(1.2)
    tiers = rng.choice(["starter", "enterprise"], size=n)
    true_var = {"starter": 0.5, "enterprise": 0.08}
    e = np.where(tiers == "starter", -1.8, -0.7) + rng.normal(size=n) * np.sqrt(
        np.where(tiers == "starter", true_var["starter"], true_var["enterprise"])
    )
    treated = rng.random(n) < 0.5
    delta = np.where(treated, e * x, 0.0) + rng.normal(0, 0.3, size=n)
    units = make_units(tiers=tiers, treated=treated, delta=delta, x=x, status=EXPOSED_OK)
    out = heterogeneity(units, [x])
    for tier, var in true_var.items():
        assert abs(out[tier]["variance"] - var) < 4 * out[tier]["se"]
    # no heterogeneity: estimated near zero, never negative in "variance"
    flat = make_units(tiers=tiers, treated=treated, delta=np.where(treated, -1.0 * x, 0.0)
                      + rng.normal(0, 0.3, size=n), x=x, status=EXPOSED_OK)  # fmt: skip
    for est in heterogeneity(flat, [x]).values():
        assert est["variance"] >= 0 and est["variance"] < 4 * est["se"]
