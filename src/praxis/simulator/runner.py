"""Run a simulation: stream events, checksum, optionally persist, record provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from praxis import __version__
from praxis.events.envelope import ENVELOPE_SCHEMA_VERSION
from praxis.events.payloads import PAYLOAD_SCHEMA_VERSION
from praxis.simulator.config import SimulationConfig, load_config
from praxis.simulator.engine import Engine
from praxis.simulator.events import canonical_line
from praxis.simulator.population import Population, generate_population
from praxis.simulator.validation import StreamValidator


@dataclass(frozen=True)
class RunResult:
    seed: int
    config_hash: str
    event_count: int
    checksum: str
    ground_truth_checksum: str
    counts: dict[str, int]
    elapsed_s: float
    peak_rss_mb: float
    population: Population = field(repr=False)
    quality_status: str = "unvalidated"

    def manifest(self, config: SimulationConfig) -> dict[str, Any]:
        return {
            "dataset": "praxis-simulator-events",
            "is_synthetic": True,
            "source": "praxis.simulator",
            "retrieved_at": datetime.now(UTC).isoformat(),  # not part of the checksum
            "source_period": {
                "start_date": config.run.start_date.isoformat(),
                "days": config.run.days,
            },
            "event_schema_version": ENVELOPE_SCHEMA_VERSION,
            "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
            "batch_id": f"{self.seed}-{self.config_hash[:12]}",
            "quality_status": self.quality_status,
            "seed": self.seed,
            "n_customers": config.population.n_customers,
            "config_hash": self.config_hash,
            "event_count": self.event_count,
            "event_counts": self.counts,
            "stream_checksum_sha256": self.checksum,
            "ground_truth_checksum_sha256": self.ground_truth_checksum,
            "elapsed_s": round(self.elapsed_s, 3),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "numpy_version": np.__version__,
            "python_version": platform.python_version(),
            "praxis_version": __version__,
            "code_revision": _git_revision(),
        }


def _git_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # KB on Linux


def run_simulation(
    config: SimulationConfig,
    seed: int,
    *,
    out_dir: Path | None = None,
    validate: bool = False,
    schema_every: int = 1,
) -> RunResult:
    start = time.perf_counter()
    population = generate_population(config, seed)
    engine = Engine(config, seed, population)
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    validator = StreamValidator(config.billing.max_attempts, schema_every) if validate else None
    handle = None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        handle = (out_dir / "events.ndjson").open("wb")
    total = 0
    try:
        for event in engine.run():
            line = canonical_line(event)
            digest.update(line)
            counts[event["event_type"]] += 1
            total += 1
            if validator is not None:
                validator.feed(event)
            if handle is not None:
                handle.write(line)
    finally:
        if handle is not None:
            handle.close()
    result = RunResult(
        seed=seed,
        config_hash=config.config_hash,
        event_count=total,
        checksum=digest.hexdigest(),
        ground_truth_checksum=population.checksum(),
        counts=dict(sorted(counts.items())),
        elapsed_s=time.perf_counter() - start,
        peak_rss_mb=peak_rss_mb(),
        population=population,
        quality_status="validated" if validate else "unvalidated",
    )
    if out_dir is not None:
        population.write_npz(out_dir / "ground_truth.npz", config, seed)
        (out_dir / "manifest.json").write_text(
            json.dumps(result.manifest(config), indent=2, sort_keys=True) + "\n"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="praxis.simulator", description="Run the SYNTHETIC simulator."
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--customers", type=int, default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help="override run.start_date (changes config_hash)",
    )
    ap.add_argument("--out", type=Path, default=None, help="directory for events / manifest")
    ap.add_argument("--validate", action="store_true", help="run the stream validator")
    ap.add_argument("--schema-every", type=int, default=1, help="Pydantic-validate every Nth event")
    args = ap.parse_args(argv)
    config = load_config(args.config).with_overrides(
        n_customers=args.customers, days=args.days, start_date=args.start_date
    )
    result = run_simulation(
        config, args.seed, out_dir=args.out, validate=args.validate, schema_every=args.schema_every
    )
    summary = {k: v for k, v in result.manifest(config).items() if k != "retrieved_at"}
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
