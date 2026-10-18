"""CLI: register -> validate -> promote -> run-day -> drills -> status / history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from praxis.mlops.__main__ import main
from praxis.mlops.training import Candidate
from tests.mlops import helpers as h
from tests.mlops.conftest import World


@pytest.fixture(autouse=True)
def _quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI prints JSON to stdout, where logs also go: keep logs at WARNING."""
    from praxis.config import get_settings

    monkeypatch.setenv("PRAXIS_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()


def cli(capsys: pytest.CaptureFixture[str], base: list[str], *args: str) -> tuple[int, Any]:
    code = main([*base, *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_cli_lifecycle(
    pg_url: str,
    world: World,
    champion: Candidate,
    bad: Candidate,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models = tmp_path / "models"
    base = [
        "--database-url",
        pg_url,
        "--warehouse",
        str(world.clean),
        "--models",
        str(models),
        "--environment",
        "dev",
    ]
    backtest = tmp_path / "backtest.json"
    backtest.write_text(json.dumps(champion.backtest))
    assert cli(capsys, base, "migrate") == (0, {"migrated": True})
    code, out = cli(
        capsys, base, "register", "--artifact", str(champion.path), "--backtest", str(backtest)
    )
    version = champion.record.model_version
    assert code == 0 and out == {"model_version": version, "stage": "candidate"}
    # validation applies the PINNED forecast acceptance (configs/forecast/demand.toml)
    code, out = cli(
        capsys,
        base,
        "--report",
        str(tmp_path / "val.json"),
        "validate",
        "--version",
        version,
        "--as-of",
        world.date(112).isoformat(),
    )
    assert code == 0 and out["stage"] == "challenger", out["failed"]
    assert (tmp_path / "val.json").is_file()
    code, out = cli(
        capsys, base, "promote", "--version", version, "--as-of", world.date(112).isoformat()
    )
    assert code == 1 and "named approver" in out["refused"]
    code, out = cli(
        capsys,
        base,
        "--actor",
        "owner",
        "promote",
        "--version",
        version,
        "--as-of",
        world.date(112).isoformat(),
    )
    assert code == 0 and out["moves"] == [[version, "challenger", "champion"]]
    code, out = cli(capsys, base, "--actor", "oncall", "rollback")
    assert code == 2 and "no rollback target" in out["error"]

    code, out = cli(capsys, base, "register", "--artifact", str(bad.path))
    assert code == 1 and "incomplete lineage" in out["refused"]  # no metrics without a backtest
    bad_backtest = tmp_path / "bad_backtest.json"
    bad_backtest.write_text(json.dumps(bad.backtest))
    code, out = cli(
        capsys, base, "register", "--artifact", str(bad.path), "--backtest", str(bad_backtest)
    )
    assert code == 0 and out["stage"] == "candidate"
    code, out = cli(
        capsys,
        base,
        "--actor",
        "drill",
        "force-promote",
        "--version",
        bad.record.model_version,
        "--as-of",
        world.date(140).isoformat(),
    )
    assert code == 0 and out["moves"][-1][2] == "champion"
    code, out = cli(capsys, base, "run-day", "--as-of", world.date(141).isoformat())
    assert code == 0 and out["champion"] == bad.record.model_version
    code, out = cli(
        capsys,
        base,
        "operate",
        "--start",
        world.date(142).isoformat(),
        "--end",
        world.date(143).isoformat(),
    )
    assert code == 0 and len(out["days"]) == 2
    code, out = cli(
        capsys, base, "--actor", "oncall", "rollback", "--as-of", world.date(144).isoformat()
    )
    assert code == 0 and [m[2] for m in out["moves"]] == ["rolled_back", "champion"]
    code, out = cli(capsys, base, "request-retraining", "--as-of", world.date(150).isoformat())
    assert code == 0 and out["status"] in {"open", "training", "completed"}
    code, out = cli(capsys, base, "status")
    assert code == 0 and out["champion"] == [version]
    assert out["rolled_back"] == [bad.record.model_version]
    assert out["champion_lineage"]["artifact_loads"] is True
    code, out = cli(capsys, base, "history")
    assert code == 0 and [e["trigger"] for e in out][:3] == [
        "registered",
        "validation.passed",
        "promote.bootstrap",
    ]
    assert cli(capsys, base, "sync-tracking") == (0, {"synced": True})
    code, out = cli(capsys, base, "--actor", "owner", "reject", "--version", "nope")
    assert code == 2 and "unknown" in out["error"]
    code, out = cli(
        capsys,
        base,
        "--actor",
        "owner",
        "start-shadow",
        "--version",
        version,
        "--as-of",
        world.date(150).isoformat(),
    )
    assert code == 2


def test_cli_retrain_without_request(
    pg_url: str, world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--database-url", pg_url, "--warehouse", str(world.clean), "--models", str(tmp_path)]
    code, out = cli(capsys, base, "retrain", "--as-of", world.date(140).isoformat())
    assert code == 0 and out == {"retrained": False, "reason": "no open retraining request"}
    assert h.N_SERIES == 12
