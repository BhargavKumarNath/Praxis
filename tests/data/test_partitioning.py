"""Partition-filter discipline for BigQuery facts (required_test.md section 8).

BigQuery is not available (no cloud resources exist, CLAUDE.md section 14), so no dry run is
possible. These static checks guard the contract instead: every fact is partitioned with a
required filter, and every model reading a fact bounds the partition column.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DBT = Path(__file__).resolve().parents[2] / "dbt" / "models"
FACTS = sorted((DBT / "marts").glob("fct_*.sql"))
REF = re.compile(r"ref\('(fct_\w+)'\)")
PARTITION = re.compile(
    r"partition_by=\{'field': '(?P<field>\w+)', 'data_type': 'date'\},\s*"
    r"require_partition_filter=true"
)


def _partition_field(fact: str) -> str:
    match = PARTITION.search((DBT / "marts" / f"{fact}.sql").read_text())
    assert match, f"{fact} lacks a BigQuery partition_by with require_partition_filter=true"
    return match.group("field")


def test_there_are_facts_to_check() -> None:
    assert len(FACTS) >= 5


@pytest.mark.parametrize("fact", [f.stem for f in FACTS])
def test_every_fact_is_date_partitioned_with_required_filter(fact: str) -> None:
    field = _partition_field(fact)
    assert field in {"event_date", "observed_date"}
    assert re.search(rf"\b{field}\b", (DBT / "marts" / f"{fact}.sql").read_text())


def test_partition_config_is_guarded_for_bigquery_only() -> None:
    for fact in FACTS:
        text = fact.read_text()
        match = PARTITION.search(text)
        assert match, fact.name
        assert "{% if target.type == 'bigquery' %}" in text[: match.start()], fact.name
        assert "{% endif %}" in text[match.end() :], fact.name


def _reader_models() -> list[Path]:
    return [
        p
        for p in DBT.rglob("*.sql")
        if p.parent.name != "marts" or not p.stem.startswith("fct_")
        if REF.search(p.read_text())
    ]


def test_models_that_read_facts_exist() -> None:
    assert any(p.name == "feat_region_daily.sql" for p in _reader_models())


@pytest.mark.parametrize("model", _reader_models(), ids=lambda p: p.name)
def test_every_fact_read_bounds_its_partition_column(model: Path) -> None:
    text = model.read_text()
    for match in REF.finditer(text):
        field = _partition_field(match.group(1))
        # The predicate must appear in the same CTE / statement as the read.
        tail = text[match.end() :]
        end = re.search(r"\n\)", tail)
        scope = tail[: end.start()] if end else tail
        assert re.search(rf"\b{field}\s+between\b", scope), (
            f"{model.name}: read of {match.group(1)} lacks a '{field} between' predicate"
        )


def test_filter_checker_rejects_an_unfiltered_read() -> None:
    """Negative control for the checker above."""
    sql = "select * from {{ ref('fct_payments') }} as p\ngroup by 1\n)"
    match = REF.search(sql)
    assert match
    scope = sql[match.end() :]
    assert not re.search(r"\bevent_date\s+between\b", scope)
