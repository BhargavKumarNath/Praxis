"""Phase 14 scale harness (ADR 0022): sharding, streaming oracle, sharded replay, SLOs."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from praxis.control.store import ControlPlaneStore
from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import generate_population
from praxis.streaming.__main__ import main
from praxis.streaming.pipeline import expected_state, oracle_state
from praxis.streaming.scale import (
    ORACLE,
    ScaleRun,
    _DayOracle,
    build_archive,
    error_rate,
    evaluate_http,
    evaluate_slo,
    load_manifest,
    load_scale_config,
    merge_latency,
    read_shard,
    run_scale,
    run_shard,
    shard_file,
    shard_of,
    shard_prefix,
    weighted_quantiles,
)

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "scale" / "slo.toml"
RUN = ScaleRun(customers=40, days=7, sim_seed=1, fault_seed=701, shards=2)


def _events(customers: int = 40, days: int = 7) -> list[dict[str, Any]]:
    cfg = load_config().with_overrides(n_customers=customers, days=days)
    return list(Engine(cfg, 1, generate_population(cfg, 1)).run())


def test_shard_of_is_deterministic_in_range_and_spread() -> None:
    ids = [f"evt-{i}" for i in range(4000)]
    shards = [shard_of(i, 8) for i in ids]
    assert shards == [shard_of(i, 8) for i in ids]
    counts = Counter(shards)
    assert set(counts) == set(range(8))
    assert max(counts.values()) < 1.2 * min(counts.values())
    assert shard_prefix(3) == "praxis-scale-s03-"


def test_per_day_oracle_equals_the_global_sort_oracle() -> None:
    events = _events()
    oracle = _DayOracle(load_config().billing.max_attempts)
    for e in events:
        oracle.add(e)
    oracle.flush()
    assert oracle_state(oracle.validator) == expected_state(
        events, load_config().billing.max_attempts
    )


def test_per_day_oracle_rejects_a_day_going_backwards() -> None:
    oracle = _DayOracle(4)
    oracle.add({"occurred_at": "2026-01-02T00:00:00Z"})
    with pytest.raises(ValueError, match="backwards"):
        oracle.add({"occurred_at": "2026-01-01T23:00:00Z"})


def test_scale_run_rejects_empty_dimensions() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        ScaleRun(customers=0, days=1, sim_seed=1, fault_seed=1, shards=1)


def test_archive_holds_every_event_once_in_its_shard(tmp_path: Path) -> None:
    report = build_archive(RUN, tmp_path)
    events = _events()
    assert report["events"] == len(events) == sum(report["events_per_shard"])
    replayed = [e for i in range(RUN.shards) for e in read_shard(shard_file(tmp_path, i))]
    assert sorted(e["event_id"] for e in replayed) == sorted(e["event_id"] for e in events)
    for i in range(RUN.shards):
        assert all(shard_of(e["event_id"], 2) == i for e in read_shard(shard_file(tmp_path, i)))
    with gzip.open(tmp_path / ORACLE, "rt") as fh:
        assert json.load(fh) == expected_state(events, load_config().billing.max_attempts)
    assert report["is_synthetic"] is True and report["archive_bytes"] > 0


def test_manifest_must_match_the_run(tmp_path: Path) -> None:
    build_archive(RUN, tmp_path)
    load_manifest(tmp_path, RUN)
    other = ScaleRun(customers=41, days=7, sim_seed=1, fault_seed=701, shards=2)
    with pytest.raises(ValueError, match="customers"):
        load_manifest(tmp_path, other)


def test_one_shard_in_process_applies_its_events(tmp_path: Path, pg_url: str) -> None:
    build_archive(RUN, tmp_path / "a")
    capped = ScaleRun(**{**RUN.__dict__, "duckdb_memory_limit_mb": 256, "duckdb_threads": 1})
    shard = run_shard(capped, tmp_path / "a", 0, pg_url, tmp_path / "wh")
    n = sum(1 for _ in read_shard(shard_file(tmp_path / "a", 0)))
    assert shard["events_published"] == shard["warehouse_rows"] == n
    assert shard["latency"]["operational"]["processing"]["count"] > 0


def test_sharded_replay_matches_the_oracle_and_reports_resources(
    tmp_path: Path, pg_url: str
) -> None:
    run = ScaleRun(customers=40, days=7, sim_seed=1, fault_seed=701, shards=2, crash_rate=0.05)
    build_archive(run, tmp_path / "a")
    report = run_scale(run, tmp_path / "a", pg_url, tmp_path / "wh")
    assert report["matches_oracle"] and report["ledger_matches_oracle"]
    assert report["subscription_inconsistencies"] == 0
    assert report["mismatched_entities"] == {"customers": 0, "invoices": 0}
    assert report["warehouse_rows"] == report["events_in_archive"] == report["events_published"]
    assert report["dead_letters_open"] == {} and error_rate(report) == 0.0
    assert report["drain"]["warehouse"]["crashes"] > 0  # chaos actually happened
    assert report["resources"]["samples"] >= 2
    assert report["postgres"]["xact_commit"] > 0
    assert report["latency"]["operational"]["processing"]["p99_ms"] is not None
    assert evaluate_slo(report, {"min_events_per_s": 1.0, "max_wall_s": 3600})["passed"]
    failed = evaluate_slo(report, {"min_events_per_s": 1e12})
    assert not failed["passed"] and not failed["checks"]["events_per_s"]["pass"]


def test_replay_detects_a_state_that_differs_from_the_oracle(tmp_path: Path, pg_url: str) -> None:
    build_archive(RUN, tmp_path / "a")
    oracle_path = tmp_path / "a" / ORACLE
    with gzip.open(oracle_path, "rt") as fh:
        expected = json.load(fh)
    expected["ledger_total_minor"] += 1
    victim = sorted(expected["customers"])[0]
    expected["customers"][victim][0] = "not-a-state"
    with gzip.open(oracle_path, "wt") as fh:
        json.dump(expected, fh)
    report = run_scale(RUN, tmp_path / "a", pg_url, tmp_path / "wh")
    assert not report["matches_oracle"] and not report["ledger_matches_oracle"]
    assert report["mismatched_entities"]["customers"] == 1
    assert not evaluate_slo(report, {})["passed"]


def test_missing_latency_fails_its_slo() -> None:
    report: dict[str, Any] = {
        "matches_oracle": True, "ledger_matches_oracle": True, "subscription_inconsistencies": 0,
        "dead_letters_open": {}, "events_published": 10, "postgres": {"deadlocks": 0},
        "latency": {}, "resources": {"host_mem_used_peak_mb": 100.0}, "events_per_s": 5.0,
        "wall_s": 2.0, "shard_peak_rss_mb_max": 50.0,
    }  # fmt: skip
    slo = {
        "max_operational_processing_p99_ms": 10.0, "max_host_mem_used_mb": 200.0,
        "max_shard_rss_mb": 100.0, "max_operational_end_to_end_p95_ms": 5.0,
    }  # fmt: skip
    result = evaluate_slo(report, slo)
    assert not result["passed"]
    assert result["checks"]["operational_processing_p99_ms"] == {
        "value": None,
        "op": "<=",
        "limit": 10.0,
        "pass": False,
    }
    assert result["checks"]["host_mem_used_peak_mb"]["pass"]
    report["dead_letters_open"] = {"x": 1}
    assert error_rate(report) == 0.1
    assert error_rate({**report, "events_published": 0}) == 0.0


def test_weighted_quantiles_weight_each_shard_by_its_count() -> None:
    small = {"count": 2, "values": [1.0, 1.0]}
    big = {"count": 98, "values": [10.0, 10.0]}  # 49 events per kept value
    q = weighted_quantiles([small, big])
    assert q["count"] == 100 and q["p50_ms"] == 10.0 and q["max_ms"] == 10.0
    assert weighted_quantiles([{"count": 0, "values": []}])["p99_ms"] is None
    merged = merge_latency([{"latency": {"r": {"k": small}}}, {"latency": {"r": {"k": big}}}])
    assert merged["r"]["k"]["count"] == 100


def test_config_steps_load_and_cli_runs_end_to_end(tmp_path: Path, pg_url: str) -> None:
    run, slo = load_scale_config(CONFIG, "smoke")
    assert slo == {} and run.sim_seed == 1 and run.fault_seed == 701
    cfg = tmp_path / "slo.toml"
    cfg.write_text(
        CONFIG.read_text()
        + "\n[scales.t]\ncustomers = 30\ndays = 5\nshards = 2\n"
        + "[scales.t.slo]\nmin_events_per_s = 1.0\nmax_error_rate = 0.0\n"
    )
    archive, out = tmp_path / "a", tmp_path / "r.json"
    assert main(["scale-archive", "--config", str(cfg), "--scale", "t", "--out", str(archive)]) == 0
    args = ["scale-run", "--config", str(cfg), "--scale", "t", "--archive", str(archive),
            "--warehouse-dir", str(tmp_path / "wh"), "--report", str(out)]  # fmt: skip
    assert main(["--database-url", pg_url, *args]) == 0
    report = json.loads(out.read_text())
    assert report["scale"] == "t" and report["slo"]["passed"]
    with pytest.raises(SystemExit, match="database-url"):
        main(args)


def test_redrive_can_be_limited_to_one_environment(store: ControlPlaneStore) -> None:
    assert store.dead_letters_for_redrive(source_prefix="praxis-scale-s00-") == []


def test_http_slo_reads_per_surface_k6_metrics() -> None:
    slo = {"max_p95_ms": 250, "max_p99_ms": 1000, "max_failed_rate": 0.001}
    ok = {
        "metrics": {
            f"http_req_duration{{surface:{s}}}": {"p(95)": 50.0, "p(99)": 100.0}
            for s in ("api", "console")
        }
        | {f"http_req_failed{{surface:{s}}}": {"value": 0} for s in ("api", "console")}
    }
    assert evaluate_http(ok, slo)["passed"]
    dropped = {"metrics": {**ok["metrics"], "dropped_iterations": {"count": 3}}}
    assert not evaluate_http(dropped, slo)["passed"]
    missing: dict[str, Any] = {"metrics": {}}
    result = evaluate_http(missing, slo)
    assert not result["passed"] and result["checks"]["api_p95_ms"]["value"] is None


def test_http_check_cli_writes_a_verdict(tmp_path: Path) -> None:
    summary = tmp_path / "k6.json"
    metrics: dict[str, Any] = {
        f"http_req_duration{{surface:{s}}}": {"p(95)": 10.0, "p(99)": 20.0}
        for s in ("api", "console")
    }
    metrics |= {f"http_req_failed{{surface:{s}}}": {"value": 0} for s in ("api", "console")}
    summary.write_text(json.dumps({"metrics": metrics}))
    args = [
        "scale-http-check",
        "--config",
        str(CONFIG),
        "--scale",
        "100k",
        "--summary",
        str(summary),
    ]
    assert main(args) == 0
    assert json.loads((tmp_path / "http-slo.json").read_text())["passed"]
    metrics["http_req_failed{surface:api}"] = {"value": 0.5}
    summary.write_text(json.dumps({"metrics": metrics}))
    assert main(args) == 1


def test_lean_oracle_gives_the_same_state() -> None:
    events = _events()
    full, lean = _DayOracle(4), _DayOracle(4, lean=True)
    for e in events:
        full.add(e)
        lean.add(e)
    full.flush()
    lean.flush()
    assert oracle_state(full.validator) == oracle_state(lean.validator)
    assert lean.validator._seen_ids == set() and len(full.validator._seen_ids) == len(events)
