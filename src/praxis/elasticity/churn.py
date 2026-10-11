"""Causal churn response to price from concurrent randomised price tests (Phase 9, ADR 0016).

Phase 6 measured each product's churn response from its own test alone, per unit log PRODUCT
price (relative churn-rate ratio). That scale makes products look heterogeneous for a
mechanical reason: a product that is 2% of a customer's bill cannot move churn like one that
is 40%. Here the response is measured per unit log BILL change:

    h_i(t) = h0_tier(i) * exp(c * z_i + sum_p gamma_p * log(1 + s_ip (exp(a_ip) - 1)))

* s_ip: product p's share of customer i's usage value in the pre-period (pre-treatment);
* a_ip: log(treatment price / control price) if i is in p's treatment arm, else 0 (the arm is
  recomputed from the registry salt, never read from the log); the regressor is the exact log
  bill change log(1 + s_ip (exp(a_ip) - 1)) (Laspeyres, fixed pre-period quantities), whose
  slope at a = 0 is s_ip (ADR 0016 second amendment: the linear s_ip a_ip is a secant and
  biases cut-tested products low and raise-tested products high at +-18%);
* z_i: standardised log pre-period daily spend (pre-treatment precision covariate);
* outcome: VOLUNTARY churn in the test window; involuntary churn ends the exposure.

s_ip * a_ip is the first-order log change of customer i's bill caused by test p, so gamma_p is
the voluntary-churn elasticity with respect to the bill, identified by randomisation (the five
tests form a 2^5 factorial). All gamma_p are estimated jointly by Poisson maximum likelihood
(``praxis.glm``); SEs are the larger of the model-based and sandwich SEs. Pooling across
products (empirical Bayes) is left to the decision layer, which also reports heterogeneity.

The tests must be concurrent (same assignment and end date); otherwise ``ChurnResponseError``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
from numpy.typing import NDArray
from scipy.stats import chi2

from praxis import glm
from praxis.domain.experiments import assign_arm
from praxis.elasticity.config import ElasticityConfig, ExperimentDesign, ExperimentRegistry

F64 = NDArray[np.float64]

METHOD = "bill_shock_poisson.v1"
RESPONSE_FORMAT = 3  # 2: + elasticity_heterogeneity; 3: exact log bill change regressor
TIERS = ("starter", "growth", "enterprise")


class ChurnResponseError(RuntimeError):
    """The design does not support the bill-shock estimate, or the inputs are unusable."""


_CUSTOMERS_SQL = """
SELECT customer_id, initial_tier, CAST(created_at AS DATE), CAST(churned_at AS DATE),
       churn_reason
