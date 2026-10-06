"""Eligibility, exposure audit and outcome definition of the unit table."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from praxis.elasticity.config import load_elasticity_config
from praxis.elasticity.dataset import (
    ARM_MISMATCH,
    CONFLICTING,
    CONTAMINATED,
    EXPOSED_OK,
    EXPOSURE_MISSING,
    PRICE_ERROR,
    build_units,
)
from praxis.elasticity.warehouse import Customer, Exposure, Usage, WarehouseExtract
from tests.elasticity.helpers import (
    PRE_START,
    START,
    Defects,
    design,
    make_extract,
)

CFG = load_elasticity_config()


def _one(
    customer: Customer, usage: Usage, exposures: tuple[Exposure, ...] = ()
) -> WarehouseExtract:
    base = make_extract(n_customers=1, products=("p_up",))
    exp = replace(base.experiments[0], usage={"cust_x": usage}, exposures={"cust_x": exposures})
    return WarehouseExtract({"cust_x": customer}, (exp,))


CUSTOMER = Customer("growth", "saas", "eu_west", True, PRE_START, None)
USAGE = Usage(pre_requested=280, post_requested=200, post_served=190, post_value_micros=10)


def test_eligibility_uses_only_pre_treatment_information() -> None:
    eligible = build_units(_one(CUSTOMER, USAGE), CFG)[0]
    assert eligible.n == 1
    late = replace(CUSTOMER, created=PRE_START + timedelta(days=1))
    churned_before = replace(CUSTOMER, churned=START - timedelta(days=1))
    low_volume = replace(USAGE, pre_requested=CFG.eligibility.min_pre_units - 1)
    assert build_units(_one(late, USAGE), CFG)[0].n == 0
    assert build_units(_one(churned_before, USAGE), CFG)[0].n == 0
    assert build_units(_one(CUSTOMER, low_volume), CFG)[0].n == 0
    churned_on_start = replace(CUSTOMER, churned=START)
    assert build_units(_one(churned_on_start, USAGE), CFG)[0].n == 1


def test_outcome_is_log_rate_change_per_active_day() -> None:
    units = build_units(_one(CUSTOMER, USAGE), CFG)[0]
    c = CFG.outcome.log_offset
    assert units.delta[0] == pytest.approx(math.log((200 + c) / 28) - math.log((280 + c) / 28))
    assert units.active_days[0] == 28 and not units.missing_outcome[0]


def test_churn_shortens_the_window_and_short_windows_are_missing() -> None:
    mid = replace(CUSTOMER, churned=START + timedelta(days=19))
    units = build_units(_one(mid, USAGE), CFG)[0]
    assert units.active_days[0] == 20 and units.churned_in_window[0]
    c = CFG.outcome.log_offset
    assert units.delta[0] == pytest.approx(math.log((200 + c) / 20) - math.log((280 + c) / 28))
    early = replace(CUSTOMER, churned=START + timedelta(days=CFG.outcome.min_active_days - 2))
    gone = build_units(_one(early, USAGE), CFG)[0]
    assert gone.missing_outcome[0] and math.isnan(gone.delta[0])
    after = replace(CUSTOMER, churned=START + timedelta(days=40))
    assert build_units(_one(after, USAGE), CFG)[0].active_days[0] == 28


def _status(*exposures: Exposure) -> str:
    return str(build_units(_one(CUSTOMER, USAGE, exposures), CFG)[0].exposure_status[0])


def test_exposure_status_classification() -> None:
    from praxis.domain.experiments import assign_arm

    d = design("p_up")
    arm = assign_arm(d.salt, "cust_x", 0.5)
    other = "control" if arm == "treatment" else "treatment"
    lp = 400_000
    own_price = round(lp * 1.2) if arm == "treatment" else lp
    other_price = lp if arm == "treatment" else round(lp * 1.2)
    assert _status(Exposure(START, arm, own_price, lp)) == EXPOSED_OK
    assert _status(Exposure(START, arm, own_price + 1, lp)) == EXPOSED_OK  # rounding tolerance
    assert _status() == EXPOSURE_MISSING
    assert _status(Exposure(START + timedelta(days=1), arm, own_price, lp)) == EXPOSURE_MISSING
    assert _status(Exposure(START, other, own_price, lp)) == ARM_MISMATCH
    assert _status(Exposure(START, arm, other_price, lp)) == CONTAMINATED
    assert _status(Exposure(START, arm, lp * 3, lp)) == PRICE_ERROR
    assert (
        _status(Exposure(START, arm, own_price, lp), Exposure(START, arm, other_price, lp))
        == CONFLICTING
    )


def test_assigned_and_exposed_log_price() -> None:
    units = build_units(make_extract(400, defects=Defects(contamination=0.5)), CFG)[0]
    treated = units.assigned_treatment
    assert np.allclose(units.x_assigned[~treated], 0.0)
    up = units.product == "p_up"
    assert np.allclose(units.x_assigned[treated & up], math.log(1.2))
    contaminated = units.exposure_status == CONTAMINATED
    assert contaminated.any()
    assert np.allclose(units.x_exposed[contaminated & up], math.log(1.2), atol=1e-6)
    assert np.allclose(units.design_weight[up], 0.25 * math.log(1.2) ** 2)


def test_data_version_is_deterministic_and_content_addressed() -> None:
    a = build_units(make_extract(300, seed=1), CFG)[0]
    b = build_units(make_extract(300, seed=1), CFG)[0]
    c = build_units(make_extract(300, seed=2), CFG)[0]
    assert a.data_version() == b.data_version() != c.data_version()
    assert a.data_version().startswith("units-")


def test_records_are_json_friendly() -> None:
    mid = replace(CUSTOMER, churned=START + timedelta(days=2))
    rows = build_units(_one(mid, USAGE), CFG)[0].records()
    assert rows[0]["delta"] is None and rows[0]["customer"] == "cust_x"


def test_late_arrivals_are_counted_but_never_eligible() -> None:
    extract = make_extract(50, products=("p_up",))
    exp = extract.experiments[0]
    late = {"cust_late": (Exposure(START + timedelta(days=3), "treatment", 480_000, 400_000),)}
    extract = WarehouseExtract(
        extract.customers, (replace(exp, exposures={**exp.exposures, **late}),)
    )
    units, _, census = build_units(extract, CFG)
    assert "cust_late" not in units.customer.tolist()
    assert census[0].late_by_arm["treatment"] == 1
