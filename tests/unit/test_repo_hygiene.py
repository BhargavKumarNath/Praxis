"""Phase 0 hygiene gates: .env.example holds names only; the secret scan is clean."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from praxis.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _entries() -> list[tuple[str, str]]:
    lines = (ROOT / ".env.example").read_text().splitlines()
    pairs = [ln.split("=", 1) for ln in lines if ln.strip() and not ln.startswith("#")]
    return [(k.strip(), v.strip()) for k, v in pairs]


def test_env_example_has_names_only() -> None:
    entries = _entries()
    assert entries, ".env.example must list variable names"
    assert all(value == "" for _, value in entries), "values must be empty"


def test_env_example_matches_settings_fields() -> None:
    documented = {k.removeprefix("PRAXIS_").lower() for k, _ in _entries()}
    assert documented == set(Settings.model_fields)


def test_env_is_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", ".env"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tracked.strip() == ""


def test_secret_scan_clean() -> None:
    from scripts.secret_scan import scan

    assert scan(ROOT) == []


@pytest.mark.parametrize(
    "sample",
    [
        "sk_live_" + "a" * 20,
        "whsec_" + "b" * 20,
        "postgresql://" + "u:pw@host/db",
        "AIza" + "c" * 35,
    ],
)
def test_secret_patterns_detect_samples(sample: str) -> None:
    from scripts.secret_scan import PATTERNS

    assert any(p.search(sample) for p in PATTERNS.values())
    assert re.search(r"\w", sample)


# Ratchet: functions over the complexity budget carry `complexity-debt`. Lower this number
# when you refactor one; never raise it (CLAUDE.md "CI/CD and code health").
COMPLEXITY_DEBT_MAX = 10


def test_complexity_debt_only_shrinks() -> None:
    markers = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted((ROOT / "src").rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "complexity-debt" in line
    ]
    assert len(markers) <= COMPLEXITY_DEBT_MAX, (
        f"{len(markers)} complexity-debt markers > {COMPLEXITY_DEBT_MAX}: split the new "
        f"function instead of adding a noqa. Markers: {markers}"
    )
