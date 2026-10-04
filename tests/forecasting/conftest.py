from __future__ import annotations

from pathlib import Path

import pytest

from praxis.forecasting.artifact import (
    ForecastArtifact,
    load_artifact,
    save_artifact,
    train_artifact,
)
from tests.forecasting.helpers import make_panel, make_plan, small_config

N_DAYS = 112


@pytest.fixture(scope="session")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    manifest, files = train_artifact(
        make_panel(n_days=N_DAYS), make_plan(), small_config(), code_revision="test-rev"
    )
    return save_artifact(manifest, files, tmp_path_factory.mktemp("models"))


@pytest.fixture(scope="session")
def artifact(artifact_dir: Path) -> ForecastArtifact:
    return load_artifact(artifact_dir)
