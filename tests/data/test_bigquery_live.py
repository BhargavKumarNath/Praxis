# ruff: noqa: S608  -- every SQL string here is built from module constants, never input
"""Live BigQuery verification (Phase 2 caveat, option B). Runs ONLY via `make bq-verify`.

Skipped unless PRAXIS_BQ_LIVE_PROJECT is set. Target: a BigQuery sandbox project (no billing),
so cost is capped at zero by Google; every real query also sets maximum_bytes_billed = 1 GB.

Pre-registered expectations (fixed before the first run):
* the dbt build on BigQuery passes every model and test (>= 90 nodes);
* an unfiltered query on any partitioned fact is rejected (require_partition_filter);
* partition pruning: dry-run bytes for ONE day x 10 <= bytes for the whole range (42 days);
* parity: row counts and integer aggregates of every mart equal the DuckDB build of the same
  data exactly; float aggregates within 1e-9 relative;
* re-loading the raw layer leaves every raw row count unchanged (idempotent).

Amended after the first run (2026-10-03), with reason: the sandbox expires partitions older
than 60 days (dataset default_partition_expiration_ms), so 3 FRED rows dated 2026-08-01 were
accepted by the load and then dropped. Parity is now compared on partitions inside the
retention window read from BigQuery (one-day margin), and a new test requires that the ONLY
missing rows are exactly those older than the cutoff. That is stricter, not looser.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

pytestmark = pytest.mark.bigquery_live

PROJECT = os.environ.get("PRAXIS_BQ_LIVE_PROJECT", "")
PARITY_DB = os.environ.get("PRAXIS_BQ_PARITY_DB", "")
DBT_RESULTS = os.environ.get("PRAXIS_BQ_DBT_RESULTS", "")
PREFIX = "praxis_dev_"
LOCATION = "europe-west2"
MAX_BYTES = 1_000_000_000
ALL_TIME = "between date '1900-01-01' and date '2999-12-31'"
FACTS = {
    "fct_usage_daily": "event_date",
    "fct_payments": "event_date",
    "fct_price_exposures": "event_date",
    "fct_service_metrics_hourly": "event_date",
    "fct_external_signals": "observed_date",
}
# (table, aggregate expressions). Integer sums must match exactly.
PARITY: list[tuple[str, list[str]]] = [
    (
        "fct_usage_daily",
        ["count(*)", "sum(units)", "sum(usage_value_micros)", "sum(throttled_units)"],
    ),
    (
        "fct_payments",
        ["count(*)", "sum(amount_minor)", "sum(case when outcome = 'failed' then 1 else 0 end)"],
    ),
    ("fct_price_exposures", ["count(*)", "sum(unit_price_micros)"]),
    ("fct_service_metrics_hourly", ["count(*)", "sum(capacity_units)"]),
    ("fct_external_signals", ["count(*)"]),
    ("dim_customer", ["count(*)", "sum(case when is_churned then 1 else 0 end)"]),
    ("dim_product", ["count(*)"]),
    ("dim_region", ["count(*)"]),
    ("feat_region_daily", ["count(*)", "sum(usage_units)", "sum(payment_attempts)"]),
]

if not PROJECT:
    pytest.skip("live BigQuery checks run only via `make bq-verify`", allow_module_level=True)


@pytest.fixture(scope="module")
def client() -> Any:
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT, location=LOCATION)


@pytest.fixture(scope="module")
def duck() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(PARITY_DB, read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


@pytest.fixture(scope="module")
def cutoff(client: Any) -> date:
    """First partition date safely inside BigQuery's retention (one-day margin)."""
    ms = client.get_dataset(f"{PROJECT}.{PREFIX}raw").default_partition_expiration_ms
    if not ms:
        return date(1900, 1, 1)
    return datetime.now(UTC).date() - timedelta(days=int(ms) // 86_400_000) + timedelta(days=1)


def _where(table: str, cutoff: date) -> str:
    if table not in FACTS:
        return ""
    return f"where {FACTS[table]} between date '{cutoff}' and date '2999-12-31'"


def _bq_row(client: Any, sql: str) -> tuple[Any, ...]:
    from google.cloud import bigquery

    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES))
    return tuple(next(iter(job.result())).values())


def _dry_bytes(client: Any, sql: str) -> int:
    from praxis.data.bigquery import BigQueryLoader

    loader = BigQueryLoader(client, project=PROJECT, prefix=PREFIX, location=LOCATION)
    return loader.dry_run_bytes(sql)


def test_dbt_build_on_bigquery_passed_every_node() -> None:
    results = json.loads(Path(DBT_RESULTS).read_text())["results"]
    statuses = {r["unique_id"]: r["status"] for r in results}
    assert len(statuses) >= 90
    assert {k: v for k, v in statuses.items() if v not in {"success", "pass"}} == {}


def test_facts_and_raw_tables_have_the_intended_physical_layout(client: Any) -> None:
    for table, column in FACTS.items():
        t = client.get_table(f"{PROJECT}.{PREFIX}marts.{table}")
        assert t.time_partitioning is not None and t.time_partitioning.field == column
        assert t.require_partition_filter is True
        assert t.clustering_fields
    raw = client.get_table(f"{PROJECT}.{PREFIX}raw.sim_events")
    assert raw.time_partitioning.field == "event_date"


