"""Warehouse consumer: every event -> analytical store, batched, insert-if-absent.

The local analytical plane is DuckDB (ADR 0008); the insert uses the same
``ON CONFLICT (event_id) DO NOTHING`` statement as the batch loader, so duplicates and
replays never create a second row. Order is irrelevant for an append-only fact table.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb

from praxis.data.warehouse import Warehouse
from praxis.events.codec import DecodedEvent
from praxis.streaming.topology import WAREHOUSE
from praxis.streaming.transport import Delivery, TransientError


class WarehouseConsumer:
    name = WAREHOUSE

    def __init__(self, warehouse: Warehouse, batch_id: str = "stream") -> None:
        self.warehouse = warehouse
        self.batch_id = batch_id
        self.inserted = 0
        self.duplicates = 0

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        return self.handle_batch([event])

    def handle_batch(self, events: Sequence[DecodedEvent]) -> str:
        rows = [e.envelope.model_dump(mode="json") for e in events]
        try:
            new = self.warehouse.insert_events(rows, self.batch_id)
        except duckdb.IOException as exc:  # file lock / disk: may clear up
            raise TransientError(f"warehouse unavailable: {type(exc).__name__}") from exc
        self.inserted += new
        self.duplicates += len(rows) - new
        return "applied"
