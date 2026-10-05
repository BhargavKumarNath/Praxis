"""Per-module coverage floors (required_test.md s5), on top of the global ``fail_under``.

Reads a ``coverage json`` report and the ordered ``[tool.praxis.coverage_floors]`` table in
pyproject.toml: each file gets the floor of the FIRST glob that matches it. Percent is
coverage.py's branch-mode total (lines + branches). A rule that matches no measured file is
an error, so floors cannot silently go stale when modules move.

Usage: python scripts/coverage_gate.py coverage.json
"""

from __future__ import annotations

import fnmatch
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_floors(pyproject: Path = ROOT / "pyproject.toml") -> dict[str, float]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    floors: dict[str, float] = data["tool"]["praxis"]["coverage_floors"]
    return {pattern: float(v) for pattern, v in floors.items()}


def evaluate(report: dict[str, Any], floors: dict[str, float]) -> tuple[list[str], list[str]]:
    """Return (failures, stale rules)."""
    failures: list[str] = []
    used: set[str] = set()
    for path, info in sorted(report["files"].items()):
        rule = next((p for p in floors if fnmatch.fnmatch(path, p)), None)
        if rule is None:
            failures.append(f"{path}: no coverage floor rule matches (add one)")
            continue
        used.add(rule)
        pct = float(info["summary"]["percent_covered"])
        if pct + 1e-9 < floors[rule]:
            failures.append(f"{path}: {pct:.1f}% < {floors[rule]:.0f}% (rule {rule!r})")
    stale = [p for p in floors if p not in used]
    return failures, stale


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(__doc__ or "")
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    failures, stale = evaluate(report, load_floors())
    for line in failures:
        sys.stderr.write(f"coverage floor: {line}\n")
    for rule in stale:
        sys.stderr.write(f"coverage floor: rule {rule!r} matches no file (stale)\n")
    if failures or stale:
        return 1
    sys.stdout.write(f"coverage floors OK ({len(report['files'])} files)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
