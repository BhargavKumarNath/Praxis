"""Dunning and retry-job machines: every allowed and every forbidden transition (gate item 1)."""

from __future__ import annotations

from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from praxis.domain.dunning import (
    DUNNING_TRANSITIONS,
    LIVE_JOB_STATES,
    OPEN_DUNNING_STATES,
    RETRY_JOB_TRANSITIONS,
    TERMINAL_DUNNING_STATES,
    Access,
    DunningState,
    RetryJobState,
    access_of,
)
from praxis.domain.states import InvalidTransition

D = DunningState
J = RetryJobState

# Written out by hand (not derived from the module) so the test is an independent spec.
DUNNING_ALLOWED = {
    (D.PAST_DUE, "decision.grace"): D.GRACE,
    (D.PAST_DUE, "decision.restrict"): D.RESTRICTED,
    (D.PAST_DUE, "decision.suspend"): D.SUSPENDED,
    (D.PAST_DUE, "payment.recovered"): D.RECOVERED,
    (D.PAST_DUE, "subscription.cancelled"): D.CLOSED,
    (D.GRACE, "decision.grace"): D.GRACE,
    (D.GRACE, "decision.restrict"): D.RESTRICTED,
    (D.GRACE, "decision.suspend"): D.SUSPENDED,
    (D.GRACE, "payment.recovered"): D.RECOVERED,
    (D.GRACE, "subscription.cancelled"): D.CLOSED,
    (D.RESTRICTED, "decision.restrict"): D.RESTRICTED,
    (D.RESTRICTED, "decision.suspend"): D.SUSPENDED,
    (D.RESTRICTED, "payment.recovered"): D.RECOVERED,
    (D.RESTRICTED, "subscription.cancelled"): D.CLOSED,
    (D.SUSPENDED, "payment.recovered"): D.RECOVERED,
    (D.SUSPENDED, "subscription.cancelled"): D.CLOSED,
}
DUNNING_ENTRY = {"payment.failed": D.PAST_DUE, "payment.recovered": D.RECOVERED}

JOB_ALLOWED = {
    (J.SCHEDULED, "job.started"): J.EXECUTING,
    (J.SCHEDULED, "job.cancelled"): J.CANCELLED,
    (J.SCHEDULED, "job.superseded"): J.SUPERSEDED,
    (J.SCHEDULED, "job.expired"): J.EXPIRED,
    (J.EXECUTING, "job.succeeded"): J.SUCCEEDED,
    (J.EXECUTING, "job.failed"): J.FAILED,
}

DUNNING_TRIGGERS = sorted(DUNNING_TRANSITIONS.triggers)
JOB_TRIGGERS = sorted(RETRY_JOB_TRANSITIONS.triggers)


@pytest.mark.parametrize(("key", "expected"), list(DUNNING_ALLOWED.items()))
def test_every_allowed_dunning_transition(key: tuple[D, str], expected: D) -> None:
    assert DUNNING_TRANSITIONS.apply(*key) is expected


DUNNING_FORBIDDEN = [k for k in product(D, DUNNING_TRIGGERS) if k not in DUNNING_ALLOWED]
JOB_FORBIDDEN = [k for k in product(J, JOB_TRIGGERS) if k not in JOB_ALLOWED]


@pytest.mark.parametrize(("state", "trigger"), DUNNING_FORBIDDEN)
def test_every_forbidden_dunning_transition_fails(state: D, trigger: str) -> None:
    with pytest.raises(InvalidTransition):
        DUNNING_TRANSITIONS.apply(state, trigger)


def test_allowed_and_forbidden_partition_every_pair() -> None:
    assert len(DUNNING_FORBIDDEN) + len(DUNNING_ALLOWED) == len(D) * len(DUNNING_TRIGGERS)
    assert len(JOB_FORBIDDEN) + len(JOB_ALLOWED) == len(J) * len(JOB_TRIGGERS)


