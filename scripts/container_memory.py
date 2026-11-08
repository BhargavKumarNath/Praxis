"""Print a Docker container's cgroup v2 memory accounting as JSON (Linux hosts only).

Used by `make scale-m1` to record the Postgres container's peak memory next to the harness's
own cgroup figures (ADR 0028). Read-only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main(container: str) -> int:
    cid = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "inspect", "-f", "{{.Id}}", container],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope")
    out: dict[str, object] = {"container": container}
    for name in ("memory.peak", "memory.max", "memory.swap.peak", "memory.swap.max"):
        f = base / name
        raw = f.read_text().strip() if f.exists() else None
        out[name] = None if raw in (None, "max") else round(int(raw) / 2**20, 1)
    events = base / "memory.events"
    if events.exists():
        out["oom_kill"] = int(dict(x.split() for x in events.read_text().splitlines())["oom_kill"])
    print(json.dumps(out))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
