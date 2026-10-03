from __future__ import annotations

from pathlib import Path

import pytest

from praxis.data.errors import RawIntegrityError
from praxis.data.models import RequestSpec, SourceId, make_batch_id, sha256_hex
from praxis.data.raw_store import LocalRawStore, RawMeta
from tests.data.helpers import NOW


def _meta(body: bytes, series: str = "s", **kw: object) -> RawMeta:
    request = RequestSpec(source=SourceId.FRED, url="u", params={"a": "1"}, series_id=series)
    return RawMeta(
        batch_id=make_batch_id(request, body),
        request=request,
        endpoint="u?a=1",
        retrieved_at=NOW,
        checksum_sha256=sha256_hex(body),
        http_status=200,
        **kw,
    )


def test_put_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    meta = _meta(b"payload")
    assert store.put(meta, b"payload") is True
    body_file = tmp_path / "fred" / f"{meta.batch_id}.body"
    mtime = body_file.stat().st_mtime_ns
    assert store.put(meta, b"payload") is False
    assert body_file.stat().st_mtime_ns == mtime
    assert store.read_body(meta) == b"payload"


def test_tampered_body_is_detected_on_read(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    meta = _meta(b"payload")
    store.put(meta, b"payload")
    (tmp_path / "fred" / f"{meta.batch_id}.body").write_bytes(b"tampered")
    with pytest.raises(RawIntegrityError, match="checksum"):
        store.read_body(meta)


def test_put_rejects_body_that_does_not_match_meta(tmp_path: Path) -> None:
    with pytest.raises(RawIntegrityError):
        LocalRawStore(tmp_path).put(_meta(b"a"), b"b")


def test_batch_id_depends_on_request_and_content() -> None:
    r1 = RequestSpec(source=SourceId.FRED, url="u", params={}, series_id="a")
    r2 = RequestSpec(source=SourceId.FRED, url="u", params={}, series_id="b")
    assert make_batch_id(r1, b"x") == make_batch_id(r1, b"x")
    assert make_batch_id(r1, b"x") != make_batch_id(r1, b"y")
    assert make_batch_id(r1, b"x") != make_batch_id(r2, b"x")


def test_iteration_is_deterministic_and_filterable(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    metas = [_meta(f"p{i}".encode(), series=f"s{i}") for i in range(4)]
    for m, i in zip(metas, range(4), strict=True):
        store.put(m, f"p{i}".encode())
    first = [m.batch_id for m in store.iter_meta(SourceId.FRED)]
    assert first == [m.batch_id for m in store.iter_meta()]
    assert first == sorted(first)  # equal retrieved_at -> ordered by batch_id
    assert list(store.iter_meta(SourceId.EIA)) == []
    assert store.get_meta(SourceId.FRED, "missing") is None


def test_quarantine_reason_round_trips(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    meta = _meta(b"x", quarantine_reason="contract violation")
    store.put(meta, b"x")
    assert store.get_meta(SourceId.FRED, meta.batch_id) == meta
