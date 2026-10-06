from __future__ import annotations

import pytest

from praxis.elasticity.analysis import Analysis, analyse
from praxis.elasticity.config import ElasticityConfig
from tests.elasticity.helpers import Defects, make_extract, small_config


@pytest.fixture(scope="session")
def cfg() -> ElasticityConfig:
    return small_config()


@pytest.fixture(scope="session")
def analysis(cfg: ElasticityConfig) -> Analysis:
    """Full analysis (incl. three PyMC fits) of a clean synthetic world with known truth."""
    return analyse(make_extract(3000, seed=11), cfg)


@pytest.fixture(scope="session")
def contaminated_analysis(cfg: ElasticityConfig) -> Analysis:
    return analyse(make_extract(3000, seed=12, defects=Defects(contamination=0.2)), cfg)
