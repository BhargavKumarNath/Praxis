"""Code-revision identifier for model and data lineage."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=_REPO,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def code_revision() -> str:
    """``<short sha>``, with ``-dirty`` when the working tree has uncommitted changes."""
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return "unknown"
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return f"{sha}-dirty" if dirty else sha
