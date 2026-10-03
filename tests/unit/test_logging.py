from __future__ import annotations

import json
import logging

import pytest

from praxis.logging import REDACTED, JsonFormatter, configure_logging
from praxis.tracing import trace_context


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("praxis.test", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_required_keys_and_context() -> None:
    fmt = JsonFormatter("svc")
    with trace_context() as (tid, cid):
        out = json.loads(fmt.format(_record(decision_id="d1")))
    assert out["service"] == "svc"
    assert out["level"] == "INFO"
    assert out["message"] == "hello"
    assert out["trace_id"] == tid
    assert out["correlation_id"] == cid
    assert out["decision_id"] == "d1"
    assert out["timestamp"].endswith("+00:00")


def test_no_context_yields_nulls() -> None:
    out = json.loads(JsonFormatter("svc").format(_record()))
    assert out["trace_id"] is None
    assert out["correlation_id"] is None


def test_secret_like_keys_redacted() -> None:
    out = json.loads(
        JsonFormatter("svc").format(
            _record(stripe_api_key="sk_live_x", webhook_secret="whsec_x", Authorization="Bearer x")
        )
    )
    assert out["stripe_api_key"] == REDACTED
    assert out["webhook_secret"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert "sk_live_x" not in json.dumps(out)


def test_exception_is_captured() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        rec = _record()
        rec.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter("svc").format(rec))
    assert "RuntimeError: boom" in out["exception"]


def test_configure_logging_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("svc", "INFO")
    logging.getLogger("praxis.x").info("structured", extra={"k": 1})
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["k"] == 1
