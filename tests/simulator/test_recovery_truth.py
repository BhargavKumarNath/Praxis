"""Phase 8 recovery ground truth: events agree with the latent cure times; worlds without it
are unchanged (hash-neutral); configuration is validated."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine, RecoveryTruth
from praxis.simulator.population import generate_population

SCENARIO = Path(__file__).resolve().parents[2] / "configs/simulator/scenarios/recovery_eval.toml"


@pytest.fixture(scope="module")
def run() -> tuple[list[dict[str, Any]], dict[str, RecoveryTruth]]:
    world = load_config(scenario=SCENARIO).with_overrides(n_customers=600, days=90)
    truth: dict[str, RecoveryTruth] = {}
    engine = Engine(
        world,
        9,
        generate_population(world, 9),
        recovery_observer=lambda t: truth.__setitem__(t.invoice_id, t),
    )
    events = [e for e in engine.run() if e["event_type"].startswith("payment.")]
    return events, truth


def test_retry_outcomes_follow_the_latent_cure_time(
    run: tuple[list[dict[str, Any]], dict[str, RecoveryTruth]],
) -> None:
    events, truth = run
    by_invoice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e["event_type"] != "payment.attempted":
            by_invoice[e["payload"]["invoice_id"]].append(e)
    checked = 0
    for invoice, results in by_invoice.items():
        first = next(r for r in results if r["payload"]["attempt_number"] == 1)
        if first["event_type"] == "payment.succeeded":
            assert invoice not in truth
            continue
        t = truth[invoice]
        assert t.reason == first["payload"]["reason"]
        previous = _day(first)
        for r in sorted(results, key=lambda r: r["payload"]["attempt_number"]):
            n = r["payload"]["attempt_number"]
            if n == 1:
                continue
            assert _day(r) - previous in (1, 2, 3, 4, 5, 7, 10)  # randomised gap design
            previous = _day(r)
            elapsed = _day(r) - _day(first)
            assert (r["event_type"] == "payment.succeeded") == (t.cured and elapsed >= t.cure_days)
            if r["event_type"] == "payment.failed":
                assert r["payload"]["reason"] == t.reason  # the cause persists
            checked += 1
    assert checked > 50 and truth


def _day(event: dict[str, Any]) -> int:
    from datetime import datetime

    return (
        datetime.fromisoformat(event["occurred_at"])
        - datetime.fromisoformat("2026-01-05T00:00:00+00:00")
    ).days


def test_truth_curve_is_a_cure_mixture(
    run: tuple[list[dict[str, Any]], dict[str, RecoveryTruth]],
) -> None:
    _, truth = run
    t = next(iter(truth.values()))
    assert t.collectible_by(0) == 0.0
    assert 0 < t.collectible_by(1000) <= t.cure_prob + 1e-12
    assert t.collectible_by(3) <= t.collectible_by(7)


def test_default_world_is_untouched_by_the_recovery_section() -> None:
    base = load_config()
    assert base.billing.recovery is None
    assert "recovery" not in base.canonical_json()
    assert load_config(scenario=SCENARIO).config_hash != base.config_hash


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("reasons",),
            {"card_declined": {"cure_logit": 0, "shape": 1, "scale_days": 1}},
            "every failure reason",
        ),
        (("gap_choices_days",), [1, 1, 2], "gap choices"),
        (("gap_choices_days",), [1, 20], "inside the billing period"),
    ],
)
def test_recovery_configuration_is_validated(
    path: tuple[str, ...], value: object, message: str
) -> None:
    raw = load_config(scenario=SCENARIO).model_dump(mode="json")
    raw["billing"]["recovery"][path[0]] = value
    with pytest.raises(ValidationError, match=message):
        type(load_config()).model_validate(raw)
