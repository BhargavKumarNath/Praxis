from __future__ import annotations

from typing import Any

from scripts.coverage_gate import evaluate, load_floors


def report(**files: float) -> dict[str, Any]:
    return {"files": {p: {"summary": {"percent_covered": v}} for p, v in files.items()}}


FLOORS = {"src/praxis/domain/*": 95.0, "src/praxis/*": 85.0}


def test_first_matching_rule_wins_and_passes() -> None:
    failures, stale = evaluate(
        report(**{"src/praxis/domain/states.py": 96.0, "src/praxis/data/x.py": 86.0}), FLOORS
    )
    assert failures == [] and stale == []


def test_critical_module_below_its_floor_fails_even_above_the_default() -> None:
    failures, _ = evaluate(report(**{"src/praxis/domain/states.py": 90.0}), FLOORS)
    assert len(failures) == 1 and "95%" in failures[0]


def test_unmatched_file_and_stale_rule_are_errors() -> None:
    failures, stale = evaluate(report(**{"scripts/x.py": 100.0}), FLOORS)
    assert "no coverage floor rule" in failures[0]
    assert stale == list(FLOORS)


def test_repository_floors_are_ordered_specific_first() -> None:
    floors = list(load_floors())
    assert floors[-1] == "src/praxis/*", "the catch-all must be last"
    assert load_floors()["src/praxis/control/*"] >= 95
