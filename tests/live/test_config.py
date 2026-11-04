"""The live loop and its world are pre-registered (ADR 0027): pinned by hash, checked on load."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from praxis.live.config import REPO, LiveConfig, load_live_config

# Pinned 2026-11-04, before any live tick. Changing either file after a tick changes the
# world the loop already observed (or its policy): it needs an ADR and the owner's approval.
PINNED = {
    "configs/live/live.toml": "2fb4e7ca3457b81bd59d2eb29f092ded5062f65063840ccb5b62a7f242987629",
    "configs/simulator/scenarios/live.toml": (
        "72ff853b2d7ae3c8b2632bf0ae18a1b19c676595736b24fec04bc57ca837978c"
    ),
}


@pytest.mark.parametrize("path", sorted(PINNED))
def test_preregistered_files_are_pinned(path: str) -> None:
    digest = hashlib.sha256((REPO / path).read_bytes()).hexdigest()
    assert digest == PINNED[path], f"{path} changed: needs an ADR + owner approval"


def test_config_loads() -> None:
    cfg = load_live_config()
    assert cfg.world.seed == 9001 and cfg.world.customers == 1000
    assert cfg.world.scenario_path.exists()
    assert cfg.bootstrap.train_through.isoformat() == "2026-08-30"
    # >= the lifecycle policy's min_training_days (112) of history for the first champion
    assert (cfg.bootstrap.train_through - cfg.world.start_date).days + 1 >= 112
    assert cfg.tick.review_ready_shadow is True


def test_start_date_must_match_the_scenario(tmp_path: Path) -> None:
    text = (REPO / "configs/live/live.toml").read_text()
    bad = tmp_path / "live.toml"
    bad.write_text(text.replace('start_date = "2026-04-06"', 'start_date = "2026-04-07"'))
    with pytest.raises(ValueError, match="differs from the scenario"):
        load_live_config(bad)
    absolute = tmp_path / "abs.toml"
    absolute.write_text(text.replace('"configs/simulator', f'"{REPO}/configs/simulator'))
    assert load_live_config(absolute).world.scenario_path.is_absolute()


def test_bootstrap_must_follow_the_world_start() -> None:
    raw = load_live_config().model_dump()
    raw["bootstrap"]["train_through"] = raw["world"]["start_date"]
    with pytest.raises(ValidationError, match="train_through"):
        LiveConfig.model_validate(raw)
