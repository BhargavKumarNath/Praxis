"""Model lifecycle state machine (Phase 10, ADR 0017).

One registered model version moves through these stages. Retraining may be automatic;
promotion to champion is a separate, gated action (ADR 0005).

    (none)      --registered-->          CANDIDATE   (trained, lineage recorded, not validated)
    CANDIDATE   --validation.passed-->   CHALLENGER  (every offline check passed)
    CANDIDATE   --validation.failed-->   REJECTED    (terminal)
    CHALLENGER  --shadow.started-->      SHADOW      (scores live observations, serves nothing)
    CHALLENGER  --rejected-->            REJECTED    (withdrawn by an operator)
    CHALLENGER  --promote.bootstrap-->   CHAMPION    (only when the model has no champion)
    SHADOW      --canary.started-->      CANARY      (serves a small, fixed share of traffic)
    SHADOW      --promote-->             CHAMPION
    SHADOW      --shadow.failed-->       REJECTED
    CANARY      --promote-->             CHAMPION
    CANARY      --canary.failed-->       REJECTED
    CHAMPION    --replaced-->            RETIRED     (a newer champion was promoted)
    CHAMPION    --rollback-->            ROLLED_BACK (terminal: regression after promotion)
    RETIRED     --restore-->             CHAMPION    (rollback target: last known-good)

``promote.forced`` (CANDIDATE, CHALLENGER, SHADOW or CANARY -> CHAMPION) exists only for
controlled development drills (a deliberately bad champion to prove rollback). The service
refuses it outside the environments the lifecycle policy names, and every use is audited.

Invariants the control plane enforces on top of this table: at most one CHAMPION, one
SHADOW and one CANARY per model name (partial unique indexes), and a promotion or rollback
writes the old and new champion's moves in one transaction under one ``decision_id``.
"""

from __future__ import annotations

from enum import StrEnum

from praxis.domain.states import TransitionTable


class ModelStage(StrEnum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    SHADOW = "shadow"
    CANARY = "canary"
    CHAMPION = "champion"
    RETIRED = "retired"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


# Stages whose artifact is loaded by a serving process (and so must match the code's
# feature contract). CHALLENGER is validated but idle.
SERVING_STAGES = frozenset({ModelStage.SHADOW, ModelStage.CANARY, ModelStage.CHAMPION})
TERMINAL_STAGES = frozenset({ModelStage.REJECTED, ModelStage.ROLLED_BACK})
FORCEABLE_STAGES = frozenset(
    {ModelStage.CANDIDATE, ModelStage.CHALLENGER, ModelStage.SHADOW, ModelStage.CANARY}
)
# Ordered pre-champion path; a policy picks a subsequence ending in CHAMPION.
PROMOTION_PATH = (
    ModelStage.CHALLENGER,
    ModelStage.SHADOW,
    ModelStage.CANARY,
    ModelStage.CHAMPION,
)


def _table() -> dict[tuple[ModelStage, str], ModelStage]:
    s = ModelStage
    table = {
        (s.CANDIDATE, "validation.passed"): s.CHALLENGER,
        (s.CANDIDATE, "validation.failed"): s.REJECTED,
        (s.CHALLENGER, "shadow.started"): s.SHADOW,
        (s.CHALLENGER, "rejected"): s.REJECTED,
        (s.CHALLENGER, "promote.bootstrap"): s.CHAMPION,
        (s.SHADOW, "canary.started"): s.CANARY,
        (s.SHADOW, "promote"): s.CHAMPION,
        (s.SHADOW, "shadow.failed"): s.REJECTED,
        (s.CANARY, "promote"): s.CHAMPION,
        (s.CANARY, "canary.failed"): s.REJECTED,
        (s.CHAMPION, "replaced"): s.RETIRED,
        (s.CHAMPION, "rollback"): s.ROLLED_BACK,
        (s.RETIRED, "restore"): s.CHAMPION,
    }
    for stage in FORCEABLE_STAGES:
        table[(stage, "promote.forced")] = s.CHAMPION
    return table


MODEL_TRANSITIONS: TransitionTable[ModelStage] = TransitionTable(
    _table(), entry={"registered": ModelStage.CANDIDATE}
)


class RetrainingStatus(StrEnum):
    """A retraining request: drift, schedule or new labels ask for a candidate, nothing more.

    OPEN --started--> TRAINING --completed--> COMPLETED (a candidate was registered)
    OPEN / TRAINING --failed--> FAILED (training could not produce a candidate)
    """

    OPEN = "open"
    TRAINING = "training"
    COMPLETED = "completed"
    FAILED = "failed"


RETRAINING_TRANSITIONS: TransitionTable[RetrainingStatus] = TransitionTable(
    {
        (RetrainingStatus.OPEN, "started"): RetrainingStatus.TRAINING,
        (RetrainingStatus.OPEN, "failed"): RetrainingStatus.FAILED,
        (RetrainingStatus.TRAINING, "completed"): RetrainingStatus.COMPLETED,
        (RetrainingStatus.TRAINING, "failed"): RetrainingStatus.FAILED,
    },
    entry={"requested": RetrainingStatus.OPEN},
)
LIVE_RETRAINING = frozenset({RetrainingStatus.OPEN, RetrainingStatus.TRAINING})


def direct_moves[S: StrEnum](
    machine: TransitionTable[S], states: type[S]
) -> frozenset[tuple[S, S]]:
    """Every single-step (from, to) pair; the database trigger allows exactly these.

    Unlike the event-sourced machines, a model version moves one audited step at a time, so
    the control plane installs the direct edges rather than their transitive closure.
    """
    return frozenset(
        (src, machine.apply(src, trigger)) for src in states for trigger in machine.allowed(src)
    )
