"""The 2026-11 held-out pricing evaluation is pre-registered and pinned (ADR 0031)."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "configs/pricing/evaluation_2026_11.toml"
PINNED = "5697294bad2b771846911eeb68d3ace3a9e93e8bdc90a91b7eb6a32af2f7ab1d"
USED = {1, 42, 43, 44, 45, *range(101, 125), 201, 202, 203, 301, 302, 303, 601, 611, 612, 613,
        621, 701, 801, 802, 803, 804, 9001}  # fmt: skip


def test_registration_is_pinned() -> None:
    assert hashlib.sha256(FILE.read_bytes()).hexdigest() == PINNED


def test_seeds_are_fresh_and_the_method_is_the_pinned_policy() -> None:
    cfg = tomllib.loads(FILE.read_text())
    seeds = set(cfg["seeds"]) | set(cfg["replacement_seeds"])
    assert not seeds & USED and len(cfg["seeds"]) == 5
    policy = (REPO / "configs/pricing/policy.toml").read_bytes()
    assert hashlib.sha256(policy).hexdigest() == cfg["policy_sha256"]
