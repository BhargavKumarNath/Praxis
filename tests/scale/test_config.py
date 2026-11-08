"""configs/scale/slo.toml is pre-registered (Phase 14, ADR 0022): workload, seeds and every
SLO are pinned by hash. Changing them needs an ADR and the owner's approval. Shard counts and
other per-step implementation knobs are not pinned (a change since 100K is recorded in the
evidence instead). Re-pinned once on 2026-10-31 when the pin was narrowed from "every step" to
"gated steps" after a development step was added; no SLO or workload value changed. Re-pinned
on 2026-11-05 (ADR 0028) to ADD the M1-budget ladder (m1-100k .. m1-1m) before any of its
runs; the Phase 14 steps, workload, http and payments are unchanged
(`test_phase14_registration_is_unchanged`)."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from praxis.streaming.scale import load_scale_config

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "scale" / "slo.toml"
PINNED_SHA256 = "9337e30ed972e92aabc568c978d58c356e107bd3992e09091cab2b1b075ab571"
PHASE14_SHA256 = "eb385f0a07c4f47029150b5e6d9923f66500bb254161e26424f3892b1e8244e9"
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


def test_phase14_registration_is_unchanged() -> None:
    part = _pinned_part()
    part["gated"] = {k: v for k, v in part["gated"].items() if not k.startswith("m1-")}  # type: ignore[attr-defined]
    assert hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest() == PHASE14_SHA256


def test_m1_ladder_gates_correctness_inside_the_budget() -> None:
    for step, n in (("m1-100k", 100_000), ("m1-250k", 250_000), ("m1-500k", 500_000),
                    ("m1-1m", 1_000_000)):  # fmt: skip
        run, slo = load_scale_config(CONFIG, step)
        assert run.customers == n and run.sim_seed == 1 and run.fault_seed == 701
        assert slo["max_cgroup_mem_peak_mb"] == 4096 and slo["max_cgroup_swap_peak_mb"] == 0
        assert slo["max_error_rate"] == 0.0 and run.oracle_lean


def test_gated_steps_have_slos_and_no_held_out_seed() -> None:
    for step in ("100k", "1m"):
        run, slo = load_scale_config(CONFIG, step)
        assert slo["max_error_rate"] == 0.0 and slo["min_events_per_s"] > 0
        assert run.sim_seed == 1 and run.fault_seed >= 701
        assert run.sim_seed not in HELD_OUT and run.fault_seed not in HELD_OUT
    assert load_scale_config(CONFIG, "1m")[0].customers == 1_000_000
