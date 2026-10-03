"""Offline BigQuery readiness (Phase 2 caveat, option A). No credentials, no network, no cost.

Compiles the whole dbt project for the BigQuery target and checks the SQL with sqlglot.
Negative controls prove each checker can fail. Passing here does not prove the models run
in BigQuery or that partitions prune; that needs a real dry run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest
from sqlglot.errors import ParseError

from praxis.data.config import load_sources_config
from praxis.data.warehouse import Warehouse
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.data import bq_static as bq
from tests.data.test_pipeline import run_dbt

pytestmark = pytest.mark.integration
FACTS = {
    "fct_usage_daily": "event_date",
    "fct_payments": "event_date",
    "fct_price_exposures": "event_date",
    "fct_service_metrics_hourly": "event_date",
    "fct_external_signals": "observed_date",
}
LAYER_FOR_SCHEMA = {"raw": "raw", "staging": "staging", "marts": "marts"}


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> dict[Path, str]:
    work = tmp_path_factory.mktemp("bq_compile")
    return {p: p.read_text() for p in bq.compile_for_bigquery(work)}


@pytest.fixture(scope="module")
def bq_schema(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
    """Column contract taken from the DuckDB build of the same models."""
    root = tmp_path_factory.mktemp("bq_schema")
    sim = root / "sim"
    run_simulation(load_config().with_overrides(n_customers=30, days=10), 1, out_dir=sim)
    db = root / "w.duckdb"
    with Warehouse(db) as wh:
        wh.migrate()
        wh.load_region_locations(load_sources_config().locations)
        wh.load_sim_events(sim / "events.ndjson", json.loads((sim / "manifest.json").read_text()))
    statuses = run_dbt(db, root / "d", "build")
    assert statuses  # built for the column contract
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "SELECT table_schema, table_name, column_name FROM information_schema.columns "
        "WHERE table_schema IN ('raw', 'staging', 'marts')"
    ).fetchall()
    con.close()
    schema: dict[str, dict[str, dict[str, str]]] = {}
    for table_schema, table, column in rows:
        dataset = f"{bq.PREFIX}{LAYER_FOR_SCHEMA[table_schema]}"
        schema.setdefault(dataset, {}).setdefault(table, {})[column] = "STRING"
    return {bq.PROJECT: schema}


def test_every_node_compiled_for_bigquery(compiled: dict[Path, str]) -> None:
    assert len(compiled) >= 90  # 20 models + 72 tests


def test_all_compiled_sql_parses_as_bigquery(compiled: dict[Path, str]) -> None:
    for path, sql in compiled.items():
        try:
            bq.parse(sql)
        except ParseError as exc:  # pragma: no cover - reported below
            pytest.fail(f"{path.name}: {exc}")


def test_no_non_native_type_names_in_bigquery_sql(compiled: dict[Path, str]) -> None:
    offenders = {
        p.name: bq.non_native_types(s) for p, s in compiled.items() if bq.non_native_types(s)
    }
    assert offenders == {}


def test_every_column_and_table_resolves_against_the_built_schema(
    compiled: dict[Path, str], bq_schema: dict[str, dict[str, dict[str, dict[str, str]]]]
) -> None:
    errors = {
        p.name: err
        for p, s in compiled.items()
        if (err := bq.unknown_columns(s, bq_schema)) is not None
    }
    assert errors == {}


def test_every_scan_of_a_partitioned_fact_bounds_its_partition_column(
    compiled: dict[Path, str],
) -> None:
    offenders = {
        p.name: bad for p, s in compiled.items() if (bad := bq.unfiltered_fact_scans(s, FACTS))
    }
    assert offenders == {}


def test_datasets_use_the_terraform_naming_scheme(compiled: dict[Path, str]) -> None:
    text = " ".join(compiled.values())
    datasets = set(re.findall(r"`praxis-placeholder`\.`([a-z_]+)`", text))
    assert datasets == {f"{bq.PREFIX}{layer}" for layer in ("raw", "staging", "marts")}


# --- negative controls: each checker must be able to fail ------------------------------
def test_control_parser_rejects_broken_sql() -> None:
    with pytest.raises(ParseError):
        bq.parse("select from where ((")


def test_control_type_check_flags_duckdb_spellings() -> None:
    assert bq.non_native_types("select cast(x as varchar), cast(y as DOUBLE)") == [
        "double",
        "varchar",
    ]
    assert bq.non_native_types("select cast(x as string), cast(y as float64), x as p") == []


def test_control_column_check_flags_unknown_column_and_table() -> None:
    schema = {"p": {"d": {"t": {"a": "STRING"}}}}
    assert bq.unknown_columns("select a from `p`.`d`.`t`", schema) is None
    assert bq.unknown_columns("select nope from `p`.`d`.`t`", schema) is not None
    assert bq.unknown_columns("select a from `p`.`d`.`missing`", schema) is not None


def test_control_partition_check_flags_unbounded_scans_and_accepts_bounded() -> None:
    facts = {"fct_payments": "event_date"}
    bare = "select count(*) from `p`.`d`.`fct_payments`"
    other_col = "select * from `p`.`d`.`fct_payments` where outcome = 'failed'"
    bounded = (
        "select * from `p`.`d`.`fct_payments` "
        "where event_date between date '2026-01-01' and date '2026-12-31'"
    )
    wrapped = (
        "select * from (select * from `p`.`d`.`fct_payments` "
        "where event_date >= date '1900-01-01') s "
        "where outcome = 'x'"
    )
    joined_unbounded = (
        "select * from `p`.`d`.`dim` d join `p`.`d`.`fct_payments` f on d.id = f.id "
        "where d.event_date > date '2026-01-01'"
    )
    assert bq.unfiltered_fact_scans(bare, facts) == ["fct_payments.event_date"]
    assert bq.unfiltered_fact_scans(other_col, facts) == ["fct_payments.event_date"]
    assert bq.unfiltered_fact_scans(bounded, facts) == []
    assert bq.unfiltered_fact_scans(wrapped, facts) == []
    # a predicate that happens to share the column name on ANOTHER table still counts as a
    # bound in this simple checker; documented limitation, hence the dry run is still needed
    assert bq.unfiltered_fact_scans(joined_unbounded, facts) == []
