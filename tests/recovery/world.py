"""A small real recovery world: simulator -> the three marts the recovery code reads.

The marts are built directly from the simulator's events with the same definitions as the
dbt models (``dim_customer``, ``fct_invoices``, ``fct_payments``), so the train / evaluate path
runs end to end in seconds without dbt. Session-scoped: built once per test run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from praxis.simulator.config import SimulationConfig, load_config
from praxis.simulator.runner import run_simulation

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "configs/simulator/scenarios/recovery_eval.toml"
SEED = 5  # test-only world; never the development (1) or held-out (42) seed
CUSTOMERS = 1200

_DDL = """
CREATE SCHEMA marts;
CREATE TABLE marts.dim_customer (customer_id TEXT, preferred_payment_method TEXT,
    is_existing BOOLEAN, tenure_days_at_start BIGINT, created_at TIMESTAMPTZ);
CREATE TABLE marts.fct_invoices (invoice_id TEXT, customer_id TEXT, invoiced_at TIMESTAMPTZ,
    amount_minor BIGINT, tier TEXT);
CREATE TABLE marts.fct_payments (invoice_id TEXT, attempt_number BIGINT, outcome TEXT,
    failure_reason TEXT, resolved_at TIMESTAMPTZ);
"""


@dataclass(frozen=True)
class World:
    config: SimulationConfig
    seed: int
    sim_dir: Path
    db: Path


def build_world(root: Path, customers: int = CUSTOMERS, seed: int = SEED) -> World:
    config = load_config(scenario=SCENARIO).with_overrides(n_customers=customers)
    sim_dir = root / "sim"
    run_simulation(config, seed, out_dir=sim_dir, validate=False, schema_every=10**9)
    customers_rows, invoices, payments = [], [], []
    with (sim_dir / "events.ndjson").open() as fh:
        for line in fh:
            e = json.loads(line)
            p, t = e["payload"], e["event_type"]
            if t == "customer.created":
                customers_rows.append(
                    (
                        e["entity_id"],
                        p["preferred_payment_method"],
                        p["is_existing"],
                        p["tenure_days"],
                        e["occurred_at"],
                    )
                )
            elif t == "invoice.created":
                invoices.append(
                    (
                        p["invoice_id"],
                        e["entity_id"],
                        e["occurred_at"],
                        p["amount_minor"],
                        p["tier"],
                    )
                )
            elif t in ("payment.succeeded", "payment.failed"):
                payments.append(
                    (
                        p["invoice_id"],
                        p["attempt_number"],
                        t.split(".")[1],
                        p.get("reason"),
                        e["occurred_at"],
                    )
                )
    db = root / "warehouse.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute(_DDL)
        con.executemany("INSERT INTO marts.dim_customer VALUES (?, ?, ?, ?, ?)", customers_rows)
        con.executemany("INSERT INTO marts.fct_invoices VALUES (?, ?, ?, ?, ?)", invoices)
        con.executemany("INSERT INTO marts.fct_payments VALUES (?, ?, ?, ?, ?)", payments)
    return World(config, seed, sim_dir, db)
