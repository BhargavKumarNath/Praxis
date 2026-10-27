"""The panel envelope every console view returns.

A panel is one independently readable block of a view. It always says where its numbers came
from (``source``), whether they are simulator output (``synthetic``), and its state:

* ``ok``: ``data`` holds real values read now;
* ``no_data``: the store answered but holds nothing for this panel; ``reason`` says why;
* ``unavailable``: the store did not answer; ``reason`` is a fixed sentence (never the
  exception text, which may carry internals) and ``error`` the exception class.

One failing store marks only its own panels, so a view never collapses into one error, and a
missing value is never reported as 0.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb
from sqlalchemy import exc

from praxis.ops.collect import STORE_ERRORS

Data = dict[str, Any]

SYNTHETIC_NOTE = (
    "All revenue, customer, payment and recovery figures are SYNTHETIC simulator output, not "
    "real business results."
)
SHADOW_NOTE = (
    "shadow: not validated for execution (Phase 4 loader defect; Phases 6 and 9 need a fresh "
    "pre-registered evaluation before live pricing)"
)


class NotConfigured(RuntimeError):
    """A store this panel needs is not configured for this console process. The message is
    ours (never driver text), so it is shown to the operator as the reason."""


def _unavailable_reason(error: BaseException) -> str:
    if isinstance(error, duckdb.CatalogException):
        return "a warehouse table this panel needs is not built (run `make dbt-build`)"
    if isinstance(error, duckdb.Error):
        return "the warehouse could not be opened read-only (missing, or locked by a writer)"
    if isinstance(error, exc.SQLAlchemyError):
        return "the control-plane database did not answer"
    return "the store behind this panel did not answer"


def panel(
    source: str,
    read: Callable[[], Data | None],
    *,
    empty: str,
    synthetic: bool = True,
) -> Data:
    """Run ``read`` and wrap the result. ``read`` returns ``None`` (or an empty dict) when the
    store holds nothing; ``empty`` is then the reason shown to the operator."""
    try:
        data = read()
    except NotConfigured as error:
        return {
            "status": "unavailable",
            "source": source,
            "synthetic": synthetic,
            "reason": f"not configured: {error}",
            "error": "NotConfigured",
            "data": None,
        }
    except STORE_ERRORS as error:
        return {
            "status": "unavailable",
            "source": source,
            "synthetic": synthetic,
            "reason": _unavailable_reason(error),
            "error": type(error).__name__,
            "data": None,
        }
    if not data:
        return {
            "status": "no_data",
            "source": source,
            "synthetic": synthetic,
            "reason": empty,
            "data": None,
        }
    return {"status": "ok", "source": source, "synthetic": synthetic, "reason": None, "data": data}


def figure(
    value: float | None,
    *,
    unit: str,
    reason: str | None = None,
    baseline: float | None = None,
    baseline_label: str | None = None,
    series: list[Data] | None = None,
) -> Data:
    """One readout value. ``value=None`` requires a ``reason`` (never a silent blank)."""
    if value is None and not reason:
        raise ValueError("a missing value needs a reason")
    return {
        "value": value,
        "unit": unit,
        "reason": None if value is not None else reason,
        "baseline": baseline,
        "baseline_label": baseline_label if baseline is not None else None,
        "series": series or [],
    }


def jsonable(value: Any) -> Any:
    """Database values to JSON: datetimes and dates as ISO text, Decimal as int or float."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def rows(result: Any) -> list[Data]:
    """SQLAlchemy result rows as JSON-ready dicts."""
    return [jsonable(dict(r._mapping)) for r in result]
