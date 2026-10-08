"""Recovery features: definitions, encoding, and no future leakage (property-tested)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.recovery.features import (
    Attempt,
    CustomerHistory,
    InvoiceRecord,
    RecoveryFeatures,
    UnknownCategory,
    column_names,
    encode,
    episode_features,
)

T0 = datetime(2026, 1, 5, tzinfo=UTC)


def inv(
    i: int, day: int, attempts: tuple[tuple[int, int, bool, str | None], ...], tier: str = "growth"
) -> InvoiceRecord:
    """attempts: (number, day resolved, succeeded, reason)."""
    return InvoiceRecord(
        f"inv_{i}",
        T0 + timedelta(days=day, hours=1),
        5000 + i,
        tier,
        tuple(
            Attempt(n, T0 + timedelta(days=d, hours=1, minutes=2), ok, r)
            for n, d, ok, r in attempts
        ),
    )


def history(*invoices: InvoiceRecord) -> CustomerHistory:
    return CustomerHistory("cust_1", "card", True, 100, T0, tuple(invoices))


def test_counts_prior_invoices_failures_and_recoveries() -> None:
    h = history(
        inv(1, 0, ((1, 0, True, None),)),
        inv(2, 30, ((1, 30, False, "card_declined"), (2, 33, True, None))),
        inv(3, 60, ((1, 60, False, "expired_card"), (2, 63, False, "expired_card"))),
        inv(4, 90, ((1, 90, False, "insufficient_funds"),), tier="starter"),
    )
    f = episode_features(h, "inv_4")
    assert f == RecoveryFeatures(
        reason="insufficient_funds",
        tier="starter",
        payment_method="card",
        is_existing=True,
        tenure_days=190,
        amount_minor=5004,
        prior_invoices=3,
        prior_failures=2,
        prior_recoveries=1,
    )


def test_episode_needs_a_failed_first_attempt() -> None:
    h = history(inv(1, 0, ((1, 0, True, None),)), inv(2, 30, ()))
    for invoice_id in ("inv_1", "inv_2"):
        with pytest.raises(ValueError, match="no failed first attempt"):
            episode_features(h, invoice_id)
    with pytest.raises(KeyError):
        episode_features(h, "inv_9")


def test_outcomes_resolved_after_the_failure_are_invisible() -> None:
    """A prior invoice recovered only AFTER this failure does not count as a recovery yet."""
    prior = inv(1, 0, ((1, 0, False, "card_declined"), (2, 25, True, None)))
    current = inv(2, 20, ((1, 20, False, "card_declined"),))
    f = episode_features(history(prior, current), "inv_2")
    assert (f.prior_failures, f.prior_recoveries) == (1, 0)


_days = st.integers(min_value=0, max_value=200)


@settings(max_examples=60, deadline=None)
@given(
    later_attempt_day=st.integers(min_value=41, max_value=200),
    later_invoice_day=st.integers(min_value=41, max_value=200),
    ok=st.booleans(),
)
def test_future_facts_never_change_features(
    later_attempt_day: int, later_invoice_day: int, ok: bool
) -> None:
    """Anything resolved after the failure (day 40) leaves the features unchanged."""
    base = history(
        inv(1, 0, ((1, 0, False, "card_declined"),)),
        inv(2, 40, ((1, 40, False, "insufficient_funds"),)),
    )
    before = episode_features(base, "inv_2")
    first = base.invoices[0]
    late = Attempt(2, T0 + timedelta(days=later_attempt_day), ok)
    extended = replace(
        base,
        invoices=(
            replace(first, attempts=(*first.attempts, late)),
            base.invoices[1],
            inv(3, later_invoice_day, ((1, later_invoice_day, False, "expired_card"),)),
        ),
    )
    assert episode_features(extended, "inv_2") == before


def test_encoding_layout_and_unknown_categories() -> None:
    f = episode_features(history(inv(1, 0, ((1, 0, False, "processor_error"),))), "inv_1")
    x = encode([f])
    names = column_names()
    assert x.shape == (1, len(names))
    assert x[0, names.index("reason=processor_error")] == 1.0
    assert x[0, names.index("tier=growth")] == 1.0
    assert x[0, names.index("method=card")] == 1.0
    for bad in (
        replace(f, reason="lost_card"),
        replace(f, tier="gold"),
        replace(f, payment_method="cash"),
    ):
        with pytest.raises(UnknownCategory):
            encode([bad])
