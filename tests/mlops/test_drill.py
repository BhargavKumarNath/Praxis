"""The lifecycle drill end to end on the toy worlds (mechanics), plus its CLI wiring."""

from __future__ import annotations

import json
import pickle
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.engine import make_url

from praxis.config import Environment
from praxis.control.db import make_engine
from praxis.mlops import __main__ as cli_module
from praxis.mlops import drill
from praxis.mlops.drill import DrillEnv, DrillPlan, DrillRegistries, run_drill
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


@dataclass(frozen=True)
class ToyRegistries:
    """Picklable registry factory: one pre-created test database per world."""

    urls: dict[str, str]

    def __call__(self, name: str, fresh: bool = True) -> ModelRegistry:
        del fresh  # the test databases are created fresh by the fixture
        engine = make_engine(self.urls[name])
        return ModelRegistry(RegistryStore(engine), h.mlops_config(), environment=Environment.DEV)


def test_drill_passes_on_the_toy_worlds(
    pg_url_factory: Callable[[], str], world: World, tmp_path: Path
) -> None:
    """The real path: one spawned process per world, then evaluation from the registries."""
    factory = ToyRegistries({n: pg_url_factory() for n in ("steady", "drift", "rollback")})
    env = DrillEnv(
        factory,
        h.mlops_config(),
        tmp_path / "models",
        h.forecast_config(),
        h.bad_forecast_config(),
        "test-rev",
        parallel=True,
    )
    report = run_drill(TOY, world.clean, world.drifted, env)
    checks = {c["check"]: c for c in report["checks"]}
    failed = {n: c for n, c in checks.items() if not c["passed"]}
    assert report["passed"], failed
    # the drift world equals the steady world before its shift, so one champion serves both
    assert report["champion_data_version"] == report["drift_prefix_data_version"]
    assert report["rollback"]["rejected"]["stage"] == "rejected"
    assert [a["trigger"] for a in report["rollback"]["audit"]] == ["rollback", "restore"]
    steady = report["steady"]
    assert steady["promotions"] and steady["champion"] == steady["promotions"][-1]["version"]
    moves = checks["no_champion_change_without_approver"]["moves"]
    approved = [m for m in moves if m["trigger"] == "promote"]
    assert {m["actor"] for m in approved} == {"drill-operator"}
    assert len(approved) == len(steady["promotions"]) + len(report["drift"]["promotions"])
    assert [m["registry"] for m in moves if m["trigger"] == "promote.forced"] == ["rollback"]
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
    # spawned world processes rebuild their registry from it: it must pickle BY MODULE
    # (a factory defined in praxis.mlops.__main__ cannot be found by a spawned process)
    assert env.parallel and pickle.dumps(env.make_registry)
    assert type(env.make_registry).__module__ == "praxis.mlops.drill"
    assert env.make_registry.admin_url == "postgresql+psycopg://u@h/db"
    assert env.bad_config.ridge.alpha == 1e9 and env.bad_config.lightgbm.num_boost_round == 1
    assert env.forecast_config.acceptance.max_vwape_ratio_vs_seasonal_naive == 0.9
    assert seen["plan"].first_as_of == 196
    capsys.readouterr()


def test_drill_registry_creates_a_fresh_database(pg_url: str, tmp_path: Path) -> None:
    admin = make_url(pg_url).set(database="postgres").render_as_string(hide_password=False)
    registries = DrillRegistries(admin, 0, tmp_path)
    reg = registries("unit")
    assert reg.environment == Environment.DEV
    assert reg.store.in_stage(reg.model_name, h.mlops_config().lifecycle.stages[0]) == []
    reg.store.request_retraining(reg.model_name, ["manual"], {}, date(2026, 1, 5), None, None)
    attached = registries("unit", False)
    assert len(attached.store.requests(attached.model_name)) == 1  # attached, not recreated
    fresh = registries("unit")
    assert fresh.store.requests(fresh.model_name) == []


def test_only_named_actors_move_versions_into_champion() -> None:
    ok = [
        {"registry": "steady", "trigger": "promote.bootstrap", "actor": "drill-operator"},
        {"registry": "steady", "trigger": "promote", "actor": "drill-operator"},
        {"registry": "rollback", "trigger": "promote.forced", "actor": "drill-operator"},
        {"registry": "rollback", "trigger": "restore", "actor": "mlops-probation"},
    ]
    assert drill._moves_are_authorised(ok)
    for bad in (
        {"registry": "steady", "trigger": "promote", "actor": "mlops-automation"},
        {"registry": "steady", "trigger": "promote", "actor": "mlops-probation"},
        {"registry": "drift", "trigger": "promote.forced", "actor": "drill-operator"},
        {"registry": "steady", "trigger": "restore", "actor": "drill-operator"},
        {"registry": "steady", "trigger": "unknown", "actor": "drill-operator"},
    ):
        assert not drill._moves_are_authorised([*ok, bad]), bad


def test_final_champion_is_the_last_approved_or_restored_version() -> None:
    world: dict[str, Any] = {"promotions": [], "rollbacks": [], "champion": "v0"}
    assert drill._final_champion_is_approved(world, "v0")
    assert not drill._final_champion_is_approved({**world, "champion": "v1"}, "v0")
    world = {**world, "promotions": [{"version": "v1"}, {"version": "v2"}], "champion": "v2"}
    assert drill._final_champion_is_approved(world, "v0")
    assert not drill._final_champion_is_approved({**world, "champion": "v1"}, "v0")
    restored = {**world, "rollbacks": [{"version": "v1"}], "champion": "v1"}
    assert drill._final_champion_is_approved(restored, "v0")


def test_worlds_run_in_sequence_without_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    def make(name: str, fresh: bool) -> ModelRegistry:
        assert fresh
        created.append(name)
        return cast(ModelRegistry, object())

    env = DrillEnv(
        make,
        h.mlops_config(),
        Path("models"),
        h.forecast_config(),
        h.bad_forecast_config(),
        "rev",
    )

    def fake(name: str) -> Callable[..., dict[str, Any]]:
        def run(e: DrillEnv, plan: DrillPlan, warehouse: Path, champion: Any) -> dict[str, Any]:
            assert e is env and plan is TOY and champion == "champion"
            e.registry(name)
            return {"world": name, "warehouse": warehouse}

        return run

    monkeypatch.setattr(drill, "WORLDS", {n: fake(n) for n in ("steady", "drift", "rollback")})
    dbs = {"steady": Path("s"), "drift": Path("d"), "rollback": Path("s")}
    out = drill._run_worlds(env, TOY, dbs, "champion")  # type: ignore[arg-type]
    assert created == ["steady", "drift", "rollback"]
    assert {n: o["warehouse"] for n, o in out.items()} == dbs
