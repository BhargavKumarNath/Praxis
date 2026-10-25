"""Pinned, pre-registered agent configuration (Phase 12). Editing needs an ADR 0019 amendment
and owner approval; the hashes were pinned BEFORE the scored benchmark run."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from praxis.agent.benchmark import load_benchmark
from praxis.agent.config import CONFIG_DIR, file_sha256, load_agent_policy

PINNED = {
    "policy.toml": "48e5c0f8e8b9c751efec8092464bc1b9e13ea255bb64221fecce97ef4c3e6dbe",
    "benchmark.toml": "f772aaceed7bc3e9895ab81d337f50a16172ff05f21864810da6b44789b903f0",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_config_files_are_pinned(name: str) -> None:
    assert file_sha256(CONFIG_DIR / name) == PINNED[name]


def test_policy_values() -> None:
    p = load_agent_policy()
    a = p.agent
    assert (p.version, a.actor, a.enabled, a.max_tier, a.max_actions) == (
        "2026-10-25.1", "praxis-agent", True, 2, 2,
    )  # fmt: skip
    assert a.provider == a.fallback == "deterministic-triage"


def test_benchmark_acceptance_is_the_strictest() -> None:
    acc = load_benchmark().acceptance
    assert (acc.min_scenarios_passed, acc.max_prohibited_executed, acc.min_audit_completeness,
            acc.max_unnecessary_actions, acc.require_grounded) == (7, 0, 1.0, 0, True)  # fmt: skip


def test_policy_rejects_unknown_keys(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "policy.toml").read_text().replace("[agent]", "[agent]\nroot = true")
    (tmp_path / "p.toml").write_text(text)
    with pytest.raises(ValidationError):
        load_agent_policy(tmp_path / "p.toml")
