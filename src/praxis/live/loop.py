"""The live loop (ADR 0027): one simulated day per real UTC day, through the existing CLIs.

A **tick** for ``as_of`` (a real UTC date; data is observed through ``as_of - 1``):

1. rebuild the SYNTHETIC world through the last day to operate (simulator -> raw load ->
   dbt marts). The world is deterministic and prefix-stable (``configs/simulator/scenarios/
   live.toml``), so a rebuild never changes a day that was already observed;
2. publish the warehouse to the shared root (the API, console and ops monitor read it);
3. run the model lifecycle (``praxis.mlops operate``) for every day not yet operated, at most
   ``max_catchup_days``: probation (automatic rollback), shadow evaluation, drift monitoring
   and retraining (candidate -> validation -> challenger -> shadow). Nothing here promotes;
4. if a shadow version has passed, the operator reviews it (``praxis.agent investigate``,
   deterministic triage without an LLM) and files a PENDING promotion request, which only a
   named human can approve (the console);
5. record the tick (report + state) on the shared root.

**Bootstrap** trains, validates and promotes the first champion on the world through
``bootstrap.train_through``; the promotion is a named human's (``--approver``), never the
automation's. Every step is a subprocess of an existing, tested CLI; this module only plans
the argument lists, runs them in order and stops at the first failure (a failed step is
recorded and the state is not advanced, so the next tick retries the same days).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from praxis.live.config import REPO, LiveConfig

logger = logging.getLogger(__name__)

PYTHON = sys.executable
DBT_PROJECT = REPO / "dbt"
# Below the job's 4-hour hard limit (Terraform `live_job.timeout_s`). A retrain runs the
# rolling-origin backtest over the whole history, so its cost grows with the world's age
# (ADR 0027 amendments 1 and 2).
STEP_TIMEOUT_S = 14100.0
TIMED_OUT = 124  # the coreutils `timeout` convention
# One file name for the built and the published warehouse: DuckDB names a database's catalog
# after its file stem, and dbt-duckdb qualifies its views with it (``praxis.marts.x``), so a
# copy under another stem has broken views (2026-11-04 cloud smoke: "Catalog does not exist").
WAREHOUSE_FILE = "praxis.duckdb"


class LiveError(RuntimeError):
    """The loop refused to run (not bootstrapped, already bootstrapped, bad request)."""


class StepFailed(RuntimeError):
    def __init__(self, result: StepResult) -> None:
        super().__init__(f"step {result.name} exited {result.returncode}")
        self.result = result


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    name: str
    returncode: int
    elapsed_s: float
    output: dict[str, Any] | None  # the last JSON object the command printed

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.name, "returncode": self.returncode,
                "elapsed_s": round(self.elapsed_s, 2)}  # fmt: skip


Runner = Callable[[Step], StepResult]


def last_json(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object in a command's stdout (logs are JSON lines too)."""
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    for start in reversed(starts):
        if not text.startswith("{", start):
            continue
        try:
            value = json.loads(text[start:])
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    return None


def subprocess_runner(step: Step, timeout_s: float = STEP_TIMEOUT_S) -> StepResult:
    """Run one CLI; its stdout (structured logs + result) is passed through to ours."""
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv built from config, never from input
            list(step.argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, **step.env},
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        # a killed step is an ordinary failure: the tick stops, the state is not advanced
        tail = expired.stderr if isinstance(expired.stderr, str) else ""
        sys.stdout.write(f"step {step.name} timed out after {timeout_s:.0f} s\n{tail[-4000:]}")
        sys.stdout.flush()
        return StepResult(step.name, TIMED_OUT, time.monotonic() - started, None)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr[-4000:])  # the tail: tracebacks, never the environment
    sys.stdout.flush()
    return StepResult(step.name, proc.returncode, time.monotonic() - started,
                      last_json(proc.stdout))  # fmt: skip


@dataclass(frozen=True)
class Paths:
    """``root``: persistent and shared (the GCS volume); ``work``: this execution only."""

    root: Path
    work: Path

    @property
    def warehouse(self) -> Path:
        return self.root / "warehouse" / WAREHOUSE_FILE

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    def world(self, name: str) -> Path:
        return self.work / name


def world_days(cfg: LiveConfig, through: date) -> int:
    """Simulated days covering ``world.start_date`` .. ``through`` inclusive."""
    days = (through - cfg.world.start_date).days + 1
    if days < 1:
        raise LiveError(f"{through} is before the world starts ({cfg.world.start_date})")
    return days


