"""Training / serving parity: warehouse features == control-plane features, same episodes.

The model trains on features built from the dbt marts and is served from features built from
the Postgres event log. Both feed ``episode_features``; this proves the two extractions agree
on a real simulated world (any skew would silently degrade every live decision).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from praxis.control.store import ControlPlaneStore, EventRecord
from praxis.domain.dunning import DunningState
from praxis.dunning.service import features_for
from praxis.dunning.store import Case, DunningRepo
from praxis.recovery.dataset import build_episodes, failed_between
from praxis.recovery.warehouse import load_histories
from tests.recovery.world import World

pytestmark = [pytest.mark.slow, pytest.mark.integration]
START = datetime(2026, 1, 5, tzinfo=UTC)
TYPES = {
    "customer.created",
    "conversion.observed",
    "subscription.started",
    "invoice.created",
    "payment.attempted",
    "payment.failed",
    "payment.succeeded",
    "churn.observed",
    "subscription.changed",
}


def test_warehouse_and_control_plane_features_agree(
    world: World, store: ControlPlaneStore, pg_engine: Engine
) -> None:
    episodes = failed_between(
        build_episodes(load_histories(world.db, START + timedelta(days=180))),
        START + timedelta(days=60),
        START + timedelta(days=150),
    )[:40]
    wanted = {e.customer_id for e in episodes}
    with (world.sim_dir / "events.ndjson").open() as fh:
        for line in fh:
            e = json.loads(line)
            if e["entity_id"] in wanted and e["event_type"] in TYPES:
                store.apply_event(
                    "operational",
                    EventRecord(
                        e["event_id"],
                        e["event_type"],
                        e["entity_id"],
                        datetime.fromisoformat(e["occurred_at"]),
                        e["payload"],
                        e["trace_id"],
                        e["correlation_id"],
                        e.get("causation_id"),
                    ),
                )
    assert len(episodes) == 40
    with pg_engine.connect() as conn:
        repo = DunningRepo(conn)
        for ep in episodes:
            case = Case(
                ep.invoice_id,
                ep.customer_id,
                None,
                DunningState.PAST_DUE,
                ep.amount_minor,
                "GBP",
                ep.features.reason,
                ep.failed_at,
                1,
                ep.failed_at,
            )
            assert features_for(repo, case) == ep.features, ep.invoice_id
