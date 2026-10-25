"""Agent CLI. Agent-side: ``tools``, ``call``, ``investigate``. Human-side: ``requests``,
``approve``, ``reject``, ``freezes``, ``lift``, ``audit``.

Everything reads and writes the control plane named by ``PRAXIS_DATABASE_URL``. ``approve``
is the ONLY way a guarded request takes effect, and it needs a named human approver who is
not the agent (``ApprovalDesk``; the database enforces the same rule).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from praxis.agent.config import load_agent_policy
from praxis.agent.executor import ToolExecutor
from praxis.agent.provider import Task
from praxis.agent.requests import RequestError
from praxis.agent.wiring import TOOLS, ControlPlane, control_plane, operator_for
from praxis.config import get_settings
from praxis.control.db import make_engine
from praxis.logging import configure_logging
from praxis.pricing.freeze import FreezeError


def _print(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


def _plane() -> ControlPlane:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("PRAXIS_DATABASE_URL is required (the control-plane database)")
    return control_plane(
        make_engine(settings.database_url.get_secret_value()),
        settings.environment,
        settings.warehouse_path,
    )


def _executor(plane: ControlPlane) -> ToolExecutor:
    return ToolExecutor(TOOLS, plane.backends, plane.audit, load_agent_policy())


def cmd_tools(_args: argparse.Namespace) -> int:
    _print([t.schema() for t in TOOLS])
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    executor = _executor(_plane())
    session = executor.new_session()
    result = executor.call(session, args.tool, json.loads(args.args))
    _print({"session_id": session.session_id, **result.message()})
    return 0 if result.ok else 2


def cmd_investigate(args: argparse.Namespace) -> int:
    task = Task(
        args.kind, args.as_of, incident_id=args.incident, model_version=args.model_version,
        decision_id=args.decision_id, note=args.note,
    )  # fmt: skip
    run = operator_for(_executor(_plane())).investigate(task)
    _print(run.as_dict())
    return 0 if run.outcome == "completed" and run.grounded else 1


def cmd_requests(args: argparse.Namespace) -> int:
    _print([r.as_dict() for r in _plane().desk.store.recent(args.limit)])
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    desk = _plane().desk
    try:
        if args.cmd == "approve":
            done = desk.approve(args.request_id, args.approver)
        else:
            done = desk.reject(args.request_id, args.approver, args.reason)
    except RequestError as refused:
        sys.stderr.write(f"refused: {refused}\n")
        return 2
    _print(done.as_dict())
    return 0 if done.status in ("completed", "rejected") else 1


def cmd_freezes(_args: argparse.Namespace) -> int:
    _print([f.as_dict() for f in _plane().backends.freezes.active()])
    return 0


def cmd_lift(args: argparse.Namespace) -> int:
    try:
        lifted = _plane().backends.freezes.lift(args.freeze_id, by=args.by)
    except FreezeError as refused:
        sys.stderr.write(f"refused: {refused}\n")
        return 2
    _print(lifted.as_dict())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    _print([e.as_dict() for e in _plane().audit.events(session_id=args.session)])
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="praxis.agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools").set_defaults(fn=cmd_tools)
    call = sub.add_parser("call", help="one audited tool call as the agent")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON object")
    call.set_defaults(fn=cmd_call)
    inv = sub.add_parser("investigate")
    kinds = ["investigate_incident", "review_candidate", "daily_check"]
    inv.add_argument("--kind", required=True, choices=kinds)
    inv.add_argument("--as-of", type=date.fromisoformat, required=True)
    inv.add_argument("--incident")
    inv.add_argument("--model-version")
    inv.add_argument("--decision-id")
    inv.add_argument("--note", default="")
    inv.set_defaults(fn=cmd_investigate)
    req = sub.add_parser("requests")
    req.add_argument("--limit", type=int, default=50)
    req.set_defaults(fn=cmd_requests)
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending agent request (named human only)")
        p.add_argument("request_id")
        p.add_argument("--approver", required=True)
        if name == "reject":
            p.add_argument("--reason", required=True)
        p.set_defaults(fn=cmd_decide)
    sub.add_parser("freezes").set_defaults(fn=cmd_freezes)
    lift = sub.add_parser("lift", help="lift a price freeze (named human only)")
    lift.add_argument("freeze_id")
    lift.add_argument("--by", required=True)
    lift.set_defaults(fn=cmd_lift)
    audit = sub.add_parser("audit")
    audit.add_argument("--session")
    audit.set_defaults(fn=cmd_audit)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("praxis-agent", get_settings().log_level.value)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
