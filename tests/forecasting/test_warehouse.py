"""Reading the marts into a panel (real DuckDB file, hand-built marts)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from praxis.forecasting.panel import SeriesKey
from praxis.forecasting.warehouse import (
    WarehouseUnavailable,
    connect,
    latest_complete_day,
    load_panel,
    load_price_plan,
    usage_date_range,
)
from tests.forecasting.helpers import START, make_panel, make_plan, write_warehouse


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "wh.duckdb"
    write_warehouse(path, make_panel(n_days=50), make_plan(), complete_days=45)
    return path


def test_panel_round_trips_through_the_marts(db: Path) -> None:
    source = make_panel(n_days=50)
    con = connect(db)
    start, end = usage_date_range(con)  # type: ignore[misc]
    panel = load_panel(con, start, end)
    assert panel.series == source.series
    np.testing.assert_array_equal(panel.demand, source.demand)
    np.testing.assert_array_equal(panel.served, source.served)
    np.testing.assert_allclose(panel.context["avg_utilization"], source.context["avg_utilization"])
    assert (panel.active == 1).all()  # one synthetic customer per series


def test_segment_is_the_tier_at_signup_not_the_current_tier(db: Path) -> None:
    panel = load_panel(connect(db), START, START + timedelta(days=5))
    assert {s.segment for s in panel.series} == {"s_big", "s_small"}


def test_fixed_series_list_fills_missing_series_with_zeros(db: Path) -> None:
    ghost = SeriesKey("r_east", "p_api", "s_new")
    wanted = [*make_panel(n_days=5).series, ghost]
    panel = load_panel(connect(db), START, START + timedelta(days=4), series=wanted)
    assert ghost in panel.series
    assert not panel.demand[panel.series.index(ghost)].any()
    with pytest.raises(ValueError):
        load_panel(connect(db), START, START - timedelta(days=1))


def test_latest_complete_day_ignores_unfinished_days(db: Path) -> None:
    con = connect(db)
    assert latest_complete_day(con, not_after=START + timedelta(days=49)) == START + timedelta(
        days=44
    )
    assert latest_complete_day(con, not_after=START + timedelta(days=10)) == START + timedelta(
        days=10
    )
    assert latest_complete_day(con, not_after=START - timedelta(days=1)) is None


def test_price_plan_keeps_only_change_points(db: Path) -> None:
    plan = load_price_plan(connect(db), until=START + timedelta(days=49))
    assert plan.changes == {"p_api": ((START, 1000),), "p_gpu": ((START, 500),)}
    longer = tmp_db_with_change(db.parent)
    plan = load_price_plan(connect(longer), until=START + timedelta(days=99))
    assert plan.changes["p_api"] == ((START, 1000), (START + timedelta(days=60), 900))


def tmp_db_with_change(root: Path) -> Path:
    path = root / "long.duckdb"
    write_warehouse(path, make_panel(n_days=100), make_plan())
    return path


def test_missing_warehouse_or_marts_is_reported(tmp_path: Path) -> None:
    with pytest.raises(WarehouseUnavailable, match="not found"):
        connect(tmp_path / "nope.duckdb")
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    con = connect(empty)
    with pytest.raises(WarehouseUnavailable, match="dbt"):
        usage_date_range(con)
    with pytest.raises(WarehouseUnavailable, match="dbt"):
        latest_complete_day(con, not_after=START)


def test_empty_usage_mart_has_no_range(tmp_path: Path) -> None:
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    con.execute("CREATE TABLE marts.fct_usage_daily (event_date DATE)")
    con.close()
    assert usage_date_range(connect(path)) is None