FROM marts.dim_customer
ORDER BY customer_id
"""

_PRE_SPEND_SQL = """
SELECT customer_id, product, sum(usage_value_micros)::HUGEINT
FROM marts.fct_usage_daily
WHERE event_date BETWEEN ? AND ?
GROUP BY ALL
"""


@dataclass(frozen=True)
class BillUnits:
    """One row per eligible customer of the concurrent tests (columnar)."""

    ids: tuple[str, ...]
    tiers: tuple[str, ...]
    shares: F64  # (n, P) pre-period usage-value shares, P = registry products
    treated: NDArray[np.bool_]  # (n, P)
    log_spend: F64  # log pre-period daily spend (GBP)
    exposure: F64
    voluntary: F64
    involuntary: F64

    @property
    def n(self) -> int:
        return len(self.ids)

    def data_version(self) -> str:
        h = hashlib.sha256("\n".join(self.ids).encode())
        h.update("\n".join(self.tiers).encode())
        for arr in (self.shares, self.treated, self.log_spend, self.exposure, self.voluntary):
            h.update(np.ascontiguousarray(arr).tobytes())
        return "billunits-" + h.hexdigest()[:16]


def concurrent_window(registry: ExperimentRegistry) -> tuple[date, date]:
    starts = {d.assignment_date for d in registry.experiments}
    ends = {d.end_date for d in registry.experiments}
    products = [d.product for d in registry.experiments]
    if len(starts) != 1 or len(ends) != 1:
        raise ChurnResponseError("the bill-shock estimate needs concurrent tests (one window)")
    if len(set(products)) != len(products):
        raise ChurnResponseError("the bill-shock estimate needs one test per product")
    return next(iter(starts)), next(iter(ends))


def build_units(
    con: duckdb.DuckDBPyConnection, registry: ExperimentRegistry, cfg: ElasticityConfig
) -> BillUnits:
    start, end = concurrent_window(registry)
    designs = registry.experiments
    pre_days = cfg.eligibility.pre_window_days
    pre_start = start - timedelta(days=pre_days)
    products = [d.product for d in designs]
    try:
        customers = con.execute(_CUSTOMERS_SQL).fetchall()
        spend_rows = con.execute(_PRE_SPEND_SQL, [pre_start, start - timedelta(days=1)]).fetchall()
    except duckdb.CatalogException as exc:
        raise ChurnResponseError(f"experiment marts are missing: {exc}") from exc
    spend: dict[str, F64] = {}
    for cid, product, value in spend_rows:
        if product in products:
            spend.setdefault(cid, np.zeros(len(products)))[products.index(product)] += float(value)
    rows = [
        _row(r, spend[r[0]], designs, start, end)
        for r in customers
        if _eligible(r, spend, cfg, pre_start, start)
    ]
    if not rows:
        raise ChurnResponseError("no eligible units")
    total = np.array([r[2].sum() for r in rows])
    return BillUnits(
        ids=tuple(r[0] for r in rows),
        tiers=tuple(r[1] for r in rows),
        shares=np.vstack([r[2] / r[2].sum() for r in rows]),
        treated=np.vstack([r[3] for r in rows]),
        log_spend=np.log(total / pre_days / 1e6),
        exposure=np.array([r[4] for r in rows]),
        voluntary=np.array([r[5] for r in rows]),
        involuntary=np.array([r[6] for r in rows]),
    )


def _eligible(
    r: tuple[Any, ...], spend: dict[str, F64], cfg: ElasticityConfig, pre_start: date, start: date
) -> bool:
    cid, tier, created, churned = r[0], r[1], r[2], r[3]
    if tier not in TIERS:
        return False
    if cfg.eligibility.require_created_by_pre_start and created > pre_start:
        return False
    if churned is not None and churned < start:
        return False
    return cid in spend and float(spend[cid].sum()) > 0


def _row(
    r: tuple[Any, ...],
    spend: F64,
    designs: tuple[ExperimentDesign, ...],
    start: date,
    end: date,
) -> tuple[str, str, F64, NDArray[np.bool_], float, float, float]:
    cid, tier, churned, reason = r[0], r[1], r[3], r[4]
    treated = np.array(
        [assign_arm(d.salt, cid, d.treated_fraction) == "treatment" for d in designs]
    )
    window = (end - start).days + 1
    exposure, vol, inv = float(window), 0.0, 0.0
    if churned is not None and churned <= end:
        exposure = float((churned - start).days + 1)
        vol, inv = (1.0, 0.0) if reason == "voluntary" else (0.0, 1.0)
    return cid, tier, spend, treated, exposure, vol, inv


@dataclass(frozen=True)
class Fit:
    names: tuple[str, ...]
    coef: F64
    se: F64  # max(model-based, sandwich)
    se_model: F64
    se_robust: F64
    log_likelihood: float


def _design(units: BillUnits, log_ratios: F64, *, pooled: bool) -> tuple[F64, tuple[str, ...]]:
    tiers = np.array(units.tiers)
    z = (units.log_spend - units.log_spend.mean()) / max(float(units.log_spend.std()), 1e-12)
    # exact log change of the customer's bill at fixed (pre-period) quantities caused by test p:
    # log(1 + s_ip (r_p - 1)); its derivative at r = 1 is s_ip, so gamma_p is the MARGINAL bill
    # elasticity a small decision needs (s_ip log r_p is a secant: biased by +-8-10% at +-18%)
    shock = np.log1p(units.shares * np.expm1(log_ratios)[None, :]) * units.treated
    cols = [(tiers == t).astype(np.float64) for t in TIERS] + [z]
    names = [f"log_h0_{t}" for t in TIERS] + ["log_pre_spend_std"]
    if pooled:
        cols.append(shock.sum(axis=1))
        names.append("gamma")
    else:
        cols += [shock[:, p] for p in range(shock.shape[1])]
        names += [f"gamma_{p}" for p in range(shock.shape[1])]
    return np.column_stack(cols), tuple(names)


def fit(units: BillUnits, log_ratios: F64, *, pooled: bool = False) -> Fit:
    design, names = _design(units, log_ratios, pooled=pooled)
    present = [k for k in range(len(TIERS)) if design[:, k].any()]
    keep = [*present, *range(len(TIERS), design.shape[1])]
    try:
        res = glm.fit_poisson(design[:, keep], units.voluntary, units.exposure, start_column=None)
    except glm.FitError as exc:
        raise ChurnResponseError(f"churn response fit failed: {exc}") from exc
    se_model = np.sqrt(np.diag(res.cov))
    se_robust = np.sqrt(np.diag(res.cov_robust))
    return Fit(
        tuple(names[k] for k in keep),
        res.coef,
        np.maximum(se_model, se_robust),
        se_model,
        se_robust,
        res.log_likelihood,
    )


def heterogeneity(gamma: F64, se: F64) -> dict[str, float]:
    """Cochran's Q of the product estimates around their precision-weighted mean."""
    w = 1.0 / se**2
    mean = float((w * gamma).sum() / w.sum())
    q = float((w * (gamma - mean) ** 2).sum())
    df = len(gamma) - 1
    return {"q": q, "df": float(df), "p_value": float(chi2.sf(q, df)) if df > 0 else 1.0}


