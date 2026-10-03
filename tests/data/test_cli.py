from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis.config import get_settings
from praxis.data.__main__ import main
from praxis.data.ingest import IngestService, Outcome
from praxis.data.models import SourceId
from praxis.data.raw_store import LocalRawStore
from praxis.data.warehouse import Warehouse
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.data.helpers import WINDOW, fixture_handler, make_fetcher, registry


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env is picked up from the repo
    monkeypatch.delenv("PRAXIS_FRED_API_KEY", raising=False)
    monkeypatch.delenv("PRAXIS_EIA_API_KEY", raising=False)
    get_settings.cache_clear()


def _args(tmp_path: Path, *rest: str) -> list[str]:
    return ["--db", str(tmp_path / "w.duckdb"), "--raw", str(tmp_path / "raw"), *rest]


def test_migrate_then_freshness_reports_missing_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_args(tmp_path, "migrate")) == 0
    assert main(_args(tmp_path, "freshness")) == 1
    states = {json.loads(line)["state"] for line in capsys.readouterr().out.splitlines()}
    assert states == {"missing"}


def test_load_sim_is_idempotent_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sim = tmp_path / "sim"
    run_simulation(load_config().with_overrides(n_customers=20, days=3), 7, out_dir=sim)
    assert main(_args(tmp_path, "load-sim", "--dir", str(sim))) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert main(_args(tmp_path, "load-sim", "--dir", str(sim))) == 0
    second = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first["new_events"] > 0 and second["new_events"] == 0


def test_ingest_without_keys_is_safe_and_makes_no_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_args(tmp_path, "ingest", "--source", "fred", "--start", "2026-01-01"))
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == 0 and summary["missing_credential"] == 1 and summary["new_records"] == 0
    strict = main(_args(tmp_path, "ingest", "--source", "eia", "--strict", "--start", "2026-01-01"))
    assert strict == 1  # --strict turns a skipped source into a failure


def test_replay_via_cli_rebuilds_from_archive_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = LocalRawStore(tmp_path / "raw")
    with Warehouse() as wh:
        wh.migrate()
        svc = IngestService(registry(), make_fetcher(fixture_handler()), raw, wh)
        assert svc.ingest(SourceId.OPEN_METEO, WINDOW).all_succeeded
    assert main(_args(tmp_path, "replay")) == 0
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["new_records"] > 0 and summary[Outcome.QUARANTINED.value] == 0
