"""configs/scale/slo.toml is pre-registered (Phase 14, ADR 0022): workload, seeds and every
SLO are pinned by hash. Changing them needs an ADR and the owner's approval. Shard counts and
other per-step implementation knobs are not pinned (a change since 100K is recorded in the
evidence instead). Re-pinned once on 2026-10-31 when the pin was narrowed from "every step" to
"gated steps" after a development step was added; no SLO or workload value changed."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from praxis.streaming.scale import load_scale_config

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "scale" / "slo.toml"
PINNED_SHA256 = "eb385f0a07c4f47029150b5e6d9923f66500bb254161e26424f3892b1e8244e9"
HELD_OUT = {42, 43, 44, 45, 201, 202, 203, 301, 302, 303, 601, 621}


def _pinned_part() -> dict[str, object]:
    cfg = tomllib.loads(CONFIG.read_text())
    return {
        "workload": cfg["workload"],
        # Gated steps only (those with an SLO); development steps calibrate and may be added.
        "gated": {
            name: {"customers": step["customers"], "slo": step["slo"]}
            for name, step in sorted(cfg["scales"].items())
            if "slo" in step
        },
        "http": cfg["http"],
        "payments": cfg["payments"],
    }


def test_workload_and_slos_are_pinned() -> None:
    body = json.dumps(_pinned_part(), sort_keys=True).encode()
    assert hashlib.sha256(body).hexdigest() == PINNED_SHA256


def test_gated_steps_have_slos_and_no_held_out_seed() -> None:
    for step in ("100k", "1m"):
        run, slo = load_scale_config(CONFIG, step)
        assert slo["max_error_rate"] == 0.0 and slo["min_events_per_s"] > 0
        assert run.sim_seed == 1 and run.fault_seed >= 701
        assert run.sim_seed not in HELD_OUT and run.fault_seed not in HELD_OUT
    assert load_scale_config(CONFIG, "1m")[0].customers == 1_000_000