def test_dunning_table_is_exactly_the_specification() -> None:
    allowed = {
        (s, t) for s, t in product(D, DUNNING_TRIGGERS) if t in DUNNING_TRANSITIONS.allowed(s)
    }
    assert allowed == set(DUNNING_ALLOWED)
    assert {t: DUNNING_TRANSITIONS.start(t) for t in DUNNING_TRANSITIONS.entries()} == (
        DUNNING_ENTRY
    )


@pytest.mark.parametrize("trigger", sorted(set(DUNNING_TRIGGERS) - set(DUNNING_ENTRY)))
def test_cases_open_only_on_failure_or_recovery(trigger: str) -> None:
    with pytest.raises(InvalidTransition):
        DUNNING_TRANSITIONS.start(trigger)


@pytest.mark.parametrize(("key", "expected"), list(JOB_ALLOWED.items()))
def test_every_allowed_job_transition(key: tuple[J, str], expected: J) -> None:
    assert RETRY_JOB_TRANSITIONS.apply(*key) is expected


@pytest.mark.parametrize(("state", "trigger"), JOB_FORBIDDEN)
def test_every_forbidden_job_transition_fails(state: J, trigger: str) -> None:
    with pytest.raises(InvalidTransition):
        RETRY_JOB_TRANSITIONS.apply(state, trigger)


def test_job_table_is_exactly_the_specification() -> None:
    allowed = {(s, t) for s, t in product(J, JOB_TRIGGERS) if t in RETRY_JOB_TRANSITIONS.allowed(s)}
    assert allowed == set(JOB_ALLOWED)
    assert RETRY_JOB_TRANSITIONS.start("job.scheduled") is J.SCHEDULED
    with pytest.raises(InvalidTransition):
        RETRY_JOB_TRANSITIONS.start("job.started")


def test_executing_job_cannot_be_cancelled_or_superseded() -> None:
    for trigger in ("job.cancelled", "job.superseded", "job.expired"):
        with pytest.raises(InvalidTransition):
            RETRY_JOB_TRANSITIONS.apply(J.EXECUTING, trigger)


def test_terminal_states_absorb() -> None:
    for state in TERMINAL_DUNNING_STATES:
        assert DUNNING_TRANSITIONS.allowed(state) == frozenset()
    for job_state in set(J) - LIVE_JOB_STATES:
        assert RETRY_JOB_TRANSITIONS.allowed(job_state) == frozenset()


def test_access_never_relaxes_without_payment() -> None:
    rank = {Access.FULL: 0, Access.LIMITED: 1, Access.NONE: 2}
    for (src, trigger), dst in DUNNING_ALLOWED.items():
        if trigger.startswith("decision."):
            assert rank[access_of(dst)] >= rank[access_of(src)], (src, trigger, dst)
    assert access_of(D.RECOVERED) is Access.FULL
    assert {access_of(s) for s in OPEN_DUNNING_STATES} == set(Access)


@given(st.lists(st.sampled_from(DUNNING_TRIGGERS), max_size=30))
def test_random_trigger_sequences_stay_on_the_machine(triggers: list[str]) -> None:
    """Applying any sequence (forbidden steps rejected) never leaves the reachable set."""
    reachable = DUNNING_TRANSITIONS.reachable_pairs()
    state = DUNNING_TRANSITIONS.start("payment.failed")
    for trigger in triggers:
        try:
            nxt = DUNNING_TRANSITIONS.apply(state, trigger)
        except InvalidTransition:
            continue
        assert (state, nxt) in reachable
        if state in TERMINAL_DUNNING_STATES:
            raise AssertionError("a terminal state moved")
        state = nxt


@given(st.lists(st.sampled_from(JOB_TRIGGERS), max_size=12))
def test_a_job_executes_at_most_once(triggers: list[str]) -> None:
    state = RETRY_JOB_TRANSITIONS.start("job.scheduled")
    started = 0
    for trigger in triggers:
        try:
            state = RETRY_JOB_TRANSITIONS.apply(state, trigger)
        except InvalidTransition:
            continue
        started += trigger == "job.started"
    assert started <= 1
