"""Recovery episodes: what the models learn from.

An *episode* is an invoice whose first attempt failed. Time zero is that failure; every later
attempt is a *retry* observed at ``elapsed`` days. Collectibility is absorbing (a payment that
can be collected stays collectible), so the retries of one episode reveal an interval for
the latent time C until the payment became collectible:

* a retry succeeded at e_j after failures at ... < e_{j-1}:  C in (e_{j-1}, e_j]
  (e_0 = 0: the first attempt failed, so C > 0)
* every observed retry failed, the last at e_m:              C > e_m  (right-censored)

Retries not yet resolved at the extract's ``as_of`` simply do not exist, which is how the
training cutoff right-censors episodes without any special case.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from praxis.recovery.features import (
    CustomerHistory,
    InvoiceRecord,
    RecoveryFeatures,
    episode_features,
)

DAY_S = 86_400.0


@dataclass(frozen=True, slots=True)
class Retry:
    elapsed_days: float
    succeeded: bool


@dataclass(frozen=True, slots=True)
class Episode:
    invoice_id: str
    customer_id: str
    failed_at: datetime
    amount_minor: int
    features: RecoveryFeatures
    retries: tuple[Retry, ...]  # in attempt order; nothing after the first success

    @property
    def recovered(self) -> bool:
        return any(r.succeeded for r in self.retries)

    @property
    def interval(self) -> tuple[float, float]:
        """(L, R] bracketing the latent collectible time; R = inf when censored."""
        lower = 0.0
        for retry in self.retries:
            if retry.succeeded:
                return lower, retry.elapsed_days
            lower = retry.elapsed_days
        return lower, math.inf

    @property
    def first_retry(self) -> Retry | None:
        return self.retries[0] if self.retries else None


def _elapsed(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / DAY_S, 6)


def _episode(history: CustomerHistory, invoice: InvoiceRecord) -> Episode | None:
    ordered = invoice.ordered()
    if not ordered or ordered[0].number != 1 or ordered[0].succeeded:
        return None
    first = ordered[0]
    retries: list[Retry] = []
    for attempt in ordered[1:]:
        retries.append(Retry(_elapsed(first.resolved_at, attempt.resolved_at), attempt.succeeded))
        if attempt.succeeded:
            break
    return Episode(
        invoice_id=invoice.invoice_id,
        customer_id=history.customer_id,
        failed_at=first.resolved_at,
        amount_minor=invoice.amount_minor,
        features=episode_features(history, invoice.invoice_id),
        retries=tuple(retries),
    )


def build_episodes(histories: Mapping[str, CustomerHistory]) -> list[Episode]:
    """Every episode in the histories, ordered by failure time then invoice id."""
    out: list[Episode] = []
    for history in histories.values():
        for invoice in history.invoices:
            episode = _episode(history, invoice)
            if episode is not None:
                out.append(episode)
    out.sort(key=lambda e: (e.failed_at, e.invoice_id))
    return out


def failed_between(episodes: Iterable[Episode], start: datetime, end: datetime) -> list[Episode]:
    return [e for e in episodes if start <= e.failed_at < end]
