"""Phase 14 W4 (ADR 0022): synthetic-gateway payment load keeps money exact under redelivery."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine

from praxis.payments.__main__ import main
from praxis.payments.load import PaymentLoad, run_payment_load


def test_payment_load_is_exact_under_duplicate_notifications(pg_engine: Engine) -> None:
    report = run_payment_load(
        pg_engine, PaymentLoad(customers=40, seed=701, fail_share=0.25, settle_every=15)
    )
    assert report["correct"] is True
    assert report["invoices_projected"] == 40
    assert 0 < report["customers_failing"] < 40
    assert (
        report["ledger_entries"]
        == report["gateway_paid_invoices"]
        == 40 - report["customers_failing"]
    )
    assert report["ledger_total_minor"] == report["gateway_paid_total_minor"] > 0
    assert report["inbox_rows_new"] == report["notifications"] > 0
    assert report["inbox_rows_from_redelivery"] == 0
    assert report["enrol_latency"]["count"] == 40 and report["is_synthetic"] is True


def test_load_cli_writes_its_report(pg_url: str, tmp_path: Path) -> None:
    out = tmp_path / "pay.json"
    args = ["--database-url", pg_url, "load", "--customers", "12", "--report", str(out)]
    assert main(args) == 0
    assert json.loads(out.read_text())["correct"] is True
