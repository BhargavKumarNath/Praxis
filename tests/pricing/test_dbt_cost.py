"""The marginal-cost mart's product catalogue stays equal to the simulator's (static check)."""

from __future__ import annotations

import re
from pathlib import Path

from praxis.simulator.config import load_config

DBT = Path(__file__).resolve().parents[2] / "dbt"


def test_marginal_cost_products_match_the_simulator_catalogue() -> None:
    text = (DBT / "dbt_project.yml").read_text()
    match = re.search(r"^\s*marginal_cost_products:\s*\[(?P<items>[^\]]*)\]", text, re.MULTILINE)
    assert match, "dbt_project.yml must list marginal_cost_products"
    listed = [p.strip() for p in match.group("items").split(",") if p.strip()]
    assert sorted(listed) == sorted(p.id for p in load_config().products)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", p) for p in listed)


def test_the_staging_model_never_queries_at_compile_time() -> None:
    sql = (DBT / "models/staging/stg_marginal_costs.sql").read_text()
    assert "run_query" not in sql and "var('marginal_cost_products')" in sql
