"""The lifecycle drill end to end on the toy worlds (mechanics), plus its CLI wiring."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from praxis.config import Environment
from praxis.control.db import make_engine
from praxis.mlops import __main__ as cli_module
from praxis.mlops.drill import DrillEnv, DrillPlan, run_drill
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryStore
from tests.forecasting.helpers import START
from tests.mlops import helpers as h
from tests.mlops.conftest import World

pytestmark = pytest.mark.slow

TOY = DrillPlan(
    start_date=START,
    champion_end_day=h.TRAIN_END_DAY,
    first_as_of=h.TRAIN_END_DAY + 1,
    last_as_of=h.N_DAYS - 1,
    drift_onset=h.SHIFT_DAY,
    detect_within_days=14,
    max_steady_alarm_days=0,
)


def test_plan_reads_the_preregistered_acceptance() -> None:
    plan = DrillPlan.from_acceptance(date(2026, 1, 5))
    assert (plan.champion_end_day, plan.first_as_of, plan.last_as_of) == (195, 196, 252)
    assert plan.drift_onset == 203 and plan.detect_within_days == 14
    assert plan.max_steady_alarm_days == 0 and plan.force_day == 217
    assert plan.day(196) == date(2026, 7, 20)


def test_drill_passes_on_the_toy_worlds(
    pg_url_factory: Callable[[], str], world: World, tmp_path: Path
) -> None:
    cfg = h.mlops_config()

    def make(name: str) -> ModelRegistry:
        return ModelRegistry(
            RegistryStore(make_engine(pg_url_factory())), cfg, environment=Environment.DEV
        )

    env = DrillEnv(
        make, cfg, tmp_path / "models", h.forecast_config(), h.bad_forecast_config(), "test-rev"
    )
    report = run_drill(TOY, world.clean, world.drifted, env)
    checks = {c["check"]: c for c in report["checks"]}
    failed = {n: c for n, c in checks.items() if not c["passed"]}
    assert report["passed"], failed
    # the drift world equals the steady world before its shift, so one champion serves both
    assert report["champion_data_version"] == report["drift_prefix_data_version"]
    assert report["rollback"]["rejected"]["stage"] == "rejected"
    assert [a["trigger"] for a in report["rollback"]["audit"]] == ["rollback", "restore"]
    assert report["steady"]["promoted"]["version"] == report["steady"]["champion"]
    assert checks["drift_detected_within_days"]["first_detection"] <= TOY.day(140).isoformat()
    json.dumps(report, default=str)  # the report is serialisable


def test_drill_cli_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from praxis.config import get_settings

    monkeypatch.setenv("PRAXIS_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}

    def fake_run(plan: DrillPlan, steady: Path, drift: Path, env: DrillEnv) -> dict[str, Any]:
        seen.update(plan=plan, steady=steady, drift=drift, env=env)
        return {"passed": False, "checks": []}

    monkeypatch.setattr(cli_module, "run_drill", fake_run)
    code = cli_module.main(
        [
            "--database-url",
            "postgresql+psycopg://u@h/db",
            "drill",
            "--steady",
            "s.duckdb",
            "--drift",
            "d.duckdb",
            "--seed",
            "7",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 1
    assert json.loads((tmp_path / "out" / "drill.json").read_text())["seed"] == 7
    env = seen["env"]
    assert env.bad_config.ridge.alpha == 1e9 and env.bad_config.lightgbm.num_boost_round == 1
    assert env.forecast_config.acceptance.max_vwape_ratio_vs_seasonal_naive == 0.9
    assert seen["plan"].first_as_of == 196
    capsys.readouterr()


def test_drill_registry_creates_a_fresh_database(pg_url: str, tmp_path: Path) -> None:
    admin = make_url(pg_url).set(database="postgres").render_as_string(hide_password=False)
    args = argparse.Namespace(seed=0, out=tmp_path, config=None)
    reg = cli_module._drill_registry(admin, "unit", args)
    assert reg.environment == Environment.DEV
    assert reg.store.in_stage(reg.model_name, h.mlops_config().lifecycle.stages[0]) == []
