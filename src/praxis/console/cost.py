"""The cloud cost ESTIMATE (``configs/console/cost.toml``). Not a metered cost: Praxis reads no
billing data, so the console shows this as an estimate with its sources and date."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "console" / "cost.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CostItem(_Cfg):
    resource: str = Field(min_length=1)
    state: str = Field(min_length=1)
    monthly_gbp: float | None = Field(default=None, ge=0)
    basis: str = Field(min_length=1)


class CostEstimate(_Cfg):
    version: str = Field(min_length=1)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    kind: Literal["estimate"]
    currency: Literal["GBP"]
    budget_note: str
    sources: list[str] = Field(min_length=1)
    item: list[CostItem] = Field(min_length=1)

    def as_dict(self) -> dict[str, Any]:
        known = [i.monthly_gbp for i in self.item if i.monthly_gbp is not None]
        unestimated = [i.resource for i in self.item if i.monthly_gbp is None]
        return {
            "kind": self.kind,
            "label": "ESTIMATE, not a metered cost: no billing data is read",
            "version": self.version,
            "as_of": self.as_of,
            "currency": self.currency,
            "monthly_estimate_gbp": sum(known),
            "not_estimated": unestimated,
            "budget_note": self.budget_note,
            "sources": self.sources,
            "items": [i.model_dump() for i in self.item],
        }


def load_cost_estimate(path: Path | None = None) -> CostEstimate:
    return CostEstimate.model_validate(tomllib.loads((path or CONFIG).read_text()))