def build_world_steps(cfg: LiveConfig, through: date, out: Path) -> list[Step]:
    """Simulate -> raw load -> dbt marts, for the world through ``through`` (inclusive)."""
    w = cfg.world
    warehouse = out / WAREHOUSE_FILE
    dbt_env = {
        "PRAXIS_DUCKDB_PATH": str(warehouse),
        "DBT_TARGET_PATH": str(out / "dbt" / "target"),
        "DBT_LOG_PATH": str(out / "dbt" / "logs"),
    }
    dbt = str(Path(PYTHON).with_name("dbt"))
    return [
        Step("simulate", (PYTHON, "-m", "praxis.simulator", "--scenario", str(w.scenario_path),
                          "--seed", str(w.seed), "--customers", str(w.customers),
                          "--days", str(world_days(cfg, through)), "--validate",
                          "--schema-every", "50", "--out", str(out / "sim"))),
        Step("load", (PYTHON, "-m", "praxis.data", "--db", str(warehouse), "--raw",
                      str(out / "raw"), "load-sim", "--dir", str(out / "sim"))),
        Step("dbt", (dbt, "run", "--project-dir", str(DBT_PROJECT), "--profiles-dir",
                     str(DBT_PROJECT), "--quiet"), dbt_env),
    ]  # fmt: skip


def mlops_step(name: str, paths: Paths, warehouse: Path, *args: str) -> Step:
    return Step(name, (PYTHON, "-m", "praxis.mlops", "--environment", "dev", "--warehouse",
                       str(warehouse), "--models", str(paths.models), *args))  # fmt: skip