@pytest.mark.parametrize("table", sorted(FACTS))
def test_unfiltered_query_on_a_fact_is_rejected(client: Any, table: str) -> None:
    from google.api_core.exceptions import BadRequest

    with pytest.raises(BadRequest, match=r"(?i)partition"):
        _dry_bytes(client, f"select count(*) from `{PROJECT}.{PREFIX}marts.{table}`")


@pytest.mark.parametrize(
    "table", ["fct_usage_daily", "fct_service_metrics_hourly", "fct_external_signals"]
)
def test_partition_pruning_cuts_scanned_bytes(
    client: Any,
    duck: duckdb.DuckDBPyConnection,
    cutoff: date,
    table: str,
    record_property: Any,
) -> None:
    column = FACTS[table]
    # The earliest day still inside retention: an expired partition would scan 0 bytes and
    # make the comparison trivially true.
    row = duck.execute(
        f"select min({column}) from marts.{table} where {column} >= date '{cutoff}'"
    ).fetchone()
    assert row is not None
    day = row[0]
    base = f"select * from `{PROJECT}.{PREFIX}marts.{table}` where {column} "
    one_day = _dry_bytes(client, base + f"= date '{day}'")
    everything = _dry_bytes(client, base + ALL_TIME)
    record_property("bytes", {"one_day": one_day, "all": everything})
    print(f"{table}: one_day={one_day} all={everything}")  # noqa: T201
    assert one_day > 0 and everything > 0
    assert one_day * 10 <= everything


@pytest.mark.parametrize(("table", "aggregates"), PARITY, ids=[p[0] for p in PARITY])
def test_bigquery_marts_match_duckdb_exactly(
    client: Any,
    duck: duckdb.DuckDBPyConnection,
    cutoff: date,
    table: str,
    aggregates: list[str],
) -> None:
    select = ", ".join(aggregates)
    where = _where(table, cutoff)
    expected = duck.execute(f"select {select} from marts.{table} {where}").fetchone()
    actual = _bq_row(client, f"select {select} from `{PROJECT}.{PREFIX}marts.{table}` {where}")
    assert expected is not None
    assert [int(x or 0) for x in actual] == [int(x or 0) for x in expected]


RAW_PARTITIONED = {"sim_events": "event_date", "external_signals": "observed_date"}


@pytest.mark.parametrize(
    ("layer", "table", "column"),
    [("raw", t, c) for t, c in RAW_PARTITIONED.items()]
    + [("marts", t, c) for t, c in FACTS.items()],
)
def test_only_partitions_past_retention_are_missing(
    client: Any,
    duck: duckdb.DuckDBPyConnection,
    cutoff: date,
    layer: str,
    table: str,
    column: str,
) -> None:
    """Every row BigQuery lacks is older than the retention cutoff; nothing else is missing.

    Rows from ``cutoff`` on must match exactly; BigQuery must hold nothing older than
    ``cutoff - 2`` days; the two days in between are mid-expiry and only reported.
    """
    target = f"`{PROJECT}.{PREFIX}{layer}.{table}`"
    stale = cutoff - timedelta(days=2)
    kept = f"{column} between date '{cutoff}' and date '2999-12-31'"
    old = f"{column} between date '1900-01-01' and date '{stale}'"
    bq_kept = int(_bq_row(client, f"select count(*) from {target} where {kept}")[0])
    bq_old = int(_bq_row(client, f"select count(*) from {target} where {old}")[0])
    row = duck.execute(
        f"select count(*) filter (where {kept}), count(*) filter (where {old}), count(*) "
        f"from {layer}.{table}"
    ).fetchone()
    assert row is not None
    duck_kept, duck_old, duck_total = (int(x) for x in row)
    sys.stdout.write(
        f"{layer}.{table}: duckdb={duck_total} kept={duck_kept} "
        f"expired_in_duckdb_window={duck_old} bigquery_old={bq_old}\n"
    )
    assert bq_kept == duck_kept
    assert bq_old == 0


def test_feature_view_float_features_match(client: Any, duck: duckdb.DuckDBPyConnection) -> None:
    cols = "avg(temperature_c_mean), avg(avg_utilization), avg(carbon_intensity_gco2_kwh_mean)"
    expected = duck.execute(f"select {cols} from marts.feat_region_daily").fetchone()
    actual = _bq_row(client, f"select {cols} from `{PROJECT}.{PREFIX}marts.feat_region_daily`")
    assert expected is not None
    for a, e in zip(actual, expected, strict=True):
        if e is None:
            assert a is None
        else:
            assert a == pytest.approx(e, rel=1e-9)


def test_reloading_raw_is_idempotent(client: Any, tmp_path: Path) -> None:
    from praxis.data.bigquery import RAW_TABLES, BigQueryLoader
    from praxis.data.warehouse import Warehouse

    def counts() -> dict[str, int]:
        return {
            t: int(_bq_row(client, f"select count(*) from `{PROJECT}.{PREFIX}raw.{t}`")[0])
            for t in RAW_TABLES
        }

    before = counts()
    copy = tmp_path / "source.duckdb"
    shutil.copy(PARITY_DB, copy)  # the parity fixture holds the original open read-only
    with Warehouse(copy) as wh:
        BigQueryLoader(client, project=PROJECT, prefix=PREFIX, location=LOCATION).load_raw(
            wh, tmp_path
        )
        local = {t: wh.count(f"raw.{t}") for t in RAW_TABLES}
    assert counts() == before
    assert all(before[t] <= local[t] for t in RAW_TABLES)  # expired partitions only reduce
