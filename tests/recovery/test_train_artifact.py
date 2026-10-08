"""Training protocol, warehouse extract, artifact lineage / integrity, CLI (small real world)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from praxis.recovery import __main__ as cli
from praxis.recovery.artifact import (
    ArtifactError,
    finalise_manifest,
    latest_artifact,
    load_artifact,
    save_artifact,
)
from praxis.recovery.config import load_model_config, load_policy
from praxis.recovery.dataset import build_episodes
from praxis.recovery.features import FEATURE_VERSION
from praxis.recovery.train import train
from praxis.recovery.warehouse import WarehouseUnavailable, load_histories
from tests.recovery.world import World

pytestmark = pytest.mark.slow
START = datetime(2026, 1, 5, tzinfo=UTC)
SEL, CUT = START + timedelta(days=100), START + timedelta(days=120)
GAPS = (1, 2, 3, 4, 5, 7, 10)


@pytest.fixture(scope="module")
def trained(world: World) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    return train(
        world.db,
        selection_cutoff=SEL,
        train_cutoff=CUT,
        cfg=load_model_config(),
        policy=load_policy(),
        gap_choices=GAPS,
        code_revision="abc1234",
        now=CUT,
    )


def test_extract_is_point_in_time(world: World) -> None:
    histories = load_histories(world.db, CUT)
    for h in histories.values():
        assert h.created_at < CUT
        for inv in h.invoices:
            assert inv.created_at < CUT
            assert all(a.resolved_at < CUT for a in inv.attempts)
    later = build_episodes(load_histories(world.db, START + timedelta(days=180)))
    early = {e.invoice_id: e for e in build_episodes(histories)}
    grew = [e for e in later if e.invoice_id in early and e.retries != early[e.invoice_id].retries]
    assert grew, "episodes open at the cutoff are right-censored there"
    assert all(
        early[e.invoice_id].retries == e.retries[: len(early[e.invoice_id].retries)] for e in grew
    )


def test_missing_warehouse_or_marts(tmp_path: Path) -> None:
    with pytest.raises(WarehouseUnavailable):
        load_histories(tmp_path / "nope.duckdb", CUT)
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    with pytest.raises(WarehouseUnavailable):
        load_histories(empty, CUT)


def test_manifest_carries_full_lineage(
    trained: tuple[dict[str, Any], dict[str, str], dict[str, Any]],
) -> None:
    manifest, files, report = trained
    for key in (
        "model_version",
        "data_version",
        "feature_version",
        "code_revision",
        "model_config",
        "model_config_hash",
        "selection",
        "artifact_checksum",
        "created_at",
        "policy_version",
        "train_cutoff",
        "files",
        "champion",
    ):
        assert manifest.get(key) not in (None, ""), key
    assert manifest["feature_version"] == FEATURE_VERSION
    assert manifest["code_revision"] == "abc1234" and manifest["is_synthetic"] is True
    assert manifest["model_version"].startswith("recovery-")
    assert set(manifest["files"]) == set(files)
    assert manifest["champion"] in report["selection"] and manifest["survival"]["converged"]
    assert report["assignment_audit"]["rows"] > 0


def test_training_is_reproducible(
    world: World, trained: tuple[dict[str, Any], dict[str, str], dict[str, Any]]
) -> None:
    again, _, _ = train(
        world.db,
        selection_cutoff=SEL,
        train_cutoff=CUT,
        cfg=load_model_config(),
        policy=load_policy(),
        gap_choices=GAPS,
        code_revision="abc1234",
        now=CUT + timedelta(hours=1),
    )
    assert again["model_version"] == trained[0]["model_version"]
    with pytest.raises(ValueError):
        train(
            world.db,
            selection_cutoff=CUT,
            train_cutoff=SEL,
            cfg=load_model_config(),
            policy=load_policy(),
            gap_choices=GAPS,
            code_revision="x",
        )


def test_save_load_and_integrity(
    tmp_path: Path, trained: tuple[dict[str, Any], dict[str, str], dict[str, Any]]
) -> None:
    manifest, files, _ = trained
    path = save_artifact(manifest, files, tmp_path)
    assert save_artifact(manifest, files, tmp_path) == path  # idempotent
    art = load_artifact(path)
    assert art.model_version == manifest["model_version"]
    assert (
        art.champion.name == manifest["champion"] and art.challenger.name == manifest["challenger"]
    )
    assert latest_artifact(tmp_path) == path and latest_artifact(tmp_path / "x") is None
    with pytest.raises(ArtifactError):
        art.model("nope")

    def corrupt(name: str, mutate: Any) -> None:
        bad = tmp_path / f"bad-{name}"
        shutil.copytree(path, bad)
        mutate(bad)
        with pytest.raises(ArtifactError):
            load_artifact(bad)

    corrupt("file", lambda p: (p / "survival.json").write_text("{}"))
    corrupt("missing", lambda p: (p / "classifier.txt").unlink())
    corrupt("json", lambda p: (p / "manifest.json").write_text("{"))
    corrupt("nomanifest", lambda p: (p / "manifest.json").unlink())

    def edit(field: str, value: object) -> Any:
        def go(p: Path) -> None:
            m = json.loads((p / "manifest.json").read_text())
            m[field] = value
            (p / "manifest.json").write_text(json.dumps(m))

        return go

    corrupt("checksum", edit("champion", "classifier_lgbm_isotonic"))
    corrupt("format", edit("artifact_format", 99))
    corrupt("features", edit("feature_version", "old"))
    resealed = finalise_manifest({**manifest, "champion": "nope"}, files)
    with pytest.raises(ArtifactError):
        load_artifact(save_artifact(resealed, files, tmp_path / "sealed"))


def test_cli_train(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = [
        "--db",
        str(world.db),
        "train",
        "--selection-cutoff",
        "2026-04-15",
        "--train-cutoff",
        "2026-05-05",
        "--gap-choices",
        "1,2,3,4,5,7,10",
        "--out",
        str(tmp_path / "models"),
        "--report",
        str(tmp_path / "train.json"),
    ]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{\n") :])  # after the one-line structured log
    assert Path(summary["artifact"]).is_dir()
    assert json.loads((tmp_path / "train.json").read_text())["champion"] == summary["champion"]
    args[1] = str(tmp_path / "missing.duckdb")
    assert cli.main(args) == 1
