"""Warehouse consumer: every event -> analytical store, batched, insert-if-absent.

The local analytical plane is DuckDB (ADR 0008). ``Warehouse.insert_events`` lands the batch
durably and compacts it with the batch loader's dedupe (ADR 0028), so duplicates and replays
never create a second row. Order is irrelevant for an append-only fact table.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb

from praxis.data.warehouse import Warehouse
from praxis.errors import TransientError
from praxis.events.codec import DecodedEvent
from praxis.streaming.topology import WAREHOUSE
from praxis.streaming.transport import Delivery


class WarehouseConsumer:
    name = WAREHOUSE

    def __init__(self, warehouse: Warehouse, batch_id: str = "stream") -> None:
        self.warehouse = warehouse
        self.batch_id = batch_id
        self.accepted = 0  # distinct ids per batch handed to the warehouse (durably landed)

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        return self.handle_batch([event])

    def handle_batch(self, events: Sequence[DecodedEvent]) -> str:
        rows = [e.envelope.model_dump(mode="json") for e in events]
        try:
            self.warehouse.insert_events(rows, self.batch_id)
        except duckdb.IOException as exc:  # file lock / disk: may clear up
            raise TransientError(f"warehouse unavailable: {type(exc).__name__}") from exc
        self.accepted += len(rows)
        return "applied"