def publish_warehouse(source: Path, target: Path) -> None:
    """Copy, then rename: a reader opens either the previous file or the new one, whole."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    shutil.copyfile(source, partial)
    partial.replace(target)


def read_state(paths: Paths) -> date | None:
    if not paths.state.exists():
        return None
    return date.fromisoformat(json.loads(paths.state.read_text())["last_as_of"])


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    partial.replace(path)


@dataclass
class LiveLoop:
    cfg: LiveConfig
    paths: Paths
    runner: Runner = subprocess_runner
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    results: list[StepResult] = field(default_factory=list)

    def _run(self, step: Step) -> StepResult:
        result = self.runner(step)
        self.results.append(result)
        logger.info("live.step", extra=result.as_dict())
        if result.returncode != 0:
            raise StepFailed(result)
        return result

    def _build(self, through: date, name: str) -> Path:
        out = self.paths.world(name)
        if out.exists():
            shutil.rmtree(out)
        for step in build_world_steps(self.cfg, through, out):
            self._run(step)
        shutil.rmtree(out / "sim", ignore_errors=True)  # ~0.3-1 GB in memory-backed /tmp
        return out / WAREHOUSE_FILE

    def today(self) -> date:
        return self.clock().astimezone(UTC).date()

    # ---------------------------------------------------------------- bootstrap
    def bootstrap(self, approver: str) -> dict[str, Any]:
        if read_state(self.paths) is not None:
            raise LiveError(f"already bootstrapped ({self.paths.state} exists)")
        approver = approver.strip()
        if not approver:
            raise LiveError("bootstrap needs a named human approver")
        through = self.cfg.bootstrap.train_through
        warehouse = self._build(through, "bootstrap")
        report = self.paths.world("bootstrap") / "backtest.json"
        forecasting = (PYTHON, "-m", "praxis.forecasting", "--db", str(warehouse))
        self._run(
            Step(
                "backtest",
                (
                    *forecasting,
                    "backtest",
                    "--scenario",
                    str(self.cfg.world.scenario_path),
                    "--report",
                    str(report),
                ),
            )
        )
        trained = self._run(
            Step(
                "train",
                (
                    *forecasting,
                    "train",
                    "--backtest-report",
                    str(report),
                    "--out",
                    str(self.paths.models),
                ),
            )
        )
        out = trained.output or {}
        version, path = out.get("model_version"), out.get("path")
        if not version or not path:
            raise LiveError("train printed no model_version / path")
        day_one = (through + timedelta(days=1)).isoformat()
        self._run(
            mlops_step(
                "register",
                self.paths,
                warehouse,
                "register",
                "--artifact",
                path,
                "--backtest",
                str(report),
            )
        )
        self._run(
            mlops_step(
                "validate",
                self.paths,
                warehouse,
                "validate",
                "--version",
                version,
                "--as-of",
                day_one,
            )
        )
        self._run(mlops_step("promote", self.paths, warehouse, "--actor", approver, "--reason",
                             "bootstrap: first champion of the live world (ADR 0027)",
                             "promote", "--version", version, "--as-of", day_one))  # fmt: skip
        write_json(self.paths.state, {"last_as_of": through.isoformat()})
        summary = {"bootstrap": True, "champion": version, "train_through": through.isoformat(),
                   "approver": approver}  # fmt: skip
        self._report(f"bootstrap-{through.isoformat()}", summary)
        return summary

    # --------------------------------------------------------------------- tick
    def tick(self, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or self.today()
        if as_of > self.today():
            raise LiveError(f"{as_of} has not happened yet (today is {self.today()} UTC)")
        last = read_state(self.paths)
        if last is None:
            raise LiveError("not bootstrapped: run `python -m praxis.live bootstrap` first")
        start = last + timedelta(days=1)
        if start > as_of:
            return {"as_of": as_of.isoformat(), "status": "up_to_date",
                    "last_as_of": last.isoformat()}  # fmt: skip
        end = min(as_of, start + timedelta(days=self.cfg.tick.max_catchup_days - 1))
        warehouse = self._build(end - timedelta(days=1), "tick")
        publish_warehouse(warehouse, self.paths.warehouse)
        days = self._run(mlops_step("lifecycle", self.paths, warehouse,
                                    "operate", "--start", start.isoformat(),
                                    "--end", end.isoformat())).output or {}  # fmt: skip
        summary = summarise(days.get("days", []))
        if self.cfg.tick.review_ready_shadow and summary["ready_for_promotion"]:
            summary["review"] = self._review(end, summary["ready_for_promotion"], warehouse)
        write_json(self.paths.state, {"last_as_of": end.isoformat()})
        summary.update({"as_of": as_of.isoformat(), "status": "operated",
                        "operated": [start.isoformat(), end.isoformat()],
                        "behind_days": (as_of - end).days})  # fmt: skip
        self._report(end.isoformat(), summary)
        return summary

    def republish(self) -> dict[str, Any]:
        """Repair: rebuild the world exactly as the last tick saw it (through the day before the
        last operated day) and publish it; nothing is operated and the state is unchanged.
        Prefix stability makes the rebuild identical to what the lifecycle already read."""
        last = read_state(self.paths)
        if last is None:
            raise LiveError("not bootstrapped: run `python -m praxis.live bootstrap` first")
        through = last - timedelta(days=1)
        publish_warehouse(self._build(through, "tick"), self.paths.warehouse)
        summary = {"status": "republished", "through": through.isoformat(),
                   "last_as_of": last.isoformat()}  # fmt: skip
        # repairs are kept apart: ``status`` (and the smoke) read the newest TICK report
        self._report(f"repairs/republish-{last.isoformat()}", summary)
        return summary

    def _review(self, as_of: date, version: str, warehouse: Path) -> dict[str, Any]:
        """The operator reviews a passed shadow; a non-zero exit (e.g. not grounded) is
        recorded, never fatal: the version simply stays in shadow until the next review."""
        result = self.runner(Step("review", (PYTHON, "-m", "praxis.agent", "investigate",
                                             "--kind", "review_candidate", "--as-of",
                                             as_of.isoformat(), "--model-version", version),
                                  {"PRAXIS_WAREHOUSE_PATH": str(warehouse)}))  # fmt: skip
        self.results.append(result)
        out = result.output or {}
        final = out.get("final") or {}
        calls = out.get("calls") or []
        requested = any(c.get("tool") == "request_promotion" and c.get("status") == "ok"
                        for c in calls)  # fmt: skip
        return {"returncode": result.returncode, "outcome": out.get("outcome"),
                "recommended_action": final.get("recommended_action"),
                "promotion_requested": requested}  # fmt: skip

    def _report(self, name: str, summary: dict[str, Any]) -> None:
        payload = {**summary, "is_synthetic": True, "world_seed": self.cfg.world.seed,
                   "recorded_at": self.clock().isoformat(),
                   "steps": [r.as_dict() for r in self.results]}  # fmt: skip
        write_json(self.paths.reports / f"{name}.json", payload)
        logger.info("live.tick", extra={k: payload[k] for k in payload if k != "steps"})


def summarise(days: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The lifecycle events of the operated days (what changed, not every report)."""
    events: list[dict[str, Any]] = []
    for day in days:
        d = day.get("as_of")
        monitor, retraining = day.get("monitor") or {}, day.get("retraining")
        shadow, probation = day.get("shadow") or {}, day.get("probation") or {}
        if monitor.get("alarms"):
            events.append({"as_of": d, "event": "drift_alarm", "signals": monitor["alarms"]})
        if (monitor.get("retraining_request") or {}).get("created"):
            events.append({"as_of": d, "event": "retraining_requested",
                           "triggers": monitor.get("retraining_triggers")})  # fmt: skip
        if retraining:
            keys = ("candidate", "validation_passed", "failed_checks", "stage", "shadow_started")
            events.append(
                {"as_of": d, "event": "retrained", **{k: retraining.get(k) for k in keys}}
            )
        if shadow.get("status") == "started":
            events.append({"as_of": d, "event": "shadow_started",
                           "version": shadow.get("challenger")})  # fmt: skip
        if shadow.get("status") == "evaluated":
            events.append({"as_of": d, "event": "shadow_evaluated",
                           "stage_after": shadow.get("stage_after"),
                           "ready_for_promotion": shadow.get("ready_for_promotion")})  # fmt: skip
        if probation.get("rolled_back"):
            events.append({"as_of": d, "event": "rolled_back",
                           "decision_id": probation["rolled_back"]})  # fmt: skip
    last = days[-1] if days else {}
    # Ready = the version still in shadow at the end had a passing evaluation in this range
    # (the registry re-checks readiness when a human approves; this only decides whether to
    # ask the operator for a review).
    passed = {
        day.get("shadow_version")
        for day in days
        if (day.get("shadow") or {}).get("ready_for_promotion")
    }
    ready = last.get("shadow_version") if last.get("shadow_version") in passed else None
    return {"champion": last.get("champion"), "shadow": last.get("shadow_version"),
            "events": events, "ready_for_promotion": ready}  # fmt: skip
