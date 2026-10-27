"""The panel envelope (unit): missing values need a reason, store errors become fixed
sentences, database values become JSON."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from praxis.console.panels import figure, jsonable, panel


def test_a_missing_value_must_say_why() -> None:
    with pytest.raises(ValueError, match="needs a reason"):
        figure(None, unit="ms")
    assert figure(None, unit="ms", reason="no feed")["reason"] == "no feed"
    assert figure(0.0, unit="ms", reason="ignored")["reason"] is None  # a real zero is a value


def test_unknown_store_errors_get_a_generic_sentence() -> None:
    def fail() -> None:
        raise OSError("/secret/path is gone")

    out = panel("disk", fail, empty="")
    assert out["status"] == "unavailable" and out["error"] == "OSError"
    assert out["reason"] == "the store behind this panel did not answer"
    assert "/secret/path" not in str(out)


def test_jsonable_handles_database_types() -> None:
    value = {"d": date(2026, 1, 2), "t": datetime(2026, 1, 2, tzinfo=UTC),
             "i": Decimal("3"), "f": Decimal("1.5"), "l": (Decimal("2"),)}  # fmt: skip
    assert jsonable(value) == {"d": "2026-01-02", "t": "2026-01-02T00:00:00+00:00",
                               "i": 3, "f": 1.5, "l": [2]}  # fmt: skip
