"""Structured JSON logging conventions.

One JSON object per line on stdout. Required keys: ``timestamp`` (UTC ISO-8601),
``level``, ``logger``, ``message``, ``service``. ``trace_id`` / ``correlation_id`` are
injected from context. Extra fields passed via ``extra={...}`` are emitted under their
own keys; keys that look secret are redacted as a last line of defence.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from praxis.tracing import current_correlation_id, current_trace_id

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SECRET_MARKERS = ("secret", "password", "token", "api_key", "apikey", "authorization")
REDACTED = "[REDACTED]"


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    return REDACTED if any(marker in lowered for marker in _SECRET_MARKERS) else value


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "trace_id": current_trace_id(),
            "correlation_id": current_correlation_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in entry:
                entry[key] = _redact(key, value)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, separators=(",", ":"))


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
