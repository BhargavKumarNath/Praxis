"""Unit-level analysis table for randomised price tests.

One row per (test, customer) that is ELIGIBLE under the pre-registered rules, which use only
pre-treatment information: customer created by the start of the pre-period, not churned before
the assignment date, and at least ``min_pre_units`` requested units of the tested product in the
pre-period. Assignment is recomputed from the registry salt (never trusted from the log); the
logged exposure on the assignment date is kept beside it so the two can be audited.

Outcome: requested units per active day in the test window, against the same rate in the
pre-period, on the log scale (``delta``). Churn ends the active window; fewer than
``min_active_days`` active days makes the outcome missing.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.domain.experiments import assign_arm
from praxis.elasticity.config import ElasticityConfig, ExperimentDesign
from praxis.elasticity.warehouse import Customer, ExperimentExtract, WarehouseExtract

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]
BOOL = NDArray[np.bool_]
STR = NDArray[np.str_]

# Exposure status of a unit on its assignment date (audit against the assigned arm).
EXPOSED_OK = "ok"  # logged arm == assigned arm, price == the assigned arm's price
EXPOSURE_MISSING = "missing"  # no exposure logged on the assignment date
ARM_MISMATCH = "arm_mismatch"  # logged arm != recomputed arm (assignment / logging defect)
CONTAMINATED = "contaminated"  # charged the OTHER arm's price (non-compliance)
PRICE_ERROR = "price_error"  # price matches neither arm
CONFLICTING = "conflicting"  # several different exposures on the assignment date


@dataclass(frozen=True)
class UnitTable:
    experiment: I64  # index into ``designs``
    customer: STR
    product: STR
    tier: STR
    industry: STR
    region: STR
    is_existing: BOOL
    assigned_treatment: BOOL
    exposure_status: STR
    x_assigned: F64  # log(assigned arm price / control price): 0 or log ratio
    x_exposed: F64  # log(price actually charged / list price); NaN if no exposure
    pre_requested: F64
    post_requested: F64
    post_served: F64
    post_value_micros: F64
    active_days: I64
    churned_in_window: BOOL
    missing_outcome: BOOL
    delta: F64  # NaN when the outcome is missing
    design_weight: F64  # pi (1 - pi) (log ratio)^2: the OLS weight of the unit's test

    @property
    def n(self) -> int:
        return int(self.experiment.size)

    @property
    def pre_log_rate(self) -> F64:
        out: F64 = np.log(self.pre_requested + 0.5)
        return out

    def subset(self, mask: BOOL) -> UnitTable:
        return UnitTable(**{f.name: getattr(self, f.name)[mask] for f in fields(self)})

    def data_version(self) -> str:
        h = hashlib.sha256()
        for f in fields(self):
            arr = np.ascontiguousarray(getattr(self, f.name))
            h.update(f.name.encode())
            h.update(arr.astype(str).tobytes() if arr.dtype.kind == "U" else arr.tobytes())
        return f"units-{h.hexdigest()[:16]}"

    def records(self) -> list[dict[str, Any]]:
        """Row dicts (JSON-friendly) for the unit file the evaluation harness reads."""
        names = [f.name for f in fields(self)]
        cols = [getattr(self, k).tolist() for k in names]
        return [
            {
                k: (None if isinstance(v, float) and math.isnan(v) else v)
                for k, v in zip(names, r, strict=True)
            }
            for r in zip(*cols, strict=True)
        ]


@dataclass(frozen=True)
class ExposureCensus:
    """Every exposure logged for a test, including customers that were never eligible."""

    eligible_units: int
    logged_customers: int
    late_by_arm: dict[str, int]  # first exposure after the assignment date (new arrivals)


def build_units(
    extract: WarehouseExtract, cfg: ElasticityConfig
) -> tuple[UnitTable, tuple[ExperimentDesign, ...], tuple[ExposureCensus, ...]]:
    designs = tuple(e.design for e in extract.experiments)
    rows: list[dict[str, Any]] = []
    census: list[ExposureCensus] = []
    for k, exp in enumerate(extract.experiments):
        unit_rows = [
            _unit(k, cid, exp, extract.customers[cid], cfg)
            for cid in sorted(exp.usage)
            if cid in extract.customers and _eligible(exp, cid, extract.customers[cid], cfg)
        ]
        rows.extend(unit_rows)
        census.append(_census(exp, len(unit_rows)))
    return _to_table(rows), designs, tuple(census)


def _eligible(exp: ExperimentExtract, cid: str, cust: Customer, cfg: ElasticityConfig) -> bool:
    d, e = exp.design, cfg.eligibility
    if e.require_created_by_pre_start and cust.created > d.pre_start(e.pre_window_days):
        return False
    if cust.churned is not None and cust.churned < d.assignment_date:
        return False
    return exp.usage[cid].pre_requested >= e.min_pre_units


def _exposure_status(
    d: ExperimentDesign, assigned: bool, on_day: tuple[Any, ...], tol: int
) -> tuple[str, float]:
    if not on_day:
        return EXPOSURE_MISSING, math.nan
    if len({(x.arm, x.unit_price_micros) for x in on_day}) > 1:
        return CONFLICTING, math.nan
    x = on_day[0]
    exposed = math.log(x.unit_price_micros / x.list_price_micros)
    arm = "treatment" if assigned else "control"
    if x.arm != arm:
        return ARM_MISMATCH, exposed
    treat_price = round(x.list_price_micros * d.treatment_price_ratio)
    expected, other = (
        (treat_price, x.list_price_micros) if assigned else (x.list_price_micros, treat_price)
    )
    if abs(x.unit_price_micros - expected) <= tol:
        return EXPOSED_OK, exposed
    if abs(x.unit_price_micros - other) <= tol:
        return CONTAMINATED, exposed
    return PRICE_ERROR, exposed


def _unit(
    k: int, cid: str, exp: ExperimentExtract, cust: Customer, cfg: ElasticityConfig
) -> dict[str, Any]:
    d = exp.design
    assigned = assign_arm(d.salt, cid, d.treated_fraction) == "treatment"
    on_day = tuple(x for x in exp.exposures.get(cid, ()) if x.day == d.assignment_date)
    status, x_exposed = _exposure_status(d, assigned, on_day, cfg.validity.price_tolerance_micros)
    churned_in = cust.churned is not None and cust.churned <= d.end_date
    active = d.window_days
    if cust.churned is not None and churned_in:
        active = (cust.churned - d.assignment_date).days + 1
    u = exp.usage[cid]
    missing = active < cfg.outcome.min_active_days
    c, pre_days = cfg.outcome.log_offset, cfg.eligibility.pre_window_days
    delta = (
        math.nan
        if missing
        else math.log((u.post_requested + c) / active) - math.log((u.pre_requested + c) / pre_days)
    )
    pi = d.treated_fraction
    return {
        "experiment": k,
        "customer": cid,
        "product": d.product,
        "tier": cust.tier,
        "industry": cust.industry,
        "region": cust.region,
        "is_existing": cust.is_existing,
        "assigned_treatment": assigned,
        "exposure_status": status,
        "x_assigned": d.log_ratio if assigned else 0.0,
        "x_exposed": x_exposed,
        "pre_requested": float(u.pre_requested),
        "post_requested": float(u.post_requested),
        "post_served": float(u.post_served),
        "post_value_micros": float(u.post_value_micros),
        "active_days": active,
        "churned_in_window": churned_in,
        "missing_outcome": missing,
        "delta": delta,
        "design_weight": pi * (1.0 - pi) * d.log_ratio**2,
    }


def _census(exp: ExperimentExtract, eligible: int) -> ExposureCensus:
    late: dict[str, int] = {"control": 0, "treatment": 0}
    logged = [xs[0] for xs in exp.exposures.values() if xs]
    for first in logged:
        if first.day > exp.design.assignment_date and first.arm in late:
            late[first.arm] += 1
    return ExposureCensus(eligible, len(logged), late)


_DTYPES: dict[str, Any] = {
    "experiment": np.int64,
    "active_days": np.int64,
    "is_existing": np.bool_,
    "assigned_treatment": np.bool_,
    "churned_in_window": np.bool_,
    "missing_outcome": np.bool_,
    "customer": np.str_,
    "product": np.str_,
    "tier": np.str_,
    "industry": np.str_,
    "region": np.str_,
    "exposure_status": np.str_,
}


def _to_table(rows: list[dict[str, Any]]) -> UnitTable:
    cols: dict[str, Any] = {}
    for f in fields(UnitTable):
        dtype = _DTYPES.get(f.name, np.float64)
        cols[f.name] = np.array([r[f.name] for r in rows], dtype=dtype)
    return UnitTable(**cols)
