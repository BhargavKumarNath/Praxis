from __future__ import annotations

import json

import pytest

from praxis.control.db import make_engine
from praxis.ops.__main__ import main
from praxis.ops.incidents import PostgresIncidentStore


def test_validate_passes_on_the_repository_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []


def test_synthetic_alert_prints_a_labelled_incident(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["synthetic-alert", "--category", "data"]) == 0
    out = capsys.readouterr().out  # structured log line(s), then the incident JSON array
    (incident,) = json.loads(out[out.index("\n[") + 1 :])
    assert incident["synthetic"] and incident["runbook"].endswith("external-source-outage.md")


def test_synthetic_alert_persists_when_asked(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRAXIS_DATABASE_URL", pg_url)
    assert main(["synthetic-alert", "--category", "model", "--persist"]) == 0
    (stored,) = PostgresIncidentStore(make_engine(pg_url)).live()
    assert stored.synthetic and stored.category == "model"
    capsys.readouterr()


def test_persist_needs_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["synthetic-alert", "--category", "model", "--persist"]) == 2
    assert "PRAXIS_DATABASE_URL" in capsys.readouterr().err
