"""Recovery features (``recovery_features.v1``): one pure function for training and serving.

Training reads payment histories from the warehouse marts; the dunning service reads them
from the control plane. Both build the same ``CustomerHistory`` and call ``episode_features``,
so the feature logic exists once (no training / serving skew by construction; the two
extractors are tested for parity).

Leakage rule: features of an episode use only facts *resolved strictly before* the episode's
first failure (``as_of``). A prior invoice's later outcome, the customer's later tier or churn,
and anything after ``as_of`` cannot change the features (property-tested).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import get_args

import numpy as np
from numpy.typing import NDArray

from praxis.events.payloads import FailureReason, PaymentMethod, Tier

FEATURE_VERSION = "recovery_features.v1"
REASONS: tuple[str, ...] = get_args(FailureReason)
TIERS: tuple[str, ...] = get_args(Tier)
METHODS: tuple[str, ...] = get_args(PaymentMethod)
NUMERIC = (
    "is_existing",
    "log_tenure",
    "log_amount",
    "log_prior_invoices",
    "prior_failure_rate",
    "prior_recovery_rate",
)

F64 = NDArray[np.float64]


class UnknownCategory(ValueError):
    """A categorical value the model was not trained on: the policy falls back to baseline."""


@dataclass(frozen=True, slots=True)
class Attempt:
    number: int
    resolved_at: datetime
    succeeded: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    invoice_id: str
    created_at: datetime
    amount_minor: int
    tier: str
    attempts: tuple[Attempt, ...]  # resolved attempts, any order

    def ordered(self) -> tuple[Attempt, ...]:
        return tuple(sorted(self.attempts, key=lambda a: a.number))


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    customer_id: str
    payment_method: str
    is_existing: bool
    tenure_days_at_start: int
    created_at: datetime
    invoices: tuple[InvoiceRecord, ...]


@dataclass(frozen=True, slots=True)
class RecoveryFeatures:
    reason: str
    tier: str
    payment_method: str
    is_existing: bool
    tenure_days: int
    amount_minor: int
    prior_invoices: int
    prior_failures: int
    prior_recoveries: int

    def numeric(self) -> tuple[float, ...]:
        return (
            float(self.is_existing),
            math.log1p(max(0, self.tenure_days)),
            math.log(max(1, self.amount_minor)),
            math.log1p(self.prior_invoices),
            (self.prior_failures + 0.1) / (self.prior_invoices + 1.0),
            (self.prior_recoveries + 0.5) / (self.prior_failures + 1.0),
        )


def _first_failure(invoice: InvoiceRecord) -> Attempt | None:
    ordered = invoice.ordered()
    if not ordered or ordered[0].number != 1 or ordered[0].succeeded:
        return None
    return ordered[0]


def episode_features(history: CustomerHistory, invoice_id: str) -> RecoveryFeatures:
    """Features of the episode opened by ``invoice_id``'s first failed attempt."""
    invoice = next((i for i in history.invoices if i.invoice_id == invoice_id), None)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not in the customer's history")
    first = _first_failure(invoice)
    if first is None or first.reason is None:
        raise ValueError(f"invoice {invoice_id} has no failed first attempt with a reason")
    as_of = first.resolved_at
    prior_invoices = prior_failures = prior_recoveries = 0
    for other in history.invoices:
        if other.invoice_id == invoice_id or other.created_at >= invoice.created_at:
            continue
        known = [a for a in other.attempts if a.resolved_at < as_of]
        prior_invoices += 1
        first_known = next((a for a in known if a.number == 1), None)
        if first_known is not None and not first_known.succeeded:
            prior_failures += 1
            prior_recoveries += any(a.succeeded for a in known if a.number > 1)
    tenure = history.tenure_days_at_start + max(0, (as_of - history.created_at).days)
    return RecoveryFeatures(
        reason=first.reason,
        tier=invoice.tier,
        payment_method=history.payment_method,
        is_existing=history.is_existing,
        tenure_days=tenure,
        amount_minor=invoice.amount_minor,
        prior_invoices=prior_invoices,
        prior_failures=prior_failures,
        prior_recoveries=prior_recoveries,
    )


def _one_hot(value: str, levels: tuple[str, ...], what: str) -> list[float]:
    if value not in levels:
        raise UnknownCategory(f"unknown {what} {value!r}")
    return [1.0 if value == level else 0.0 for level in levels]


def column_names() -> list[str]:
    return [
        *(f"reason={r}" for r in REASONS),
        *(f"tier={t}" for t in TIERS),
        *(f"method={m}" for m in METHODS),
        *NUMERIC,
    ]


def encode(rows: Sequence[RecoveryFeatures]) -> F64:
    """Full one-hot design (no intercept): reasons, tiers, methods, then numeric columns."""
    out = np.empty((len(rows), len(column_names())), dtype=np.float64)
    for k, f in enumerate(rows):
        out[k] = [
            *_one_hot(f.reason, REASONS, "reason"),
            *_one_hot(f.tier, TIERS, "tier"),
            *_one_hot(f.payment_method, METHODS, "payment method"),
            *f.numeric(),
        ]
    return out
