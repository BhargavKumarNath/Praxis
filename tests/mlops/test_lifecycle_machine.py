"""Model lifecycle and retraining-request state machines (pure, exhaustive + property)."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from praxis.domain.model_lifecycle import (
    FORCEABLE_STAGES,
    MODEL_TRANSITIONS,
    RETRAINING_TRANSITIONS,
    SERVING_STAGES,
    TERMINAL_STAGES,
    ModelStage,
    RetrainingStatus,
    direct_moves,
)
from praxis.domain.states import InvalidTransition

S = ModelStage


def test_every_version_starts_as_candidate() -> None:
    assert MODEL_TRANSITIONS.start("registered") == S.CANDIDATE
    with pytest.raises(InvalidTransition):
        MODEL_TRANSITIONS.start("promote")


@pytest.mark.parametrize(
    ("stage", "trigger", "target"),
    [
        (S.CANDIDATE, "validation.passed", S.CHALLENGER),
        (S.CANDIDATE, "validation.failed", S.REJECTED),
        (S.CHALLENGER, "shadow.started", S.SHADOW),
        (S.CHALLENGER, "promote.bootstrap", S.CHAMPION),
        (S.SHADOW, "promote", S.CHAMPION),
        (S.SHADOW, "shadow.failed", S.REJECTED),
        (S.CANARY, "promote", S.CHAMPION),
        (S.CHAMPION, "replaced", S.RETIRED),
        (S.CHAMPION, "rollback", S.ROLLED_BACK),
        (S.RETIRED, "restore", S.CHAMPION),
    ],
)
def test_allowed_moves(stage: ModelStage, trigger: str, target: ModelStage) -> None:
    assert MODEL_TRANSITIONS.apply(stage, trigger) == target


@pytest.mark.parametrize(
    ("stage", "trigger"),
    [
        (S.CANDIDATE, "promote"),  # no unvalidated promotion (only promote.forced, a drill)
        (S.CANDIDATE, "shadow.started"),
        (S.CHALLENGER, "promote"),  # must pass shadow first
        (S.REJECTED, "validation.passed"),
        (S.ROLLED_BACK, "restore"),  # a rolled-back version never serves again
        (S.RETIRED, "promote"),
        (S.CHAMPION, "promote"),
    ],
)
def test_forbidden_moves(stage: ModelStage, trigger: str) -> None:
    with pytest.raises(InvalidTransition):
        MODEL_TRANSITIONS.apply(stage, trigger)


def test_terminal_stages_have_no_exit() -> None:
    for stage in TERMINAL_STAGES:
        assert MODEL_TRANSITIONS.allowed(stage) == frozenset()


def test_champion_is_reached_only_by_promotion_or_restore() -> None:
    into_champion = {
        t
        for s in ModelStage
        for t in MODEL_TRANSITIONS.allowed(s)
        if MODEL_TRANSITIONS.apply(s, t) == S.CHAMPION
    }
    assert into_champion == {"promote", "promote.bootstrap", "promote.forced", "restore"}
    assert {s for s in ModelStage if (s, "promote.forced") in direct_moves_triggers()} == set(
        FORCEABLE_STAGES
    )


def direct_moves_triggers() -> set[tuple[ModelStage, str]]:
    return {(s, t) for s in ModelStage for t in MODEL_TRANSITIONS.allowed(s)}


def test_direct_moves_are_single_steps_not_the_closure() -> None:
    moves = direct_moves(MODEL_TRANSITIONS, ModelStage)
    assert (S.CANDIDATE, S.CHALLENGER) in moves
    assert (S.CANDIDATE, S.SHADOW) not in moves  # reachable in two steps, not one
    assert (S.CHAMPION, S.CHAMPION) not in moves
    assert S.CHAMPION in SERVING_STAGES and S.CHALLENGER not in SERVING_STAGES


@given(st.lists(st.sampled_from(sorted(MODEL_TRANSITIONS.triggers)), max_size=40))
def test_random_trigger_sequences_never_leave_the_machine(triggers: list[str]) -> None:
    stage = MODEL_TRANSITIONS.start("registered")
    seen = [stage]
    for trigger in triggers:
        try:
            stage = MODEL_TRANSITIONS.apply(stage, trigger)
        except InvalidTransition:
            continue
        seen.append(stage)
    moves = direct_moves(MODEL_TRANSITIONS, ModelStage)
    assert all(pair in moves for pair in itertools.pairwise(seen))
    if S.ROLLED_BACK in seen:  # absorbing
        assert seen[-1] == S.ROLLED_BACK


def test_retraining_request_machine() -> None:
    r = RetrainingStatus
    assert RETRAINING_TRANSITIONS.start("requested") == r.OPEN
    assert RETRAINING_TRANSITIONS.apply(r.OPEN, "started") == r.TRAINING
    assert RETRAINING_TRANSITIONS.apply(r.TRAINING, "completed") == r.COMPLETED
    assert RETRAINING_TRANSITIONS.apply(r.TRAINING, "failed") == r.FAILED
    with pytest.raises(InvalidTransition):
        RETRAINING_TRANSITIONS.apply(r.COMPLETED, "started")
