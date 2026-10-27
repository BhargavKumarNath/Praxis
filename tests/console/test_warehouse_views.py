"""Warehouse-backed panels: executive daily series (with a missing mart reported per figure),
external source health with provenance, and MLOps monitoring reports with thresholds."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from praxis.config import Settings
from praxis.console.app import create_console_app
from praxis.data.models import BatchProvenance, QualityStatus, SignalRecord, SourceId
from praxis.data.warehouse import Warehouse
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import MonitoringReport, RegistryStore
from tests.console.conftest import make_console
from tests.mlops.helpers import mlops_config
from tests.mlops.test_registry import A, with_champion

pytestmark = pytest.mark.integration

DAY0 = date(2026, 1, 1)
DAYS = 40


def _marts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA marts")
    con.execute(
        "CREATE TABLE marts.fct_payments (invoice_id VARCHAR, event_date DATE, "
        "attempt_number INT, amount_minor BIGINT, currency VARCHAR, outcome VARCHAR, "
        "attempted_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ)"
    )
    con.execute(
        "CREATE TABLE marts.fct_usage_daily (event_date DATE, customer_id VARCHAR, "
        "product VARCHAR, region_id VARCHAR, units BIGINT, throttled_units BIGINT, "
        "usage_value_micros BIGINT, observations BIGINT)"
    )
    con.execute(
        "CREATE TABLE marts.dim_customer (customer_id VARCHAR, created_at TIMESTAMPTZ, "
        "churned_at TIMESTAMPTZ)"
    )
    con.execute(
        "CREATE TABLE marts.fct_price_exposures (event_date DATE, product VARCHAR, "
        "list_price_micros BIGINT)"
    )
    for i in range(DAYS):
        d = DAY0 + timedelta(days=i)
        ts = datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
        con.execute(
            "INSERT INTO marts.fct_payments VALUES (?, ?, 1, ?, 'GBP', 'succeeded', ?, ?)",
            [f"in_{i}", d, 1000 + 10 * i, ts, ts],
        )
        con.execute(
            "INSERT INTO marts.fct_payments VALUES (?, ?, 1, 500, 'GBP', 'failed', ?, ?)",
            [f"fail_{i}", d, ts, ts],
        )
        con.execute(
            "INSERT INTO marts.fct_usage_daily VALUES (?, 'c1', 'api_requests', 'r1', ?, 0, 1, 1)",
            [d, 100 + i],
        )
        con.execute(
            "INSERT INTO marts.fct_price_exposures VALUES (?, 'api_requests', ?)",
            [d, 1_000_000 if i < 20 else 1_100_000],
        )
    con.execute("INSERT INTO marts.dim_customer VALUES ('c1', '2026-01-01', NULL)")
    con.execute("INSERT INTO marts.dim_customer VALUES ('c2', '2026-01-05', '2026-01-20 08:00')")


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "wh.duckdb"
    now = datetime.now(UTC)
    with Warehouse(path) as wh:
        wh.migrate()
        prov = BatchProvenance(
            batch_id="b1", source=SourceId.FRED, series_id="DEXUSUK",
            endpoint="https://example.invalid/series", retrieved_at=now,
            checksum_sha256="0" * 64, http_status=200, quality_status=QualityStatus.OK,
        )  # fmt: skip
        wh.record_batch(prov)
        wh.upsert_signals([
            SignalRecord(record_key="k1", source=SourceId.FRED, series_id="DEXUSUK",
                         entity_id="gb", metric="fx", unit="usd_per_gbp",
                         observed_at=now - timedelta(hours=1), value=1.27, batch_id="b1",
                         retrieved_at=now)
        ])  # fmt: skip
        _marts(wh.con)
    return path


@pytest.fixture
def client(pg_engine: Engine, warehouse: Path) -> Iterator[TestClient]:
    with make_console(pg_engine, warehouse) as c:
        yield c


def test_executive_series_come_from_the_marts(client: TestClient) -> None:
    body: dict[str, Any] = client.get("/v1/console/executive").json()
    panels = body["panels"]
    revenue = panels["revenue"]
    assert revenue["status"] == "ok" and revenue["synthetic"] is True
    fig = revenue["data"]["figure"]
    assert fig["value"] == 1000 + 10 * (DAYS - 1)
    assert fig["baseline"] == pytest.approx(1000 + 10 * (DAYS - 1 - 14.5))
    assert fig["baseline_label"] == "median of the previous 28 days"
    assert len(fig["series"]) == DAYS and revenue["data"]["latest_day"] == "2026-02-09"
    assert panels["demand"]["data"]["figure"]["value"] == 100 + DAYS - 1
    assert panels["price_index"]["data"]["figure"]["value"] == pytest.approx(110.0)
    assert panels["recovery_rate"]["data"]["figure"]["value"] == 0.0
    assert panels["churn"]["data"]["figure"]["value"] == 1
    assert panels["active_customers"]["data"]["figure"]["value"] == 1
    margin = panels["gross_margin"]
    assert margin["status"] == "unavailable"
    assert (
        margin["reason"] == "a warehouse table this panel needs is not built (run `make dbt-build`)"
    )
    assert margin["data"] is None


def test_external_sources_carry_provenance(client: TestClient) -> None:
    sources = client.get("/v1/console/system").json()["panels"]["sources"]
    assert sources["status"] == "ok" and sources["synthetic"] is False
    by_name = {s["source"]: s for s in sources["data"]["sources"]}
    fred = by_name["fred"]
    assert fred["state"] == "fresh" and fred["provenance"] == "external"
    assert fred["latest_batch"]["batch_id"] == "b1" and "endpoint" not in fred["latest_batch"]
    assert by_name["eia"]["state"] == "missing" and by_name["eia"]["age_s"] is None


def test_locked_or_corrupt_warehouse_is_unavailable(pg_engine: Engine, tmp_path: Path) -> None:
    bad = tmp_path / "bad.duckdb"
    bad.write_bytes(b"not a duckdb file")
    with make_console(pg_engine, bad) as c:
        panels = c.get("/v1/console/executive").json()["panels"]
    assert {p["status"] for p in panels.values()} == {"unavailable"}
    assert "could not be opened read-only" in panels["revenue"]["reason"]


def test_monitoring_reports_carry_series_and_thresholds(
    client: TestClient, pg_engine: Engine
) -> None:
    registry = ModelRegistry(RegistryStore(pg_engine), mlops_config())
    with_champion(registry)
    registry.store.add_monitoring_report(
        MonitoringReport("rep_1", registry.model_name, A, "probation", date(2026, 8, 1),
                         date(2026, 7, 25), date(2026, 7, 31), False,
                         {"residual": {"coverage_80": 0.81, "vwape_ratio": 1.02}})
    )  # fmt: skip
    data = client.get("/v1/console/mlops").json()["panels"]["monitoring"]["data"]
    (report,) = data["reports"]
    assert report["series"]["coverage_80"] == 0.81 and report["series"]["prediction_psi"] is None
    assert (
        data["thresholds"]["coverage_80_band"] and "pre-registered" in data["thresholds"]["source"]
    )


def test_app_from_settings_wires_the_fleet_and_admin(pg_url: str, tmp_path: Path) -> None:
    settings = Settings(
        database_url=SecretStr(pg_url), warehouse_path=tmp_path / "absent.duckdb",
        console_approvers=SecretStr("alice=" + "a" * 12 + "b" * 12),
    )  # fmt: skip
    with TestClient(create_console_app(settings)) as c:
        body = c.get("/v1/console/system").json()
        who = c.get(
            "/v1/console/whoami", headers={"Authorization": "Bearer " + "a" * 12 + "b" * 12}
        )
    assert body["inputs"]["telemetry_scope"] == "fleet"
    assert who.json() == {"approver": "alice"}


def test_without_a_database_every_store_panel_says_not_configured(tmp_path: Path) -> None:
    with make_console(None, None) as c:
        body = c.get("/v1/console/operator").json()
        detail = c.get("/v1/console/incidents/inc_1")
    assert body["panels"]["incidents"]["reason"] == "not configured: PRAXIS_DATABASE_URL is not set"
    assert detail.status_code == 503 and detail.json()["error"] == "not_configured"
