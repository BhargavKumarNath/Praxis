"""Runbook structure validation: every alert points at a complete, existing runbook."""

from __future__ import annotations

import re
from pathlib import Path

from praxis.ops.config import RUNBOOK_DIR, OpsConfig

REQUIRED_RUNBOOKS = (
    "external-source-outage",
    "stripe-webhook-surge",
    "stale-model",
    "broken-candidate-deployment",
    "database-outage",
    "pubsub-backlog",
    "severe-forecast-error",
    "agent-failure",
)
REQUIRED_SECTIONS = ("Trigger", "Impact", "Diagnose", "Mitigate", "Verify", "Escalate")
_ALERTS_LINE = re.compile(r"^Alerts:\s*(.*)$", re.MULTILINE)


def alerts_of(text: str) -> list[str]:
    m = _ALERTS_LINE.search(text)
    return (
        [a.strip() for a in m.group(1).split(",") if a.strip() and a.strip() != "none"] if m else []
    )


def validate_runbooks(cfg: OpsConfig, directory: Path | None = None) -> list[str]:
    """Problems found; empty means every runbook is complete and consistent with the alerts."""
    base = directory or RUNBOOK_DIR
    problems: list[str] = []
    wanted = set(REQUIRED_RUNBOOKS) | {r.runbook for r in cfg.rules}
    rule_ids = {r.id for r in cfg.rules}
    for name in sorted(wanted):
        path = base / f"{name}.md"
        if not path.exists():
            problems.append(f"{name}: runbook file missing")
            continue
        text = path.read_text()
        for section in REQUIRED_SECTIONS:
            if not re.search(rf"^## {section}\b", text, re.MULTILINE):
                problems.append(f"{name}: missing section '## {section}'")
        listed = set(alerts_of(text))
        problems += [f"{name}: lists unknown alert {a}" for a in sorted(listed - rule_ids)]
        owned = {r.id for r in cfg.rules if r.runbook == name}
        problems += [f"{name}: does not list alert {a}" for a in sorted(owned - listed)]
    return problems
