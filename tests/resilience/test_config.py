"""Pinned, pre-registered resilience criteria (Phase 15, ADR 0023 and amendments 1-3).

Every hash was pinned BEFORE the runs it governs: v1 (``failures.toml``, fault seed 801),
remediation 1 (``remediation.toml``, 802), remediation 2 (``remediation-803.toml``, 803) and
remediation 3 (``remediation-804.toml``, 804, the active suite). The first three are history
and never change. Editing any of them needs an
ADR 0023 amendment and the owner's approval; never edit after a result.
"""

from __future__ import annotations

import hashlib

from praxis.ops.config import load_ops_config
from praxis.ops.runbooks import validate_runbooks
from tests.resilience.harness import (
    CONFIG_PATH,
    CRITERIA,
    ORIGINAL_PATH,
    REMEDIATION_802_PATH,
    REMEDIATION_803_PATH,
    load_criteria,
)

PINNED_SHA256 = "607dec4a252d70d91b458fb478038619190c4a2f2e36849552a016d708f85c77"
REMEDIATION_803_SHA256 = "9fd2f85aad15485b69ca212aeb7ac3f94a1f9f0703a94a4951f85c34c4c5d2f0"
REMEDIATION_802_SHA256 = "9328255d1f5ffb8d23709bf3c422d42a8d0818585ac8bd83b2517b2bd255a811"
ORIGINAL_SHA256 = "4810c7dbbc980b61cc8a0eab6ca18ac51789477ef2182fbe631fcf138aef3b14"
AMENDED = {"consumer_crash"}

# required_test.md s21 failure list and the project_plan.md Phase 15 extras.
REQUIRED_S21 = {
    "source timeout", "source malformed", "DB timeout", "Pub/Sub duplicate", "consumer crash",
    "model missing", "feature stale", "agent unavailable", "event burst",
}  # fmt: skip
PLAN_ITEMS = {
    "external API timeout", "malformed source payload", "DB timeout", "Pub/Sub redelivery",
    "service restart", "model artifact missing", "stale features", "failed candidate",
    "Stripe duplicate event", "agent API failure", "sudden event burst",
}  # fmt: skip
HELD_OUT_SEEDS = {42, 43, 44, 45, 201, 202, 203, 301, 302, 303, 601, 621, 701, 801, 802, 803}


def test_criteria_files_are_pinned() -> None:
    assert hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest() == PINNED_SHA256
    assert hashlib.sha256(ORIGINAL_PATH.read_bytes()).hexdigest() == ORIGINAL_SHA256
    assert hashlib.sha256(REMEDIATION_802_PATH.read_bytes()).hexdigest() == REMEDIATION_802_SHA256
    assert hashlib.sha256(REMEDIATION_803_PATH.read_bytes()).hexdigest() == REMEDIATION_803_SHA256


def test_remediation_changes_only_the_amended_row_and_the_seed() -> None:
    v1, r1 = load_criteria(ORIGINAL_PATH), load_criteria(REMEDIATION_802_PATH)
    assert r1.supersedes == v1.version and v1.run.fault_seed == 801
    assert r1.run.model_dump() == v1.run.model_dump() | {"fault_seed": 802}
    assert [f.id for f in r1.failure] == [f.id for f in v1.failure]
    changed = {f.id for f, o in zip(r1.failure, v1.failure, strict=True) if f != o}
    assert changed == AMENDED
    assert all(r1.get(i).amendment for i in AMENDED)
    assert not any(f.amendment for f in r1.failure if f.id not in AMENDED)
    # Registrations 2 and 3 re-run registration 1 unchanged on fresh seeds (ADR 0023
    # amendments 2 and 3).
    r2 = load_criteria(REMEDIATION_803_PATH)
    assert r2.supersedes == r1.version and CRITERIA.supersedes == r2.version
    assert CRITERIA.failure == r2.failure == r1.failure
    assert r2.run.model_dump() == r1.run.model_dump() | {"fault_seed": 803}
    assert CRITERIA.run.model_dump() == r1.run.model_dump() | {"fault_seed": 804}


def test_every_required_failure_and_plan_item_has_a_row() -> None:
    assert {f.required_test for f in CRITERIA.failure if f.required_test} == REQUIRED_S21
    covered = {f.plan_item for f in CRITERIA.failure}
    # "service restart" is covered by two rows: the consumer and the API / ops monitor.
    assert covered | {"service restart"} >= PLAN_ITEMS
    assert "service restart" in covered
    assert len({f.id for f in CRITERIA.failure}) == len(CRITERIA.failure) == 12


def test_bounds_are_the_strictest() -> None:
    run = CRITERIA.run
    assert (run.tick_interval_s, run.clear_after_ticks) == (60, 2)
    assert all(f.max_ticks_to_detect == 1 for f in CRITERIA.failure)
    assert run.sim_seed == 1 and run.fault_seed == 804
    assert {run.sim_seed, run.fault_seed}.isdisjoint(HELD_OUT_SEEDS)


def test_named_alerts_exist_with_complete_runbooks() -> None:
    ops = load_ops_config()
    rules = {r.id for r in ops.rules}
    named = {a for f in CRITERIA.failure for a in (*f.alerts, *f.quiet)}
    assert named <= rules, named - rules
    assert validate_runbooks(ops) == []


def test_rows_without_alerts_name_the_signal_that_records_them() -> None:
    for f in CRITERIA.failure:
        assert f.alerts or f.signal, f.id
        assert not set(f.alerts) & set(f.quiet), f.id
