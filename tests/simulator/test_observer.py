"""Read-only day observer (Phase 6 counterfactual truth): it sees the engine's own equations
and can never change the world."""

from __future__ import annotations

import numpy as np

from praxis.simulator.engine import DayView, Engine
from praxis.simulator.population import generate_population
from tests.simulator.sim_helpers import SEED, scenario


def _run(observer: bool) -> tuple[list[dict[str, object]], list[DayView], Engine]:
    cfg = scenario(n_customers=400, days=21)
    views: list[DayView] = []
    engine = Engine(cfg, SEED, generate_population(cfg, SEED), views.append if observer else None)
    return list(engine.run()), views, engine


def test_observing_never_changes_the_event_stream() -> None:
    plain, _, _ = _run(observer=False)
    observed, views, _ = _run(observer=True)
    assert observed == plain
    assert [v.day for v in views] == list(range(21))


def test_expected_demand_matches_realised_demand() -> None:
    """Sum of E[requested] equals realised requested units within sampling error."""
    events, views, engine = _run(observer=True)
    expected = sum(float(engine.expected_demand(v, v.prices).sum()) for v in views)
    realised = 0
    for e in events:
        if e["event_type"] == "usage.observed":
            payload = e["payload"]
            assert isinstance(payload, dict)
            realised += int(payload["units"]) + int(payload["throttled_units"])
    np.testing.assert_allclose(realised, expected, rtol=0.03)


def test_counterfactual_prices_follow_the_latent_elasticity_exactly() -> None:
    _, views, engine = _run(observer=True)
    v = views[10]
    doubled = v.prices.copy()
    doubled[:, 0] *= 2
    base, alt = engine.expected_demand(v, v.prices), engine.expected_demand(v, doubled)
    active = v.active
    np.testing.assert_allclose(
        alt[active, 0], base[active, 0] * 2.0 ** engine.pop.elasticity[active], rtol=1e-12
    )
    np.testing.assert_array_equal(alt[:, 1:], base[:, 1:])  # own-price only
    assert not np.any(base[~active])  # inactive customers demand nothing


def test_churn_hazard_rises_with_price_and_respects_the_cap() -> None:
    _, views, engine = _run(observer=True)
    v = views[10]
    raised = (v.prices * 1.5).astype(np.int64)
    base, alt = engine.churn_hazard(v, v.prices), engine.churn_hazard(v, raised)
    assert np.all(alt >= base) and np.any(alt > base)
    assert np.all(alt <= engine.cfg.behaviour.churn_hazard_cap)
    assert v.unit_cost.shape == (len(engine.region_ids), len(engine.prod_ids))
    assert np.all((v.served_ratio >= 0) & (v.served_ratio <= 1))
