"""Resilience suite plumbing: shared fixtures and the s21 report (``make resilience``).

With ``PRAXIS_RESILIENCE_OUT`` set, the session writes one record per pre-registered failure
to that path. A failure without a record (its test errored before being judged, or did not
run) is written as ``FAIL (no record)``: a missing row can never look like a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.agent.conftest import pg_world, policy  # noqa: F401  (shared fixtures)
from tests.mlops.conftest import (  # noqa: F401  (session-scoped MLOps world fixtures)
    cfg,
    champion,
    good,
    make_registry,
    registry,
    registry_store,
    world,
)
from tests.resilience.harness import (
    CONFIG_PATH,
    CRITERIA,
    ORIGINAL_PATH,
    RECORDS,
    REMEDIATION_802_PATH,
    REMEDIATION_803_PATH,
    load_criteria,
)

# Captured at import: the autouse settings fixture strips PRAXIS_* variables per test.
OUT = os.environ.get("PRAXIS_RESILIENCE_OUT")


def _revision() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv
            ["git", *args],  # noqa: S607 - git from PATH, fixed arguments
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def report() -> dict[str, Any]:
    rows = []
    for f in CRITERIA.failure:
        rec = RECORDS.get(f.id)
        if rec is None:
            rows.append({"failure_id": f.id, "verdict": "FAIL", "reasons": ["no record"],
                         "criteria": f.model_dump(), "measured": None})  # fmt: skip
        else:
            rows.append(rec.__dict__)
    return {
        "phase": 15,
        "suite": "resilience",
        "generated_at": datetime.now(UTC).isoformat(),
        "criteria_version": CRITERIA.version,
        "criteria_path": CONFIG_PATH.name,
        "criteria_sha256": hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        "supersedes": [
            {
                "criteria": path.name,
                "version": load_criteria(path).version,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "evidence": f"docs/evidence/{evidence} (kept unchanged)",
            }
            for path, evidence in (
                (REMEDIATION_803_PATH, "phase-15-remediation-803-failures.json"),
                (REMEDIATION_802_PATH, "phase-15-remediation-802-failures.json"),
                (ORIGINAL_PATH, "phase-15-failures.json"),
            )
        ],
        "code_revision": _revision(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "run": CRITERIA.run.model_dump(),
        "data_origin": "SYNTHETIC (simulator worlds, fixtures, local services; no real customer)",
        "passed": sum(r["verdict"] == "PASS" for r in rows),
        "total": len(rows),
        "gate": "PASS" if all(r["verdict"] == "PASS" for r in rows) else "FAIL",
        "failures": rows,
    }


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if OUT:
        Path(OUT).write_text(json.dumps(report(), indent=2, default=str) + "\n")
