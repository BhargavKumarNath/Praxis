"""Training panel: point-in-time snapshots with their churn labels (non-overlapping windows).

Snapshot dates are aligned to the END of the training range: the last window ends exactly at
the cutoff (the first day not available for training), earlier windows step back by
``snapshot_step_days`` while a full feature history exists. Each row = one customer active at
one snapshot date: features from before the date, label from the ``window_days`` after it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from datetime import date, timedelta

import duckdb
import numpy as np
from numpy.typing import NDArray

from praxis.customer.config import CustomerValueConfig, Label
from praxis.customer.features import feature_matrix
from praxis.customer.warehouse import load_outcomes, load_snapshot

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]


def snapshot_dates(start: date, cutoff: date, cfg: Label) -> list[date]:
    """Snapshot dates T with T >= start + first_snapshot_day and T + window <= cutoff (sorted)."""
    first = start + timedelta(days=cfg.first_snapshot_day)
    out: list[date] = []
    t = cutoff - timedelta(days=cfg.window_days)
    while t >= first:
        out.append(t)
        t -= timedelta(days=cfg.snapshot_step_days)
    return sorted(out)


@dataclass(frozen=True)
class Panel:
    dates: tuple[date, ...]  # snapshot dates, sorted
    window: I64  # (rows,) index into ``dates``
    ids: tuple[str, ...]
    tiers: tuple[str, ...]
    x: F64  # (rows, features) raw features
    exposure: F64  # days at risk in the label window
    voluntary: F64
    involuntary: F64

    @property
    def n(self) -> int:
        return len(self.ids)

    def subset(self, mask: NDArray[np.bool_]) -> Panel:
        idx = np.flatnonzero(mask)
        return Panel(
            self.dates,
            self.window[idx],
            tuple(self.ids[i] for i in idx),
            tuple(self.tiers[i] for i in idx),
            self.x[idx],
            self.exposure[idx],
            self.voluntary[idx],
            self.involuntary[idx],
        )

    def data_version(self) -> str:
        h = hashlib.sha256()
        h.update(",".join(d.isoformat() for d in self.dates).encode())
        h.update("\n".join(self.ids).encode())
        h.update("\n".join(self.tiers).encode())
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, np.ndarray):
                h.update(f.name.encode())
                h.update(np.ascontiguousarray(v).tobytes())
        return "customers-" + h.hexdigest()[:16]


def build_panel(
    con: duckdb.DuckDBPyConnection, dates: list[date], cfg: CustomerValueConfig
) -> Panel:
    if not dates:
        raise ValueError("no snapshot dates: the training range is shorter than one window")
    ids: list[str] = []
    tiers: list[str] = []
    window, xs, exposure, vol, inv = [], [], [], [], []
    for k, t in enumerate(dates):
        snap = load_snapshot(con, t, cfg.features)
        out = load_outcomes(con, snap.ids, t, cfg.label.window_days)
        ids += snap.ids
        tiers += snap.tier
        window.append(np.full(snap.n, k, dtype=np.int64))
        xs.append(feature_matrix(snap, cfg.features.trend_half_days))
        exposure.append(out.exposure_days)
        vol.append(out.voluntary)
        inv.append(out.involuntary)
    return Panel(
        tuple(dates),
        np.concatenate(window),
        tuple(ids),
        tuple(tiers),
        np.vstack(xs),
        np.concatenate(exposure),
        np.concatenate(vol),
        np.concatenate(inv),
    )
