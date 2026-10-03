"""Shared fixtures for simulator tests."""

from __future__ import annotations

import pytest

from tests.simulator.sim_helpers import Collected, collect, scenario


@pytest.fixture(scope="module")
def base() -> Collected:
    return collect(scenario())
