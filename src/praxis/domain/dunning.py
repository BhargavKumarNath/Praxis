"""Dunning state machines (Phase 8, ADR 0015).

Two machines, kept apart on purpose: a case's *access stage* (what the customer may still
do) and a retry job's *execution status* (whether a charge may be sent) change for
different reasons and at different times. Folding them into one machine ("retry_scheduled"
as a stage) would multiply states (restricted-with-retry, grace-with-retry, ...) without
adding a guarantee.

Dunning case (one per invoice whose collection failed)
    (none)      --payment.failed-->          PAST_DUE    (case opened, no decision yet)
    (none)      --payment.recovered-->       RECOVERED   (a recovery seen before any failure:
                                                          out-of-order delivery)
    PAST_DUE    --decision.grace-->          GRACE       (full access, retry planned)
    PAST_DUE    --decision.restrict-->       RESTRICTED
    PAST_DUE    --decision.suspend-->        SUSPENDED
    GRACE       --decision.grace-->          GRACE       (a retry failed inside the grace window)
    GRACE       --decision.restrict-->       RESTRICTED
    GRACE       --decision.suspend-->        SUSPENDED
    RESTRICTED  --decision.restrict-->       RESTRICTED  (re-planned, still restricted)
    RESTRICTED  --decision.suspend-->        SUSPENDED
    any open    --payment.recovered-->       RECOVERED   (terminal)
    any open    --subscription.cancelled-->  CLOSED      (terminal)

Access never relaxes without payment: there is no RESTRICTED -> GRACE and no
SUSPENDED -> RESTRICTED. A suspended customer who pays is RECOVERED.

Retry job (one scheduled charge attempt; ``attempt_number`` >= 2)
    (none)     --job.scheduled-->   SCHEDULED
    SCHEDULED  --job.started-->     EXECUTING   (the charge may now reach the provider)
    SCHEDULED  --job.cancelled-->   CANCELLED   (invoice recovered / case closed / stale)
    SCHEDULED  --job.superseded-->  SUPERSEDED  (rescheduled: a new job replaces it)
    SCHEDULED  --job.expired-->     EXPIRED     (dispatched too late to be meaningful)
    EXECUTING  --job.succeeded-->   SUCCEEDED
    EXECUTING  --job.failed-->      FAILED

Once EXECUTING, a job cannot be cancelled: the provider may already have the charge, so its
outcome must be recorded. A re-dispatched EXECUTING job repeats the provider call with the
same idempotency key, which returns the first result instead of charging twice.
"""

from __future__ import annotations

from enum import StrEnum

from praxis.domain.states import TransitionTable


class DunningState(StrEnum):
    PAST_DUE = "past_due"
    GRACE = "grace"
    RESTRICTED = "restricted"
    SUSPENDED = "suspended"
    RECOVERED = "recovered"
    CLOSED = "closed"


class Access(StrEnum):
    FULL = "full"
    LIMITED = "limited"
    NONE = "none"


class RetryJobState(StrEnum):
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


OPEN_DUNNING_STATES = frozenset(
    {DunningState.PAST_DUE, DunningState.GRACE, DunningState.RESTRICTED, DunningState.SUSPENDED}
)
TERMINAL_DUNNING_STATES = frozenset({DunningState.RECOVERED, DunningState.CLOSED})
LIVE_JOB_STATES = frozenset({RetryJobState.SCHEDULED, RetryJobState.EXECUTING})

# Decision triggers by the access stage a policy asks for.
DECISION_TRIGGER = {
    DunningState.GRACE: "decision.grace",
    DunningState.RESTRICTED: "decision.restrict",
    DunningState.SUSPENDED: "decision.suspend",
}

_ACCESS = {
    DunningState.PAST_DUE: Access.FULL,
    DunningState.GRACE: Access.FULL,
    DunningState.RESTRICTED: Access.LIMITED,
    DunningState.SUSPENDED: Access.NONE,
    DunningState.RECOVERED: Access.FULL,
    DunningState.CLOSED: Access.NONE,
}


def access_of(state: DunningState) -> Access:
    return _ACCESS[state]


def _dunning_table() -> dict[tuple[DunningState, str], DunningState]:
    s = DunningState
    table: dict[tuple[DunningState, str], DunningState] = {
        (s.PAST_DUE, "decision.grace"): s.GRACE,
        (s.PAST_DUE, "decision.restrict"): s.RESTRICTED,
        (s.PAST_DUE, "decision.suspend"): s.SUSPENDED,
        (s.GRACE, "decision.grace"): s.GRACE,
        (s.GRACE, "decision.restrict"): s.RESTRICTED,
        (s.GRACE, "decision.suspend"): s.SUSPENDED,
        (s.RESTRICTED, "decision.restrict"): s.RESTRICTED,
        (s.RESTRICTED, "decision.suspend"): s.SUSPENDED,
    }
    for open_state in OPEN_DUNNING_STATES:
        table[(open_state, "payment.recovered")] = s.RECOVERED
        table[(open_state, "subscription.cancelled")] = s.CLOSED
    return table


DUNNING_TRANSITIONS: TransitionTable[DunningState] = TransitionTable(
    _dunning_table(),
    entry={"payment.failed": DunningState.PAST_DUE, "payment.recovered": DunningState.RECOVERED},
)

RETRY_JOB_TRANSITIONS: TransitionTable[RetryJobState] = TransitionTable(
    {
        (RetryJobState.SCHEDULED, "job.started"): RetryJobState.EXECUTING,
        (RetryJobState.SCHEDULED, "job.cancelled"): RetryJobState.CANCELLED,
        (RetryJobState.SCHEDULED, "job.superseded"): RetryJobState.SUPERSEDED,
        (RetryJobState.SCHEDULED, "job.expired"): RetryJobState.EXPIRED,
        (RetryJobState.EXECUTING, "job.succeeded"): RetryJobState.SUCCEEDED,
        (RetryJobState.EXECUTING, "job.failed"): RetryJobState.FAILED,
    },
    entry={"job.scheduled": RetryJobState.SCHEDULED},
)
