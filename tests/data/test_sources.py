"""Source contract tests (required_test.md section 8): valid parse, required/optional fields,
schema drift, chunking, credentials."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from itertools import pairwise

import pytest

from praxis.data.errors import MissingCredentialError, SchemaDriftError
from praxis.data.models import RequestSpec, SourceId, TimeWindow
from praxis.data.sources import build_registry
from tests.data.helpers import FIXTURE_FOR_SOURCE, NOW, WINDOW, cfg, fixture_bytes, registry

BATCH = "b" * 64


def _parse(source_id: SourceId, body: bytes, request_index: int = 0):  # type: ignore[no-untyped-def]
    src = registry()[source_id]
    request = src.build_requests(WINDOW)[request_index]
    return src.parse(body, request, batch_id=BATCH, retrieved_at=NOW), request


def _mutate(source_id: SourceId, fn) -> bytes:  # type: ignore[no-untyped-def]
    data = json.loads(fixture_bytes(FIXTURE_FOR_SOURCE[source_id]))
    fn(data)
    return json.dumps(data).encode()


# --- valid responses ------------------------------------------------------------------
def test_open_meteo_valid_response_parses_to_utc_records() -> None:
    parsed, request = _parse(
        SourceId.OPEN_METEO, fixture_bytes("open_meteo_london_2026-01-05.json")
    )
    assert request.entity_id == "eu_west"
    assert len(parsed.records) == 24 * 5 and parsed.skipped == 0
    first = next(r for r in parsed.records if r.metric == "temperature_2m")
    assert first.observed_at == datetime(2026, 1, 5, 0, tzinfo=UTC)
    assert first.value == -1.7 and first.unit == "°C"
    assert {r.metric for r in parsed.records} == set(cfg().open_meteo.hourly)
    assert parsed.quality.value == "ok"


def test_carbon_valid_response_yields_actual_and_forecast() -> None:
    parsed, _ = _parse(SourceId.CARBON_INTENSITY, fixture_bytes("carbon_gb_2026-01-05.json"))
    metrics = {r.metric for r in parsed.records}
    assert metrics == {"carbon_intensity_actual", "carbon_intensity_forecast"}
    first = min(parsed.records, key=lambda r: (r.observed_at, r.metric))
    assert first.observed_at == datetime(2026, 1, 4, 23, 30, tzinfo=UTC)
    assert first.unit == "gCO2/kWh"


def test_fred_missing_marker_is_skipped_and_marks_partial() -> None:
    parsed, _ = _parse(SourceId.FRED, fixture_bytes("fred_cpiaucsl.json"))
    assert [r.value for r in parsed.records] == [325.1, 325.9, 327.2]
    assert parsed.skipped == 1 and parsed.quality.value == "partial"
    assert parsed.records[0].observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert parsed.records[0].metric == "cpiaucsl"


def test_eia_null_value_is_skipped_and_marks_partial() -> None:
    parsed, request = _parse(SourceId.EIA, fixture_bytes("eia_ny_demand.json"))
    assert request.entity_id == "NY"
    assert [r.value for r in parsed.records] == [15210.0, 14875.0]
    assert parsed.skipped == 1
    assert parsed.records[1].observed_at == datetime(2026, 1, 5, 1, tzinfo=UTC)


def test_carbon_null_actual_for_unmeasured_slot_is_skipped_not_zeroed() -> None:
    def blank_actual(d: dict) -> None:  # type: ignore[type-arg]
        d["data"][0]["intensity"]["actual"] = None

    parsed, _ = _parse(SourceId.CARBON_INTENSITY, _mutate(SourceId.CARBON_INTENSITY, blank_actual))
    assert parsed.skipped == 1
    assert all(r.value > 0 for r in parsed.records)


def test_open_meteo_null_hour_is_skipped() -> None:
    def blank(d: dict) -> None:  # type: ignore[type-arg]
        d["hourly"]["temperature_2m"][3] = None

    parsed, _ = _parse(SourceId.OPEN_METEO, _mutate(SourceId.OPEN_METEO, blank))
    assert parsed.skipped == 1 and len(parsed.records) == 24 * 5 - 1


def test_optional_fields_absent_are_tolerated() -> None:
    def strip(d: dict) -> None:  # type: ignore[type-arg]
        for key in ("elevation", "generationtime_ms", "latitude", "longitude"):
            d.pop(key, None)

    parsed, _ = _parse(SourceId.OPEN_METEO, _mutate(SourceId.OPEN_METEO, strip))
    assert len(parsed.records) == 120

    def strip_index(d: dict) -> None:  # type: ignore[type-arg]
        for slot in d["data"]:
            del slot["intensity"]["index"]

    parsed, _ = _parse(SourceId.CARBON_INTENSITY, _mutate(SourceId.CARBON_INTENSITY, strip_index))
    assert parsed.records


# --- schema drift is visible, never silent --------------------------------------------
DRIFT_CASES = [
    (SourceId.OPEN_METEO, lambda d: d.pop("hourly_units"), "hourly_units"),
    (SourceId.OPEN_METEO, lambda d: d["hourly"].pop("cloud_cover"), "cloud_cover"),
    (SourceId.OPEN_METEO, lambda d: d["hourly"]["precipitation"].pop(), "length"),
    (SourceId.OPEN_METEO, lambda d: d.update(timezone="Europe/London"), "GMT"),
    (SourceId.OPEN_METEO, lambda d: d["hourly"].update(temperature_2m=["x"] * 24), "string"),
    (SourceId.OPEN_METEO, lambda d: d["hourly"].pop("time"), "time"),
    (SourceId.CARBON_INTENSITY, lambda d: d.pop("data"), "data"),
    (SourceId.CARBON_INTENSITY, lambda d: d["data"][0].pop("intensity"), "intensity"),
    (SourceId.CARBON_INTENSITY, lambda d: d["data"][0].update({"from": "20260105"}), "timestamp"),
    (
        SourceId.CARBON_INTENSITY,
        lambda d: d["data"][0]["intensity"].update(actual="high"),
        "actual",
    ),
    (SourceId.FRED, lambda d: d.pop("observations"), "observations"),
    (SourceId.FRED, lambda d: d["observations"][0].update(value="abc"), "unparseable"),
    (SourceId.FRED, lambda d: d["observations"][0].update(date="Jan"), "unparseable"),
    (SourceId.FRED, lambda d: d["observations"][0].pop("value"), "value"),
    (SourceId.EIA, lambda d: d.pop("response"), "response"),
    (SourceId.EIA, lambda d: d["response"].update(total="9"), "truncated"),
    (SourceId.EIA, lambda d: d["response"]["data"][0].update(respondent="CAL"), "facet"),
    (SourceId.EIA, lambda d: d["response"]["data"][0].update(period="Jan"), "unparseable"),
    (SourceId.EIA, lambda d: d["response"]["data"][0].pop("value-units"), "value-units"),
]


@pytest.mark.parametrize(("source_id", "mutation", "needle"), DRIFT_CASES)
def test_schema_change_raises_drift(source_id: SourceId, mutation, needle: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SchemaDriftError) as err:
        _parse(source_id, _mutate(source_id, mutation))
    assert needle in str(err.value)


@pytest.mark.parametrize("source_id", list(SourceId))
@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[]", b"null"])
def test_garbage_bodies_raise_drift(source_id: SourceId, body: bytes) -> None:
    with pytest.raises(SchemaDriftError):
        _parse(source_id, body)


def test_fred_unconfigured_series_is_drift() -> None:
    src = registry()[SourceId.FRED]
    request = RequestSpec(
        source=SourceId.FRED, url="u", params={"series_id": "NOPE"}, series_id="fred:NOPE"
    )
    with pytest.raises(SchemaDriftError, match="not configured"):
        src.parse(fixture_bytes("fred_cpiaucsl.json"), request, batch_id=BATCH, retrieved_at=NOW)


# --- request construction -------------------------------------------------------------
def test_open_meteo_requests_one_per_location_with_gmt_and_no_credentials() -> None:
    requests = registry()[SourceId.OPEN_METEO].build_requests(WINDOW)
    assert len(requests) == len(cfg().locations)
    for r in requests:
        assert r.params["timezone"] == "GMT"
        assert not any("key" in k for k in r.params)


def test_windows_are_chunked_within_api_limits() -> None:
    long_window = TimeWindow(start=date(2026, 1, 1), end=date(2026, 3, 31))  # 90 days
    carbon = registry()[SourceId.CARBON_INTENSITY].build_requests(long_window)
    assert len(carbon) == 7  # ceil(90 / 13)
    eia = registry()[SourceId.EIA].build_requests(long_window)
    assert len(eia) == 3 * 2  # 3 x 30-day chunks per respondent, NY and CAL
    assert {r.entity_id for r in eia} == {"NY", "CAL"}
    om = registry()[SourceId.OPEN_METEO].build_requests(long_window)
    assert len(om) == 3 * len(cfg().locations)  # 31-day cap -> 3 chunks


def test_chunks_cover_the_window_exactly_without_overlap() -> None:
    from praxis.data.sources.base import chunk_window

    window = TimeWindow(start=date(2026, 1, 1), end=date(2026, 3, 31))
    chunks = list(chunk_window(window, 13))
    assert chunks[0].start == window.start and chunks[-1].end == window.end
    for a, b in pairwise(chunks):
        assert (b.start - a.end).days == 1
    assert all((c.end - c.start).days + 1 <= 13 for c in chunks)


def test_window_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="precedes"):
        TimeWindow(start=date(2026, 1, 2), end=date(2026, 1, 1))


def test_keyed_sources_refuse_to_run_without_credentials() -> None:
    reg = build_registry(cfg())
    for sid in (SourceId.FRED, SourceId.EIA):
        with pytest.raises(MissingCredentialError):
            reg[sid].credential_params()
    assert reg[SourceId.OPEN_METEO].credential_params() == {}


def test_requests_never_embed_credentials() -> None:
    for src in registry().values():
        for request in src.build_requests(WINDOW):
            blob = request.model_dump_json()
            assert "ffffffff" not in blob and "eeeeeeee" not in blob


# --- batch identity ignores volatile server fields only --------------------------------
def test_fingerprint_ignores_open_meteo_generation_time_but_not_data() -> None:
    src = registry()[SourceId.OPEN_METEO]
    base = json.loads(fixture_bytes("open_meteo_london_2026-01-05.json"))
    timing = {**base, "generationtime_ms": 99.9}
    other = json.loads(json.dumps(base))
    other["hourly"]["temperature_2m"][0] = 42.0
    fp = src.fingerprint
    assert fp(json.dumps(base).encode()) == fp(json.dumps(timing).encode())
    assert fp(json.dumps(base).encode()) != fp(json.dumps(other).encode())


@pytest.mark.parametrize("source_id", list(SourceId))
def test_fingerprint_is_stable_and_falls_back_on_non_json(source_id: SourceId) -> None:
    src = registry()[source_id]
    body = fixture_bytes(FIXTURE_FOR_SOURCE[source_id])
    assert src.fingerprint(body) == src.fingerprint(body)
    assert src.fingerprint(b"\xffnot json") == b"\xffnot json"
