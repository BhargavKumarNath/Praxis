"""Pricing CLI paths not covered by the end-to-end pipeline (fast; Postgres for the store)."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from praxis.pricing.__main__ import main
from praxis.pricing.config import Mode
from praxis.pricing.store import PostgresDecisionStore
from tests.pricing import market_db
from tests.pricing.test_store import change

pytestmark = pytest.mark.integration


def test_entry_point_parses_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["praxis.pricing", "--help"])
    monkeypatch.delitem(sys.modules, "praxis.pricing.__main__", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("praxis.pricing", run_name="__main__")
    assert exc.value.code == 0


def test_unusable_evidence_still_records_a_decision_per_product(tmp_path: Path) -> None:
    """A missing analysis report: every product is decided UNAVAILABLE (model_unavailable)."""
    db = market_db.build(tmp_path / "wh.duckdb")
    out = tmp_path / "cycle.json"
    code = main(
        [
            "decide",
            "--db", str(db),
            "--forecast-model", str(tmp_path / "no-forecast"),
            "--elasticity-model", str(tmp_path / "no-models"),
            "--elasticity-report", str(tmp_path / "missing.json"),
            "--as-of", "2026-07-20",
            "--out", str(out),
        ]
    )  # fmt: skip
    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["audit_complete"] and len(payload["decisions"]) == 5
    assert {d["status"] for d in payload["decisions"]} == {"unavailable"}
    assert all(d["reason_codes"] == ["model_unavailable"] for d in payload["decisions"])
    assert all("EvidenceError" in r["errors"][0] for r in payload["records"])


def test_execute_and_approve_by_decision_id(
    pg_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = PostgresDecisionStore(create_engine(pg_url))
    executable = change(Mode.EXECUTE)
    store.record(executable)
    args = ["--database-url", pg_url, "--decision-id", executable.decision_id]
    assert main(["approve", *args, "--approver", "pricing-lead"]) == 2  # not a recommendation
    assert main(["execute", *args]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["execution"]["to_price_micros"] == executable.chosen_price_micros
    assert printed["execution"]["executed_by"] == "pricing-cli"
    assert main(["execute", "--database-url", pg_url, "--decision-id", "dec-none"]) == 2


def test_a_recommendation_is_approved_then_executed(pg_url: str) -> None:
    store = PostgresDecisionStore(create_engine(pg_url))
    recommended = change(Mode.RECOMMEND)
    store.record(recommended)
    args = ["--database-url", pg_url, "--decision-id", recommended.decision_id]
    assert main(["execute", *args]) == 2
    assert main(["approve", *args, "--approver", "pricing-lead"]) == 0
    assert main(["execute", *args]) == 0
    assert main(["show", *args]) == 0
