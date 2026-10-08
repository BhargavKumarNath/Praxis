"""Synthetic recovery episodes from a KNOWN cure process (test-only ground truth).

Each episode: features drawn at random, latent cure ~ Bernoulli(q[reason]) and cure time ~
Weibull(k[reason], s[reason]); retries at randomised gaps (the Phase 8 design), the first at
or after the cure time succeeds. Optional ``cutoff`` drops retries after it (right-censoring).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from praxis.recovery.dataset import Episode, Retry
from praxis.recovery.features import METHODS, REASONS, TIERS, RecoveryFeatures

T0 = datetime(2026, 3, 1, tzinfo=UTC)
GAPS = (1, 2, 3, 4, 5, 7, 10)
TRUE = {  # reason -> (cure probability, Weibull shape, Weibull scale in days)
    "insufficient_funds": (0.8, 2.2, 7.0),
    "card_declined": (0.4, 0.7, 2.5),
    "expired_card": (0.6, 1.2, 9.0),
    "processor_error": (0.95, 0.8, 0.5),
    "authentication_required": (0.5, 1.0, 4.0),
}


def features(rng: np.random.Generator, reason: str | None = None) -> RecoveryFeatures:
    n_prior = int(rng.integers(0, 8))
    fails = int(rng.integers(0, n_prior + 1))
    return RecoveryFeatures(
        reason=reason or str(rng.choice(REASONS)),
        tier=str(rng.choice(TIERS)),
        payment_method=str(rng.choice(METHODS)),
        is_existing=bool(rng.random() < 0.8),
        tenure_days=int(rng.integers(0, 900)),
        amount_minor=int(rng.choice([4900, 19900, 99900])) + int(rng.integers(0, 5000)),
        prior_invoices=n_prior,
        prior_failures=fails,
        prior_recoveries=int(rng.integers(0, fails + 1)),
    )


def true_cdf(reason: str, t: float) -> float:
    q, k, s = TRUE[reason]
    return 0.0 if t <= 0 else q * (1.0 - math.exp(-((t / s) ** k)))


def episodes(
    n: int,
    seed: int = 0,
    *,
    max_attempts: int = 3,
    cutoff_days: float | None = None,
    reasons: Sequence[str] | None = None,
) -> list[Episode]:
    rng = np.random.default_rng(seed)
    out: list[Episode] = []
    for i in range(n):
        f = features(rng, str(rng.choice(reasons)) if reasons else None)
        q, k, s = TRUE[f.reason]
        cure = s * (-math.log1p(-rng.random())) ** (1.0 / k) if rng.random() < q else math.inf
        failed_at = T0 + timedelta(days=i * 0.05)
        elapsed, retries = 0.0, []
        for _ in range(max_attempts - 1):
            elapsed += float(rng.choice(GAPS))
            if cutoff_days is not None and i * 0.05 + elapsed >= cutoff_days:
                break
            ok = elapsed >= cure
            retries.append(Retry(elapsed, ok))
            if ok:
                break
        out.append(
            Episode(f"inv_{i:06d}", f"cust_{i:06d}", failed_at, f.amount_minor, f, tuple(retries))
        )
    return out
