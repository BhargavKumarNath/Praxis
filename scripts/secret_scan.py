"""Minimal secret scan over tracked and untracked-but-not-ignored files.

Fails on high-confidence credential patterns. ``.env`` is ignored by git and is
checked separately: it must never be tracked.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "stripe_live_or_test_secret": re.compile(r"\b[sr]k_(live|test)_[0-9A-Za-z]{10,}"),
    "stripe_webhook_secret": re.compile(r"\bwhsec_[0-9A-Za-z]{10,}"),
    "gcp_service_account": re.compile(r'"type"\s*:\s*"service_account"'),
    "private_key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "postgres_url_with_password": re.compile(r"postgres(ql)?://[^:\s/]+:[^@\s]+@"),
}
SELF = Path(__file__).name


def _files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return [root / p for p in out.decode().split("\0") if p]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", ".env"],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if tracked:
        findings.append(".env is tracked by git")
    for path in _files(root):
        if path.name == SELF or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(root)}: {name}")
    return findings


def main() -> int:
    findings = scan(Path(__file__).resolve().parents[1])
    for f in findings:
        print(f"SECRET-SCAN: {f}")  # noqa: T201
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
