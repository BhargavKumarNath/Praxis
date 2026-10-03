"""Export payload JSON Schemas to ``schemas/events/payloads/`` (or check they are current)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from praxis.events.payloads import PAYLOAD_MODELS, PAYLOAD_SCHEMA_VERSION

OUT = Path(__file__).resolve().parents[1] / "schemas" / "events" / "payloads"


def render() -> dict[str, str]:
    out: dict[str, str] = {}
    for event_type, model in PAYLOAD_MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = (
            f"https://praxis.local/schemas/events/payloads/{event_type}.v{PAYLOAD_SCHEMA_VERSION}.schema.json"
        )
        out[f"{event_type}.v{PAYLOAD_SCHEMA_VERSION}.schema.json"] = (
            json.dumps(schema, indent=2, sort_keys=True) + "\n"
        )
    return out


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        path = OUT / name
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
    for name in stale:
        print(f"STALE: {name}")  # noqa: T201
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
