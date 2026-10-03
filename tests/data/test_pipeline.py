"""End-to-end data path: simulator -> raw load -> external signals (replayed fixtures) -> dbt.

Real boundaries: DuckDB file, dbt subprocess. Negative controls corrupt a copy of the built
warehouse and assert the specific dbt test that must catch it fails, proving each test can fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from praxis.data.ingest import IngestService
from praxis.data.models import SourceId, TimeWindow
from praxis.data.raw_store import LocalRawStore
from praxis.data.warehouse import Warehouse
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.data.helpers import fixture_handler, make_fetcher, registry

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
DBT_DIR = REPO / "dbt"
SIM_DAYS = 70


def run_dbt(db: Path, workdir: Path, *args: str) -> dict[str, str]:
    """Run dbt; return {unique_id: status} from run_results.json (or sources.json)."""
    env = {
        **os.environ,
        "PRAXIS_DUCKDB_PATH": str(db),
        "DBT_TARGET_PATH": str(workdir / "target"),
        "DBT_LOG_PATH": str(workdir / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    subprocess.run(  # noqa: S603
        [
            str(REPO / ".venv/bin/dbt"),
            *args,
            "--project-dir",
            str(DBT_DIR),
            "--profiles-dir",
            str(DBT_DIR),
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    results = workdir / "target" / ("sources.json" if args[0] == "source" else "run_results.json")
    data = json.loads(results.read_text())["results"]
    return {r["unique_id"]: r["status"] for r in data}


def failing(statuses: dict[str, str]) -> list[str]:
    return sorted(k for k, v in statuses.items() if v in {"fail", "error"})


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("pipeline")
    config = load_config().with_overrides(n_customers=300, days=SIM_DAYS)
    sim_dir = root / "sim"
    run_simulation(config, 42, out_dir=sim_dir)
    db = root / "praxis.duckdb"
    now = datetime.now(UTC)
    with Warehouse(db) as wh:
        wh.migrate()
        from praxis.data.config import load_sources_config

        wh.load_region_locations(load_sources_config().locations)
        wh.load_sim_events(
            sim_dir / "events.ndjson", json.loads((sim_dir / "manifest.json").read_text())
        )
        service = IngestService(
            registry(),
            make_fetcher(fixture_handler()),
            LocalRawStore(root / "raw"),
            wh,
            clock=lambda: now,
        )
        window = TimeWindow(start=date(2026, 1, 5), end=date(2026, 1, 5))
        for sid in SourceId:
            assert service.ingest(sid, window).all_succeeded
    statuses = run_dbt(db, root / "dbt", "build")
    assert failing(statuses) == [], failing(statuses)
    return {"root": root, "db": db, "statuses": statuses}


def test_dbt_build_passes_all_models_and_tests(built: dict[str, Any]) -> None:
    statuses = built["statuses"]
    assert len(statuses) >= 90  # models + tests actually ran
    kinds = {k.split(".")[0] for k in statuses}
    assert kinds == {"model", "test"}
    assert set(statuses.values()) == {"success", "pass"}


def test_required_dbt_test_categories_are_present(built: dict[str, Any]) -> None:
    ids = " ".join(built["statuses"])
    for needle in ("unique_", "not_null_", "relationships_", "accepted_values_", "invariant_"):
        assert needle in ids


def test_feature_view_joins_real_signals_and_never_uses_unreleased_macro_data(
    built: dict[str, Any],
) -> None:
    import duckdb

    con = duckdb.connect(str(built["db"]), read_only=True)
    london = con.execute(
        "SELECT temperature_c_mean, carbon_intensity_gco2_kwh_mean FROM marts.feat_region_daily "
        "WHERE region_id = 'eu_west' AND feature_date = DATE '2026-01-05'"
    ).fetchone()
    assert london is not None and london[0] is not None and london[1] is not None
    assert -10 < london[0] < 10  # a January day in London, deg C
    macro = con.execute(
        "SELECT feature_date, macro_cpiaucsl, macro_cpiaucsl_observed_date "
        "FROM marts.feat_region_daily WHERE region_id = 'eu_west' ORDER BY feature_date"
    ).fetchall()
    first_value_date = min(r[0] for r in macro if r[1] is not None)
    assert first_value_date == date(2026, 2, 15)  # 2026-01-01 + 45-day release lag
    assert all(r[0] >= date(2026, 2, 15) for r in macro if r[1] is not None)
    assert all(r[1] is None for r in macro if r[0] < date(2026, 2, 15))
    con.close()


def test_leakage_test_can_fail(built: dict[str, Any], tmp_path: Path) -> None:
    """Negative control: judge the same data with a longer release lag; it must object."""
    db = tmp_path / "copy.duckdb"
    shutil.copy(built["db"], db)
    statuses = run_dbt(
        db,
        tmp_path / "d",
        "test",
        "--select",
        "invariant_features_have_no_future_leakage",
        "--vars",
        "{macro_release_lag_days: 200}",
    )
    assert failing(statuses) == ["test.praxis.invariant_features_have_no_future_leakage"]


def _corrupt(built: dict[str, Any], tmp_path: Path, sql: str) -> Path:
    import duckdb

    db = tmp_path / "bad.duckdb"
    shutil.copy(built["db"], db)
    con = duckdb.connect(str(db))
    con.execute("SET TimeZone = 'UTC'")
    changed = con.execute(sql).fetchone()
    con.close()
    assert changed is not None and changed[0] >= 1, "negative control modified no rows"
    return db


NEGATIVE_CONTROLS = {
    "duplicate_success": (
        "INSERT INTO raw.sim_events SELECT uuid()::VARCHAR, event_type, source, schema_version, "
        "entity_id, occurred_at, published_at, event_date, trace_id, correlation_id, causation_id,"
        " is_synthetic, payload, batch_id, loaded_at FROM raw.sim_events "
        "WHERE event_type = 'payment.succeeded' ORDER BY event_id LIMIT 1",
        "invariant_invoice_paid_at_most_once",
    ),
    "unknown_event_type": (
        "UPDATE raw.sim_events SET event_type = 'payment.refunded' WHERE event_id = "
        "(SELECT event_id FROM raw.sim_events WHERE event_type = 'churn.observed' "
        "ORDER BY event_id LIMIT 1)",
        "accepted_values_stg_sim_events_event_type",
    ),
    "orphan_customer": (
        "DELETE FROM raw.sim_events WHERE event_type = 'customer.created' AND entity_id = "
        "(SELECT entity_id FROM raw.sim_events WHERE event_type = 'usage.observed' "
        "ORDER BY event_id LIMIT 1)",
        "relationships_fct_usage_daily_customer_id",
    ),
    "impossible_humidity": (
        "UPDATE raw.external_signals SET value = 150 WHERE record_key = (SELECT record_key "
        "FROM raw.external_signals WHERE metric = 'relative_humidity_2m' "
        "ORDER BY record_key LIMIT 1)",
        "invariant_signal_physical_ranges",
    ),
    "future_dated_signal": (
        "UPDATE raw.external_signals SET retrieved_at = observed_at - INTERVAL 1 DAY "
        "WHERE record_key = (SELECT record_key FROM raw.external_signals "
        "ORDER BY record_key LIMIT 1)",
        "invariant_signals_not_future_dated",
    ),
    "amount_mismatch": (
        "UPDATE raw.sim_events SET payload = json_merge_patch(payload, '{\"amount_minor\": 1}') "
        "WHERE event_id = (SELECT event_id FROM raw.sim_events "
        "WHERE event_type = 'payment.attempted' ORDER BY event_id LIMIT 1)",
        "invariant_payment_amount_matches_invoice",
    ),
    "non_synthetic_event": (
        "UPDATE raw.sim_events SET is_synthetic = false WHERE event_id = "
        "(SELECT event_id FROM raw.sim_events ORDER BY event_id LIMIT 1)",
        "accepted_values_raw_sim_events_is_synthetic",
    ),
    "usage_after_churn": (
        "UPDATE raw.sim_events SET occurred_at = occurred_at + INTERVAL 150 DAY, "
        "event_date = event_date + 150 WHERE event_id = (SELECT u.event_id FROM raw.sim_events u "
        "JOIN raw.sim_events c ON c.entity_id = u.entity_id AND c.event_type = 'churn.observed' "
        "WHERE u.event_type = 'usage.observed' ORDER BY u.event_id LIMIT 1)",
        "invariant_no_usage_after_churn",
    ),
}


@pytest.mark.parametrize("name", sorted(NEGATIVE_CONTROLS))
def test_dbt_tests_catch_corrupted_data(built: dict[str, Any], tmp_path: Path, name: str) -> None:
    sql, expected = NEGATIVE_CONTROLS[name]
    db = _corrupt(built, tmp_path, sql)
    statuses = run_dbt(db, tmp_path / "d", "build")
    assert any(expected in test_id for test_id in failing(statuses)), (name, failing(statuses))


def test_source_freshness_passes_on_fresh_loads_and_fails_when_stale(
    built: dict[str, Any], tmp_path: Path
) -> None:
    fresh = run_dbt(built["db"], tmp_path / "f", "source", "freshness")
    assert set(fresh.values()) == {"pass"}, fresh
    db = _corrupt(
        built,
        tmp_path,
        "UPDATE raw.sim_events SET loaded_at = loaded_at - INTERVAL 40 DAY; "
        "UPDATE raw.ingest_batches SET retrieved_at = retrieved_at - INTERVAL 40 DAY",
    )
    stale = run_dbt(db, tmp_path / "s", "source", "freshness")
    assert failing(stale), stale
