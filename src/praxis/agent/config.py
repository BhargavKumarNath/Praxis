"""Agent policy (``configs/agent/policy.toml``): identity, tier ceiling, budgets, triage."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs" / "agent"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentSection(_Cfg):
    actor: str = Field(min_length=1)
    enabled: bool
    max_tier: int = Field(ge=0, le=2)
    max_steps: int = Field(ge=1, le=64)
    max_actions: int = Field(ge=0, le=10)
    max_tool_errors: int = Field(ge=1, le=20)
    provider: str = Field(min_length=1)
    fallback: str | None = None


class TriageSection(_Cfg):
    recovery_min_resolved: int = Field(ge=1)
    recovery_collapse_ratio: float = Field(gt=0, lt=1)
    recovery_window_days: int = Field(ge=1, le=90)


class AgentPolicy(_Cfg):
    version: str = Field(min_length=1)
    agent: AgentSection
    triage: TriageSection


def load_agent_policy(path: Path | None = None) -> AgentPolicy:
    return AgentPolicy.model_validate(
        tomllib.loads((path or CONFIG_DIR / "policy.toml").read_text())
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
