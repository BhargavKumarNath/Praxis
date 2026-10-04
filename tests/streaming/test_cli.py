"""CLI (`python -m praxis.streaming`) without the emulator; emulator commands are in
tests/integration/test_pubsub_emulator.py."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from praxis.streaming.__main__ import main
from tests.streaming.helpers import command_output


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBSUB_EMULATOR_HOST", raising=False)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_migrate_and_dlq(empty_pg_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--database-url", empty_pg_url, "migrate"]) == 0
    assert "migrated" in capsys.readouterr().out
    assert main(["--database-url", empty_pg_url, "dlq"]) == 0
    assert command_output(capsys.readouterr().out) == {"open_dead_letters": {}}


def test_run_local_matches_oracle_and_writes_report(
    pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "out" / "report.json"
    code = main(
        [
            "--database-url", pg_url, "run-local", "--customers", "40", "--days", "21",
            "--crash-rate", "0.02", "--duplicate-rate", "0.3", "--chunk", "500",
            "--archive", str(tmp_path / "archive"), "--report", str(report_path),
        ]
    )  # fmt: skip
    assert code == 0
    report = json.loads(report_path.read_text())
    assert report["matches_oracle"] is True and report["is_synthetic"] is True
    assert report["dead_letters_after_redrive"] == {}
    assert report["subscription_inconsistencies"] == 0
    assert report["warehouse_rows"] == report["events_published"]
    assert any((tmp_path / "archive").rglob("part-*.ndjson"))
    out = command_output(capsys.readouterr().out)
    assert out["control_plane_checksum"] == report["control_plane_checksum"]


@pytest.mark.parametrize(
    "argv",
    [
        ["emulator-setup"],
        ["produce"],
        ["consume", "--role", "operational"],
        ["replay", "--archive", "x"],
        ["emulator-bench"],
    ],
)
def test_pubsub_commands_require_the_emulator(argv: list[str], pg_url: str) -> None:
    with pytest.raises(SystemExit, match="PUBSUB_EMULATOR_HOST"):
        main(["--database-url", pg_url, *argv])


def test_redrive_requires_the_emulator(pg_url: str) -> None:
    with pytest.raises(SystemExit, match="PUBSUB_EMULATOR_HOST"):
        main(["--database-url", pg_url, "dlq", "--redrive"])


def test_missing_database_url_is_explicit() -> None:
    with pytest.raises(SystemExit, match="PRAXIS_DATABASE_URL"):
        main(["migrate"])
