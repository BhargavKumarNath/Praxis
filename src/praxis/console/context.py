"""What every console reader needs: the stores, the fleet reader and a clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from praxis.console.panels import NotConfigured
from praxis.ops.config import OpsConfig, load_ops_config
from praxis.ops.system import SystemReader


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ConsoleContext:
    engine: Engine | None
    warehouse_path: Path | None
    reader: SystemReader
    ops: OpsConfig = field(default_factory=load_ops_config)
    clock: Callable[[], datetime] = _utc_now

    def db(self) -> Engine:
        if self.engine is None:
            raise NotConfigured("PRAXIS_DATABASE_URL is not set")
        return self.engine

    def warehouse(self) -> Path:
        if self.warehouse_path is None or not self.warehouse_path.exists():
            raise NotConfigured("the warehouse file does not exist")
        return self.warehouse_path
