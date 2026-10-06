"""Synthetic experiment extracts with KNOWN elasticity (fast; no simulator, no dbt).

``make_extract`` builds the same structures ``praxis.elasticity.warehouse`` returns, with
controllable defects (dropped exposures, wrong arms, contamination, price errors, early churn,
a common time trend) so each validity check and estimator can be tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from praxis.domain.experiments import assign_arm
from praxis.elasticity.config import (
    ElasticityConfig,
    ExperimentDesign,
    ExperimentRegistry,
    load_elasticity_config,
)
from praxis.elasticity.warehouse import (
    Customer,
    ExperimentExtract,
    Exposure,
    Usage,
    WarehouseExtract,
)

START = date(2026, 2, 2)
END = date(2026, 3, 1)
PRE_START = START - timedelta(days=28)
TIERS = ("enterprise", "growth", "starter")
INDUSTRIES = ("fintech", "media", "saas")
REGIONS = ("eu_west", "us_east")
TRUE_TIER = {"enterprise": -0.6, "growth": -1.1, "starter": -1.8}
INDUSTRY_SHIFT = {"fintech": 0.15, "media": -0.15, "saas": 0.0}
LIST_PRICE = {"p_up": 400_000, "p_down": 70_000}
RATIOS = {"p_up": 1.2, "p_down": 1 / 1.2}


def design(product: str = "p_up", ratio: float | None = None) -> ExperimentDesign:
    return ExperimentDesign(
        id=f"px-{product}",
        product=product,
        assignment_unit="customer",
        salt=f"px-{product}-v1",
        treated_fraction=0.5,
        treatment_price_ratio=RATIOS[product] if ratio is None else ratio,
        assignment_date=START,
        end_date=END,
    )


def registry(products: tuple[str, ...] = ("p_up", "p_down")) -> ExperimentRegistry:
    return ExperimentRegistry(experiments=tuple(design(p) for p in products))


def true_elasticity(tier: str, industry: str) -> float:
    return TRUE_TIER[tier] + INDUSTRY_SHIFT[industry]


@dataclass
class Defects:
    drop_exposure: frozenset[str] = frozenset()  # customer ids without an exposure log
    wrong_arm: frozenset[str] = frozenset()
    price_error: frozenset[str] = frozenset()
    contamination: float = 0.0  # share of control units charged the treatment price
    drop_treated_share: float = 0.0  # SRM: treated units missing from the warehouse
    trend: float = 0.0  # common log change in demand between periods
    churn_mid_window: frozenset[str] = frozenset()  # churn 5 days into the window


def make_extract(
    n_customers: int = 1200,
    seed: int = 0,
    products: tuple[str, ...] = ("p_up", "p_down"),
    defects: Defects | None = None,
    product_tiers: dict[str, tuple[str, ...]] | None = None,
) -> WarehouseExtract:
    """``product_tiers``: restrict a product's users to some tiers (product mix that
    correlates with elasticity, as industries do in the simulated world)."""
    d = defects or Defects()
    rng = np.random.default_rng(seed)
    customers: dict[str, Customer] = {}
    for i in range(n_customers):
        cid = f"cust_{i:08d}"
        churned = START + timedelta(days=4) if cid in d.churn_mid_window else None
        customers[cid] = Customer(
            tier=TIERS[i % 3],
            industry=INDUSTRIES[(i // 3) % 3],
            region=REGIONS[i % 2],
            is_existing=bool(i % 5),
            created=PRE_START,
            churned=churned,
        )
    users = product_tiers or {}
    experiments = tuple(
        _experiment(
            design(p),
            {k: c for k, c in customers.items() if c.tier in users.get(p, TIERS)},
            rng,
            d,
        )
        for p in products
    )
    return WarehouseExtract(customers, experiments)


def _experiment(
    des: ExperimentDesign, customers: dict[str, Customer], rng: np.random.Generator, d: Defects
) -> ExperimentExtract:
    usage: dict[str, Usage] = {}
    exposures: dict[str, tuple[Exposure, ...]] = {}
    list_price = LIST_PRICE[des.product]
    treat_price = round(list_price * des.treatment_price_ratio)
    for cid, c in customers.items():
        treated = assign_arm(des.salt, cid, des.treated_fraction) == "treatment"
        if treated and rng.random() < d.drop_treated_share:
            continue
        charged_treat = treated or rng.random() < d.contamination
        rate = float(np.exp(rng.normal(np.log(8.0), 0.8)))  # units/day
        pre = int(rng.poisson(rate * 28))
        effect = des.log_ratio * true_elasticity(c.tier, c.industry) if charged_treat else 0.0
        active = 5 if c.churned is not None else des.window_days
        post = int(rng.poisson(rate * active * np.exp(effect + d.trend + rng.normal(0, 0.1))))
        usage[cid] = Usage(pre, post, post, post * (treat_price if charged_treat else list_price))
        if cid in d.drop_exposure:
            continue
        arm = "treatment" if treated else "control"
        if cid in d.wrong_arm:
            arm = "control" if treated else "treatment"
        price = treat_price if charged_treat else list_price
        if cid in d.price_error:
            price = list_price * 3
        exposures[cid] = (Exposure(START, arm, price, list_price),)
    return ExperimentExtract(des, usage, exposures)


def small_config() -> ElasticityConfig:
    """The pre-registered config (sampler included); only the cell-size floor is lowered so the
    small synthetic world estimates every cell."""
    return replace_estimation(load_elasticity_config(), min_units_per_cell=30)


def replace_estimation(cfg: ElasticityConfig, **update: object) -> ElasticityConfig:
    est = cfg.estimation.model_copy(update=update)
    return cfg.model_copy(update={"estimation": est})