def estimate(
    con: duckdb.DuckDBPyConnection,
    registry: ExperimentRegistry,
    cfg: ElasticityConfig,
    *,
    report_data_version: str,
) -> dict[str, Any]:
    """The churn-response evidence document (JSON-ready, content checksum included)."""
    units = build_units(con, registry, cfg)
    designs = registry.experiments
    log_ratios = np.array([d.log_ratio for d in designs])
    joint = fit(units, log_ratios)
    pooled = fit(units, log_ratios, pooled=True)
    k0 = len(joint.names) - len(designs)
    gamma, se = joint.coef[k0:], joint.se[k0:]
    start, end = concurrent_window(registry)
    products = {
        d.product: {
            "experiment_id": d.id,
            "log_ratio": d.log_ratio,
            "gamma": float(gamma[p]),
            "se": float(se[p]),
            "se_model": float(joint.se_model[k0 + p]),
            "se_robust": float(joint.se_robust[k0 + p]),
            "mean_share": float(units.shares[:, p].mean()),
            "treated_units": int(units.treated[:, p].sum()),
        }
        for p, d in enumerate(designs)
    }
    doc: dict[str, Any] = {
        "response_format": RESPONSE_FORMAT,
        "method": METHOD,
        "is_synthetic": True,
        "registry_hash": registry.config_hash,
        "report_data_version": report_data_version,
        "units_version": units.data_version(),
        "assignment_date": start.isoformat(),
        "end_date": end.isoformat(),
        "window_days": (end - start).days + 1,
        "units": units.n,
        "voluntary_events": float(units.voluntary.sum()),
        "involuntary_events": float(units.involuntary.sum()),
        "exposure_days": float(units.exposure.sum()),
        "products": products,
        "pooled_fixed": {"gamma": float(pooled.coef[-1]), "se": float(pooled.se[-1])},
        "heterogeneity": heterogeneity(gamma, se),
        "nuisance": {n: float(c) for n, c in zip(joint.names[:k0], joint.coef[:k0], strict=True)},
    }
    doc["checksum"] = checksum(doc)
    return doc


def checksum(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "checksum"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()
