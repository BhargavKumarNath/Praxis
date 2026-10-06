"""Deterministic experiment assignment (pure; no I/O).

A unit's arm is a function of ``(salt, unit_id, treated_fraction)`` only: a 64-bit
BLAKE2b hash of ``"{salt}:{unit_id}"`` mapped to a uniform in [0, 1). The same function is
used by the simulator (to set prices) and by the experiment analysis (to recompute the
expected arm and audit the exposure log), so a logged arm can be checked independently.

Arms are mutually exclusive by construction: every unit gets exactly one arm per salt.
A fresh salt per experiment makes concurrent experiments independent (a factorial design).
"""

from __future__ import annotations

import hashlib
from typing import Literal

Arm = Literal["control", "treatment"]
ARMS: tuple[Arm, Arm] = ("control", "treatment")
_SCALE = float(2**64)


def assignment_uniform(salt: str, unit_id: str) -> float:
    """Uniform in [0, 1), deterministic in ``(salt, unit_id)``."""
    if not salt:
        raise ValueError("salt must be non-empty")
    digest = hashlib.blake2b(f"{salt}:{unit_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / _SCALE


def assign_arm(salt: str, unit_id: str, treated_fraction: float) -> Arm:
    """``treatment`` with probability ``treated_fraction`` (over units), else ``control``."""
    if not 0.0 < treated_fraction < 1.0:
        raise ValueError("treated_fraction must lie in (0, 1)")
    return "treatment" if assignment_uniform(salt, unit_id) < treated_fraction else "control"
