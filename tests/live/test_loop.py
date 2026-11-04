"""The live loop sequences the existing CLIs (ADR 0027): order, state, catch-up, review."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from praxis.live.config import load_live_config
from praxis.live.loop import (
    TIMED_OUT,
    LiveError,
    LiveLoop,
    Paths,
    Step,
    StepFailed,
    StepResult,
    build_world_steps,
    last_json,
    publish_warehouse,
    read_state,
    subprocess_runner,
    summarise,
    world_days,
)

CFG = load_live_config()
TODAY = datetime(2026, 11, 4, 3, 10, tzinfo=UTC)


def _arg(step: Step, flag: str) -> str:
    return step.argv[step.argv.index(flag) + 1]


class FakeCli:
    """Plays the CLIs: dbt writes the warehouse, train names an artifact, operate reports."""

    def __init__(self, days: list[dict[str, Any]] | None = None, fail: str | None = None,
                 review_rc: int = 0) -> None:  # fmt: skip
        self.steps: list[Step] = []
        self.days = days or []
        self.fail = fail
        self.review_rc = review_rc

    def __call__(self, step: Step) -> StepResult:
        self.steps.append(step)
        if step.name == self.fail:
            return StepResult(step.name, 1, 0.1, None)
        out: dict[str, Any] | None = None
        if step.name == "dbt":
            wh = Path(step.env["PRAXIS_DUCKDB_PATH"])
            wh.parent.mkdir(parents=True, exist_ok=True)
            wh.write_bytes(b"warehouse")
        elif step.name == "simulate":
            Path(_arg(step, "--out")).mkdir(parents=True, exist_ok=True)
        elif step.name == "train":
            out = {"model_version": "demand-hybrid-v1", "path": "/live/models/demand-hybrid-v1"}
        elif step.name == "lifecycle":
            out = {"days": self.days}
        elif step.name == "review":
            # the shape of praxis.agent investigate's JSON (AgentRun.as_dict)
            out = {"outcome": "completed",
                   "final": {"recommended_action": "request_promotion"},
                   "calls": [{"tool": "get_shadow_report", "status": "ok"},
                             {"tool": "request_promotion", "status": "ok"}]}  # fmt: skip
            return StepResult(step.name, self.review_rc, 0.1, None if self.review_rc else out)
        return StepResult(step.name, 0, 0.1, out)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.steps]


def _loop(tmp_path: Path, cli: FakeCli, cfg: Any = CFG) -> LiveLoop:
    return LiveLoop(cfg, Paths(tmp_path / "root", tmp_path / "work"), cli, clock=lambda: TODAY)


def _bootstrapped(tmp_path: Path, last: str) -> Paths:
    paths = Paths(tmp_path / "root", tmp_path / "work")
    paths.root.mkdir(parents=True)
    paths.state.write_text(json.dumps({"last_as_of": last}))
    return paths


def _day(as_of: str, **kw: Any) -> dict[str, Any]:
    return {"as_of": as_of, "champion": "v1", "shadow_version": None, "monitor": None,
            "retraining": None, "shadow": None, "probation": None, **kw}  # fmt: skip


# ------------------------------------------------------------------------ bootstrap
def test_bootstrap_trains_validates_and_has_a_human_promote(tmp_path: Path) -> None:
    cli = FakeCli()
    out = _loop(tmp_path, cli).bootstrap(" owner ")
    assert cli.names == ["simulate", "load", "dbt", "backtest", "train", "register",
                         "validate", "promote"]  # fmt: skip
    assert _arg(cli.steps[0], "--days") == str(world_days(CFG, CFG.bootstrap.train_through))
    promote = cli.steps[-1]
    assert _arg(promote, "--actor") == "owner"  # a named human, never the automation
    assert _arg(promote, "--version") == "demand-hybrid-v1"
    assert _arg(promote, "--as-of") == "2026-08-31"
    assert _arg(cli.steps[5], "--artifact") == "/live/models/demand-hybrid-v1"
    paths = Paths(tmp_path / "root", tmp_path / "work")
    assert read_state(paths) == CFG.bootstrap.train_through
    assert out["champion"] == "demand-hybrid-v1"
    report = json.loads((paths.reports / "bootstrap-2026-08-30.json").read_text())
    assert report["is_synthetic"] is True and len(report["steps"]) == 8
    assert not (paths.work / "bootstrap" / "sim").exists()  # the event file is dropped


def test_bootstrap_refuses_twice_and_without_an_approver(tmp_path: Path) -> None:
    with pytest.raises(LiveError, match="named human"):
        _loop(tmp_path, FakeCli()).bootstrap("  ")
    _bootstrapped(tmp_path, "2026-08-30")
    with pytest.raises(LiveError, match="already bootstrapped"):
        _loop(tmp_path, FakeCli()).bootstrap("owner")


def test_bootstrap_stops_at_a_failed_step_without_state(tmp_path: Path) -> None:
    cli = FakeCli(fail="validate")
    loop = _loop(tmp_path, cli)
    with pytest.raises(StepFailed, match="validate"):
        loop.bootstrap("owner")
    assert "promote" not in cli.names
    assert read_state(loop.paths) is None


def test_bootstrap_needs_the_trained_artifact(tmp_path: Path) -> None:
    class NoArtifact(FakeCli):
        def __call__(self, step: Step) -> StepResult:
            result = super().__call__(step)
            return StepResult(step.name, 0, 0.1, {}) if step.name == "train" else result

    with pytest.raises(LiveError, match="model_version"):
        _loop(tmp_path, NoArtifact()).bootstrap("owner")


# ----------------------------------------------------------------------------- tick
def test_republish_rebuilds_what_the_last_tick_saw_and_operates_nothing(
    tmp_path: Path,
) -> None:
    paths = _bootstrapped(tmp_path, "2026-11-04")
    cli = FakeCli()
    out = _loop(tmp_path, cli).republish()
    assert cli.names == ["simulate", "load", "dbt"]  # no lifecycle, no review
    assert _arg(cli.steps[0], "--days") == str((date(2026, 11, 3) - CFG.world.start_date).days + 1)
    assert out == {"status": "republished", "through": "2026-11-03", "last_as_of": "2026-11-04"}
    assert paths.warehouse.read_bytes() == b"warehouse"
    assert read_state(paths) == date(2026, 11, 4)  # unchanged
    assert (paths.reports / "repairs" / "republish-2026-11-04.json").exists()
    assert not list(paths.reports.glob("*.json"))  # the newest tick report stays the newest


def test_republish_needs_a_bootstrap(tmp_path: Path) -> None:
    with pytest.raises(LiveError, match="not bootstrapped"):
        _loop(tmp_path, FakeCli()).republish()


def test_tick_refuses_without_bootstrap_and_the_future(tmp_path: Path) -> None:
    with pytest.raises(LiveError, match="not bootstrapped"):
        _loop(tmp_path, FakeCli()).tick()
    _bootstrapped(tmp_path, "2026-11-01")
    with pytest.raises(LiveError, match="has not happened"):
        _loop(tmp_path, FakeCli()).tick(date(2026, 11, 5))


def test_tick_is_idempotent_once_up_to_date(tmp_path: Path) -> None:
    _bootstrapped(tmp_path, "2026-11-04")
    cli = FakeCli()
    out = _loop(tmp_path, cli).tick()
    assert out["status"] == "up_to_date" and cli.steps == []


def test_tick_operates_the_missing_days_on_point_in_time_data(tmp_path: Path) -> None:
    paths = _bootstrapped(tmp_path, "2026-11-02")
    cli = FakeCli(days=[_day("2026-11-03"), _day("2026-11-04")])
    out = _loop(tmp_path, cli).tick()
    assert cli.names == ["simulate", "load", "dbt", "lifecycle"]
    # the world ends the day before the last operated day: data through as_of - 1 only
    assert _arg(cli.steps[0], "--days") == str(world_days(CFG, date(2026, 11, 3)))
    lifecycle = cli.steps[-1]
    assert lifecycle.argv[-5:] == ("operate", "--start", "2026-11-03", "--end", "2026-11-04")
    assert _arg(lifecycle, "--models") == str(paths.models)
    assert paths.warehouse.read_bytes() == b"warehouse"  # published for the readers
    assert read_state(paths) == date(2026, 11, 4)
    assert out["operated"] == ["2026-11-03", "2026-11-04"] and out["behind_days"] == 0
    assert (paths.reports / "2026-11-04.json").exists()


def test_tick_catch_up_is_bounded(tmp_path: Path) -> None:
    paths = _bootstrapped(tmp_path, "2026-08-30")
    cli = FakeCli()
    out = _loop(tmp_path, cli).tick()
    assert cli.steps[-1].argv[-3:] == ("2026-08-31", "--end", "2026-09-06")  # 7 days
    assert read_state(paths) == date(2026, 9, 6)
    assert out["behind_days"] == (date(2026, 11, 4) - date(2026, 9, 6)).days


def test_a_failed_lifecycle_does_not_advance_the_state(tmp_path: Path) -> None:
    paths = _bootstrapped(tmp_path, "2026-11-02")
    with pytest.raises(StepFailed):
        _loop(tmp_path, FakeCli(fail="lifecycle")).tick()
    assert read_state(paths) == date(2026, 11, 2)  # the next tick retries the same days


def test_a_passed_shadow_is_reviewed_by_the_operator(tmp_path: Path) -> None:
    _bootstrapped(tmp_path, "2026-11-03")
    days = [_day("2026-11-04", shadow_version="v2",
                 shadow={"status": "evaluated", "stage_after": "shadow",
                         "ready_for_promotion": True})]  # fmt: skip
    cli = FakeCli(days=days)
    out = _loop(tmp_path, cli).tick()
    review = cli.steps[-1]
    assert review.name == "review"
    assert review.argv[3:] == ("investigate", "--kind", "review_candidate", "--as-of",
                               "2026-11-04", "--model-version", "v2")  # fmt: skip
    assert review.env["PRAXIS_WAREHOUSE_PATH"].endswith("/praxis.duckdb")
    assert out["review"] == {"returncode": 0, "outcome": "completed",
                             "recommended_action": "request_promotion",
                             "promotion_requested": True}  # fmt: skip


def test_a_failed_review_is_recorded_not_fatal(tmp_path: Path) -> None:
    paths = _bootstrapped(tmp_path, "2026-11-03")
    days = [_day("2026-11-04", shadow_version="v2", shadow={"ready_for_promotion": True})]
    out = _loop(tmp_path, FakeCli(days=days, review_rc=1)).tick()
    assert out["review"]["returncode"] == 1
    assert out["review"]["promotion_requested"] is False
    assert read_state(paths) == date(2026, 11, 4)


def test_no_review_without_a_passed_shadow_or_when_disabled(tmp_path: Path) -> None:
    _bootstrapped(tmp_path, "2026-11-03")
    days = [_day("2026-11-04", shadow_version="v2", shadow={"status": "insufficient"})]
    cli = FakeCli(days=days)
    _loop(tmp_path, cli).tick()
    assert "review" not in cli.names
    paths = Paths(tmp_path / "root2", tmp_path / "work2")
    paths.root.mkdir()
    paths.state.write_text(json.dumps({"last_as_of": "2026-11-03"}))
    off = CFG.model_copy(
        update={"tick": CFG.tick.model_copy(update={"review_ready_shadow": False})}
    )
    ready = [_day("2026-11-04", shadow_version="v2", shadow={"ready_for_promotion": True})]
    cli = FakeCli(days=ready)
    LiveLoop(off, paths, cli, clock=lambda: TODAY).tick()
    assert "review" not in cli.names


# ------------------------------------------------------------------------- helpers
def test_summarise_reports_what_changed() -> None:
    days = [
        _day(
            "2026-10-01",
            monitor={
                "alarms": ["residual"],
                "retraining_triggers": ["residual"],
                "retraining_request": {"created": True},
            },
        ),
        _day(
            "2026-10-02",
            retraining={
                "candidate": "v2",
                "validation_passed": True,
                "failed_checks": [],
                "stage": "shadow",
                "shadow_started": True,
            },
        ),
        _day("2026-10-03", shadow={"status": "started", "challenger": "v3"}),
        _day(
            "2026-10-20",
            shadow_version="v2",
            shadow={"status": "evaluated", "stage_after": "shadow", "ready_for_promotion": True},
        ),
        _day("2026-10-21", probation={"rolled_back": "rb-1"}, shadow_version="v2"),
    ]
    out = summarise(days)
    assert [e["event"] for e in out["events"]] == [
        "drift_alarm", "retraining_requested", "retrained", "shadow_started",
        "shadow_evaluated", "rolled_back"]  # fmt: skip
    assert out["ready_for_promotion"] == "v2" and out["shadow"] == "v2"
    assert summarise([])["ready_for_promotion"] is None
    # a version that passed earlier but is no longer in shadow is not ready
    assert summarise([days[3], _day("2026-10-22")])["ready_for_promotion"] is None


def test_last_json_skips_log_lines() -> None:
    text = '{"level":"INFO","message":"x"}\n{\n  "a": 1,\n  "b": [2]\n}\n'
    assert last_json(text) == {"a": 1, "b": [2]}
    assert last_json('{"only": true}') == {"only": True}
    assert last_json("no json\n") is None
    assert last_json("[1, 2]\n") is None
    assert last_json('{"broken": \n') is None


def test_world_steps_and_days() -> None:
    assert world_days(CFG, CFG.world.start_date) == 1
    with pytest.raises(LiveError, match="before the world"):
        world_days(CFG, date(2026, 4, 5))
    steps = build_world_steps(CFG, date(2026, 4, 15), Path("/w"))
    assert [s.name for s in steps] == ["simulate", "load", "dbt"]
    sim = steps[0]
    assert _arg(sim, "--seed") == "9001" and _arg(sim, "--days") == "10"
    assert _arg(sim, "--customers") == "1000"
    assert steps[2].env["PRAXIS_DUCKDB_PATH"] == "/w/praxis.duckdb"


def test_the_published_warehouse_keeps_its_catalog(tmp_path: Path) -> None:
    """dbt-duckdb views name the catalog (= file stem); the copy the API reads must resolve them."""
    paths = Paths(root=tmp_path / "root", work=tmp_path / "work")
    built = build_world_steps(CFG, date(2026, 4, 15), tmp_path / "w")[2].env["PRAXIS_DUCKDB_PATH"]
    assert Path(built).name == paths.warehouse.name
    Path(built).parent.mkdir(parents=True)
    con = duckdb.connect(built)
    catalog = Path(built).stem
    con.execute("CREATE SCHEMA marts; CREATE TABLE marts.t AS SELECT 7 AS x")
    view = f"CREATE VIEW marts.v AS SELECT x FROM {catalog}.marts.t"  # noqa: S608 - test constant
    con.execute(view)  # qualified with the catalog, as dbt-duckdb writes it
    con.close()
    publish_warehouse(Path(built), paths.warehouse)
    with duckdb.connect(str(paths.warehouse), read_only=True) as published:
        assert published.execute("SELECT x FROM marts.v").fetchone() == (7,)


def test_publish_replaces_the_warehouse_whole(tmp_path: Path) -> None:
    src = tmp_path / "w.duckdb"
    src.write_bytes(b"new")
    target = tmp_path / "root" / "warehouse" / "praxis.duckdb"
    publish_warehouse(src, target)
    assert target.read_bytes() == b"new"
    assert not target.with_name("praxis.duckdb.partial").exists()


def test_subprocess_runner_parses_the_result(capsys: pytest.CaptureFixture[str]) -> None:
    code = 'import json; print(json.dumps({"level": "INFO"})); print(json.dumps({"ok": 1}))'
    result = subprocess_runner(Step("py", (sys.executable, "-c", code)))
    assert result.returncode == 0 and result.output == {"ok": 1}
    assert '"ok": 1' in capsys.readouterr().out  # passed through to the job's log
    bad = subprocess_runner(Step("py", (sys.executable, "-c", "raise SystemExit('boom')")))
    assert bad.returncode == 1 and bad.output is None
    assert "boom" in capsys.readouterr().out
    assert bad.as_dict() == {"step": "py", "returncode": 1, "elapsed_s": bad.as_dict()["elapsed_s"]}


def test_a_step_that_times_out_is_a_failure_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    slow = subprocess_runner(Step("slow", (sys.executable, "-c", "import time; time.sleep(5)")),
                             timeout_s=0.2)  # fmt: skip
    assert slow.returncode == TIMED_OUT and slow.output is None
    assert "slow timed out" in capsys.readouterr().out
