"""Phase 15 failures 1-2: external source timeout and malformed source payload.

Real boundary: a local HTTP server (``servers.py``) behind the production ``HttpFetcher``
(real httpx, real socket timeouts), the filesystem raw archive and a DuckDB warehouse FILE.
Detection runs the real ops path: the warehouse pull collector -> ``source_stale`` /
``data_contract_failure`` -> incidents (``OpsProbe``). Source: NESO carbon intensity (no
credential), freshness limit 48 h (configs/data/sources.toml).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from praxis.data.config import load_sources_config
from praxis.data.fetch import HttpFetcher, RetryPolicy
from praxis.data.ingest import IngestReport, IngestService, Outcome
from praxis.data.models import SourceId, TimeWindow
from praxis.data.raw_store import LocalRawStore
from praxis.data.sources import Source
from praxis.data.sources.carbon_intensity import CarbonIntensitySource
from praxis.data.warehouse import Warehouse
from praxis.ops.collect import warehouse_collector
from praxis.ops.system import SystemReader
from tests.resilience.harness import CRITERIA, Measured, OpsProbe, detection, judge, quiet_fired
from tests.resilience.servers import SourceServer, source_server

SOURCE = SourceId.CARBON_INTENSITY
LIMIT = timedelta(hours=load_sources_config().max_age_hours(SOURCE))
DAY1, DAY2, DAY3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)


def _day_of(path: str) -> date:
    # /intensity/2026-01-05T00:00Z/2026-01-06T00:00Z
    return date.fromisoformat(path.split("/")[2][:10])


def carbon_body(path: str) -> bytes:
    """48 measured half-hour slots of the requested day (a valid NESO response)."""
    start = datetime.combine(_day_of(path), datetime.min.time(), tzinfo=UTC)
    slots = []
    for i in range(48):
        a, b = start + timedelta(minutes=30 * i), start + timedelta(minutes=30 * (i + 1))
        slots.append({"from": a.strftime("%Y-%m-%dT%H:%MZ"), "to": b.strftime("%Y-%m-%dT%H:%MZ"),
                      "intensity": {"forecast": 150 + i, "actual": 151 + i,
                                    "index": "moderate"}})  # fmt: skip
    return json.dumps({"data": slots}).encode()


def drifted_body(path: str) -> bytes:
    """The provider renamed a field and changed its type: the contract is broken."""
    good = json.loads(carbon_body(path))
    for slot in good["data"]:
        slot["intensity"] = {"forecast": "n/a", "actual_gco2": slot["intensity"]["actual"]}
    return json.dumps(good).encode()


@pytest.fixture
def server() -> Iterator[SourceServer]:
    with source_server(carbon_body, drifted_body) as s:
        yield s


class World:
    """Warehouse file + raw archive; every ingest opens and closes the warehouse, so the
    ops collector (read-only) can open the same file between runs, as in production."""

    def __init__(self, tmp: Path, server: SourceServer, probe_clock: OpsProbe) -> None:
        self.path = tmp / "warehouse.duckdb"
        self.raw = LocalRawStore(tmp / "raw")
        self.server = server
        self.probe = probe_clock
        self.sleeps: list[float] = []
        with Warehouse(self.path) as wh:
            wh.migrate()

    def ingest(self, day: date) -> tuple[IngestReport, float]:
        carbon = CarbonIntensitySource(load_sources_config().carbon_intensity,
                                       base_url=self.server.base_url)  # fmt: skip
        sources: dict[SourceId, Source] = {SOURCE: carbon}
        policy = RetryPolicy(timeout_s=CRITERIA.run.fetch_timeout_s)
        with httpx.Client() as client, Warehouse(self.path) as wh:
            fetcher = HttpFetcher(client, policy, sleeper=self.sleeps.append)
            service = IngestService(sources, fetcher, self.raw, wh, clock=lambda: self.probe.now)
            started = datetime.now(UTC)
            report = service.ingest(SOURCE, TimeWindow(start=day, end=day))
            return report, (datetime.now(UTC) - started).total_seconds()

    def rows(self) -> list[tuple[object, ...]]:
        with Warehouse(self.path, read_only=True) as wh:
            return wh.con.execute(
                "SELECT record_key, value, batch_id FROM raw.external_signals ORDER BY 1"
            ).fetchall()

    def latest(self) -> datetime:
        with Warehouse(self.path, read_only=True) as wh:
            latest = wh.latest_observation(SOURCE, self.probe.now)
        assert latest is not None
        return latest


def _probe(tmp_path: Path) -> OpsProbe:
    path = tmp_path / "warehouse.duckdb"
    reader = SystemReader(None, {"warehouse": warehouse_collector(path, load_sources_config())})
    return OpsProbe(reader, start=datetime(2026, 1, 6, tzinfo=UTC))


def test_source_timeout(tmp_path: Path, server: SourceServer) -> None:
    fid = "source_timeout"
    probe = _probe(tmp_path)
    world = World(tmp_path, server, probe)
    assert world.ingest(DAY1)[0].count(Outcome.STORED) == 1
    probe.tick()
    baseline = world.rows()

    # Inject: the source accepts connections and never answers.
    server.mode = "stall"
    failed, elapsed_s = world.ingest(DAY2)
    attempts = server.count("stall")
    within_budget = probe.tick()  # newest observation still inside its freshness limit
    unchanged = world.rows() == baseline

    # The effect that must page: the newest observation passes the freshness limit.
    probe.now = world.latest() + LIMIT + timedelta(seconds=30) - probe.interval
    probe.mark()
    probe.tick()
    ticks, wall = detection(probe, ("source_stale",))

    # Recover: the source answers; the next window is stored and the alert clears.
    server.mode = "ok"
    recovered, _ = world.ingest(DAY3)
    clear = probe.until_clear(("source_stale",), max_ticks=5)
    after = world.rows()
    again, _ = world.ingest(DAY3)  # idempotent re-ingest: no duplicate rows
    lost = len(set(baseline) - set(after))
    new_rows = len(after) - len(baseline)

    record = judge(Measured(
        fid,
        actual=(f"ingest outcome {failed.results[0].outcome.value} after {attempts} attempts in "
                f"{elapsed_s:.2f} s; warehouse unchanged={unchanged}; alerts within the freshness "
                f"budget={sorted(a.rule_id for a in within_budget.health.alerts())}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=(recovered.count(Outcome.STORED) == 1 and unchanged
                   and failed.results[0].outcome is Outcome.UNAVAILABLE),
        recovery_actual=f"recovery ingest {recovered.summary()}; {new_rows} new rows",
        clear_ticks=clear, data_loss=lost,
        duplicate_side_effects=(len(world.rows()) - len(after)) + again.new_records,
        evidence={"attempts": attempts, "max_attempts": RetryPolicy().max_attempts,
                  "per_attempt_timeout_s": CRITERIA.run.fetch_timeout_s,
                  "backoff_s_requested": world.sleeps, "fetch_elapsed_s": round(elapsed_s, 3),
                  "freshness_limit_h": LIMIT.total_seconds() / 3600,
                  "alerts_within_budget": [a.rule_id for a in within_budget.health.alerts()],
                  "incidents_notified": [e for e, _ in probe.notifier.sent]},
    ))  # fmt: skip
    assert attempts == RetryPolicy().max_attempts
    assert within_budget.health.alerts() == []
    assert record.passed, record.reasons


def test_source_malformed(tmp_path: Path, server: SourceServer) -> None:
    fid = "source_malformed"
    probe = _probe(tmp_path)
    world = World(tmp_path, server, probe)
    assert world.ingest(DAY1)[0].count(Outcome.STORED) == 1
    probe.tick()
    baseline = world.rows()

    # Inject: the source changes its schema.
    server.mode = "drift"
    probe.mark()
    drifted, _ = world.ingest(DAY2)
    probe.tick()
    ticks, wall = detection(probe, ("data_contract_failure",))
    quarantined = [m for m in world.raw.iter_meta(SOURCE) if m.quarantine_reason]
    (meta,) = quarantined
    archived = world.raw.read_body(meta)
    raw_kept = hashlib.sha256(archived).hexdigest() == meta.checksum_sha256
    raw_is_source_body = archived == drifted_body(meta.request.url.removeprefix(server.base_url))
    unchanged = world.rows() == baseline

    # Recover: a valid body is stored; the alert clears once the quarantine leaves the window.
    server.mode = "ok"
    fixed, _ = world.ingest(DAY2)
    probe.now = meta.retrieved_at + timedelta(hours=24)
    clear = probe.until_clear(("data_contract_failure",), max_ticks=5)
    after = world.rows()
    lost = len(set(baseline) - set(after))

    record = judge(Measured(
        fid,
        actual=(f"ingest outcome {drifted.results[0].outcome.value}: {drifted.results[0].detail}; "
                f"raw archived with checksum={raw_kept}; normalised rows unchanged={unchanged}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=(drifted.results[0].outcome is Outcome.QUARANTINED and raw_kept
                   and raw_is_source_body and unchanged and fixed.count(Outcome.STORED) == 1),
        recovery_actual=f"valid body: {fixed.summary()}; alert cleared after the 24 h window",
        clear_ticks=clear, data_loss=lost + (0 if raw_kept and raw_is_source_body else 1),
        duplicate_side_effects=0 if len(after) == len(baseline) + fixed.new_records else 1,
        evidence={"quarantine_reason": meta.quarantine_reason, "batch_id": meta.batch_id,
                  "checksum_sha256": meta.checksum_sha256,
                  "incidents_notified": [e for e, _ in probe.notifier.sent]},
    ))  # fmt: skip
    assert record.passed, record.reasons
