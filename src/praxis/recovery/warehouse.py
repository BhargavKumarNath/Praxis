"""Read payment histories from the dbt marts (DuckDB warehouse).

Reads only ``marts.dim_customer`` (attributes at signup, never the current tier or churn),
``marts.fct_invoices`` and ``marts.fct_payments``. Nothing here knows the simulator: invoices
and attempts come from logged events, as they would from a real billing system.

``as_of`` makes the extract a point-in-time view: invoices created and attempts resolved at
or after it do not exist yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from praxis.recovery.features import Attempt, CustomerHistory, InvoiceRecord

_CUSTOMERS_SQL = """
SELECT customer_id, preferred_payment_method, is_existing, tenure_days_at_start, created_at
FROM marts.dim_customer
"""
_INVOICES_SQL = """
SELECT invoice_id, customer_id, invoiced_at, amount_minor, tier
FROM marts.fct_invoices
WHERE invoiced_at < ?
"""
_ATTEMPTS_SQL = """
SELECT invoice_id, attempt_number, outcome, failure_reason, resolved_at
FROM marts.fct_payments
WHERE outcome IN ('succeeded', 'failed') AND resolved_at < ?
"""


class WarehouseUnavailable(RuntimeError):
    """The warehouse file or the marts it needs are missing."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def load_histories(db: Path, as_of: datetime) -> dict[str, CustomerHistory]:
    """Every customer's invoices and resolved attempts known strictly before ``as_of``."""
    if not db.exists():
        raise WarehouseUnavailable(f"warehouse {db} does not exist")
    cutoff = _utc(as_of)
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            con.execute("SET TimeZone = 'UTC'")
            customers = con.execute(_CUSTOMERS_SQL).fetchall()
            invoices = con.execute(_INVOICES_SQL, [cutoff]).fetchall()
            attempts = con.execute(_ATTEMPTS_SQL, [cutoff]).fetchall()
    except duckdb.CatalogException as exc:
        raise WarehouseUnavailable(f"recovery marts missing: {exc}") from exc

    by_invoice: dict[str, list[Attempt]] = defaultdict(list)
    for invoice_id, number, outcome, reason, resolved_at in attempts:
        by_invoice[invoice_id].append(
            Attempt(int(number), _utc(resolved_at), outcome == "succeeded", reason)
        )
    by_customer: dict[str, list[InvoiceRecord]] = defaultdict(list)
    for invoice_id, customer_id, invoiced_at, amount, tier in invoices:
        by_customer[customer_id].append(
            InvoiceRecord(
                invoice_id,
                _utc(invoiced_at),
                int(amount),
                tier,
                tuple(sorted(by_invoice.get(invoice_id, ()), key=lambda a: a.number)),
            )
        )
    out: dict[str, CustomerHistory] = {}
    for customer_id, method, is_existing, tenure, created_at in customers:
        if _utc(created_at) >= cutoff:
            continue
        out[customer_id] = CustomerHistory(
            customer_id=customer_id,
            payment_method=method,
            is_existing=bool(is_existing),
            tenure_days_at_start=int(tenure),
            created_at=_utc(created_at),
            invoices=tuple(
                sorted(by_customer.get(customer_id, ()), key=lambda i: (i.created_at, i.invoice_id))
            ),
        )
    return out
