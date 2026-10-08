from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from praxis.recovery.artifact import (
    RecoveryArtifact,
    finalise_manifest,
    load_artifact,
    model_files,
    save_artifact,
)
from praxis.recovery.classifier import CalibratedClassifier
from praxis.recovery.config import load_model_config, load_policy
from praxis.recovery.survival import CureWeibull
from praxis.recovery.train import fit_all
from tests.recovery.helpers import episodes
from tests.recovery.world import World, build_world

CREATED = datetime(2026, 5, 5, tzinfo=UTC)


def build_artifact(
    root: Path, champion: str = CureWeibull.name, created: datetime = CREATED
) -> Path:
    fitted = fit_all(episodes(3000, seed=31), load_model_config())
    files = model_files(fitted.survival, fitted.classifier, fitted.table)
    challenger = CalibratedClassifier.name if champion == CureWeibull.name else CureWeibull.name
    core = {
        "champion": champion,
        "challenger": challenger,
        "created_at": created.isoformat(),
        "code_revision": "test",
        "data_version": "recovery-data-test",
        "policy_version": load_policy().version,
        "model_config": load_model_config().model_dump(mode="json"),
        "model_config_hash": load_model_config().config_hash,
        "selection_cutoff": "2026-04-15T00:00:00+00:00",
        "train_cutoff": "2026-05-05T00:00:00+00:00",
    }
    return save_artifact(finalise_manifest(core, files), files, root)


@pytest.fixture(scope="session")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_artifact(tmp_path_factory.mktemp("recovery-models"))


@pytest.fixture(scope="session")
def artifact(artifact_dir: Path) -> RecoveryArtifact:
    return load_artifact(artifact_dir)


@pytest.fixture(scope="session")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    """A small real recovery world (simulator -> marts), shared by the slow tests."""
    return build_world(tmp_path_factory.mktemp("recovery-world"))
