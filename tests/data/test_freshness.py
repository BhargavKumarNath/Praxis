from __future__ import annotations

from datetime import datetime, timedelta

from praxis.data.freshness import FreshnessState, check_freshness
from praxis.data.models import SignalRecord, SourceId, make_record_key
from praxis.data.warehouse import Warehouse
from tests.data.helpers import NOW, cfg


def _put(wh: Warehouse, source: SourceId, observed: datetime) -> None:
    wh.upsert_signals(
        [
            SignalRecord(
                record_key=make_record_key(source, "s", "e", "m", observed),
                source=source,
                series_id="s",
                entity_id="e",
                metric="m",
                unit="u",
                observed_at=observed,
                value=1.0,
                batch_id="b",
                retrieved_at=NOW,
            )
        ]
    )


def _states(wh: Warehouse) -> dict[SourceId, FreshnessState]:
    return {r.source: r.state for r in check_freshness(wh, cfg(), NOW)}


def test_empty_warehouse_is_missing_for_every_source() -> None:
    wh = Warehouse()
    wh.migrate()
    assert set(_states(wh).values()) == {FreshnessState.MISSING}


def test_stale_fresh_and_exact_boundary() -> None:
    wh = Warehouse()
    wh.migrate()
    limit = timedelta(hours=cfg().max_age_hours(SourceId.CARBON_INTENSITY))
    _put(wh, SourceId.CARBON_INTENSITY, NOW - limit)  # exactly at the limit: fresh
    _put(
        wh,
        SourceId.EIA,
        NOW - timedelta(hours=cfg().max_age_hours(SourceId.EIA)) - timedelta(seconds=1),
    )
    _put(wh, SourceId.OPEN_METEO, NOW - timedelta(hours=1))
    states = _states(wh)
    assert states[SourceId.CARBON_INTENSITY] is FreshnessState.FRESH
    assert states[SourceId.EIA] is FreshnessState.STALE
    assert states[SourceId.OPEN_METEO] is FreshnessState.FRESH
    assert states[SourceId.FRED] is FreshnessState.MISSING


def test_forecast_slots_in_the_future_do_not_hide_staleness() -> None:
    wh = Warehouse()
    wh.migrate()
    _put(wh, SourceId.CARBON_INTENSITY, NOW - timedelta(days=30))
    _put(wh, SourceId.CARBON_INTENSITY, NOW + timedelta(hours=6))  # future forecast slot
    assert _states(wh)[SourceId.CARBON_INTENSITY] is FreshnessState.STALE


def test_every_source_has_a_policy() -> None:
    wh = Warehouse()
    wh.migrate()
    assert {r.source for r in check_freshness(wh, cfg(), NOW)} == set(SourceId)
