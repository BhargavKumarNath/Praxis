"""The live world is prefix-stable (ADR 0027): a longer run reproduces every earlier day.

This is what makes a daily rebuild safe: day d's events never change once observed, so the
monitoring and training history of earlier ticks stays valid. Event / trace / correlation
ids are scoped to a run (they hash the config, which includes the run length), so they are
excluded; every other field must be identical.
"""

from __future__ import annotations

import json
from datetime import timedelta

from praxis.live.config import load_live_config
from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import generate_population

RUN_SCOPED = {"event_id", "correlation_id", "trace_id", "causation_id"}
CFG = load_live_config()


def _events(days: int) -> list[str]:
    cfg = load_config(scenario=CFG.world.scenario_path).with_overrides(days=days, n_customers=60)
    pop = generate_population(cfg, CFG.world.seed)
    return [
        json.dumps({k: v for k, v in e.items() if k not in RUN_SCOPED}, sort_keys=True)
        for e in Engine(cfg, CFG.world.seed, pop).run()
    ]


def test_a_longer_world_keeps_every_earlier_day() -> None:
    short, longer = _events(45), _events(46)
    cut = (CFG.world.start_date + timedelta(days=45)).isoformat()
    head = [e for e in longer if json.loads(e)["occurred_at"] < cut]
    assert len(short) > 1000
    assert all(json.loads(e)["occurred_at"] < cut for e in short)  # nothing from the future
    assert head == short
    assert len(longer) > len(short)  # the new day has events of its own
