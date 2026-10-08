"""Task queue: local Cloud-Tasks semantics and the REST adapter (HTTP boundary mocked)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from praxis.dunning.tasks import (
    CloudTasksQueue,
    CloudTasksTarget,
    EnqueueStatus,
    LocalTaskQueue,
    RetryTask,
)
from praxis.errors import PermanentError, TransientError

T0 = datetime(2026, 10, 8, 12, tzinfo=UTC)


def task(name: str = "retry-a", minutes: int = 0, job: str = "job-a") -> RetryTask:
    return RetryTask(name, job, T0 + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------- local
def test_names_deduplicate_including_deleted_and_executed() -> None:
    q = LocalTaskQueue()
    assert q.enqueue(task()) is EnqueueStatus.CREATED
    assert q.enqueue(task()) is EnqueueStatus.ALREADY_EXISTS  # duplicate scheduling request
    assert q.cancel("retry-a") is True
    assert q.cancel("retry-a") is False  # already gone
    assert q.enqueue(task()) is EnqueueStatus.ALREADY_EXISTS  # name stays reserved
    assert q.cancel("never") is False


def test_due_tasks_run_in_time_order_and_leave_the_queue() -> None:
    q = LocalTaskQueue()
    q.enqueue(task("b", 10, "job-b"))
    q.enqueue(task("a", 5, "job-a"))
    q.enqueue(task("c", 60, "job-c"))
    assert q.next_run_at() == T0 + timedelta(minutes=5)
    assert [t.job_id for t in q.due(T0 + timedelta(minutes=30))] == ["job-a", "job-b"]
    seen: list[str] = []

    def handler(job: str) -> bool:
        seen.append(job)
        return True

    assert q.run_due(T0 + timedelta(minutes=30), handler) == 2
    assert seen == ["job-a", "job-b"] and set(q.tasks) == {"c"}
    assert q.enqueue(task("a")) is EnqueueStatus.ALREADY_EXISTS  # executed names stay reserved


def test_failures_are_redelivered_with_backoff_then_exhausted() -> None:
    q = LocalTaskQueue(retry_backoff=timedelta(minutes=5), max_dispatch_attempts=3)
    q.enqueue(task())
    calls: list[str] = []

    def flaky(job: str) -> bool:
        calls.append(job)
        if len(calls) == 1:
            raise TransientError("db down")
        return False

    assert q.run_due(T0, flaky) == 0
    assert q.tasks["retry-a"].task.run_at == T0 + timedelta(minutes=5)
    assert q.run_due(T0 + timedelta(minutes=1), flaky) == 0  # not due yet
    q.run_due(T0 + timedelta(minutes=5), flaky)
    q.run_due(T0 + timedelta(minutes=10), flaky)
    assert q.exhausted == ["job-a"] and not q.tasks and len(calls) == 3


def test_redeliver_dispatches_an_executed_task_again() -> None:
    q = LocalTaskQueue()
    q.enqueue(task())
    q.run_due(T0, lambda _: True)
    assert q.redeliver("retry-a", lambda j: j == "job-a") is True
    assert q.dispatched == ["job-a", "job-a"]
    with pytest.raises(KeyError):
        q.redeliver("unknown", lambda _: True)


# ---------------------------------------------------------------------- Cloud Tasks
TARGET = CloudTasksTarget(
    project="p",
    location="europe-west2",
    queue="praxis-dev-payment-retries",
    url="https://svc.example/v1/tasks/payment-retry",
    service_account_email="sa@p.iam",
    audience="https://svc.example",
    task_token="t0k",
)


def cloud(handler: object) -> CloudTasksQueue:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CloudTasksQueue(TARGET, lambda: "bearer-x", client=client)


def test_create_builds_a_named_oidc_http_task() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    assert cloud(handler).enqueue(task("retry-abc")) is EnqueueStatus.CREATED
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == (
        "https://cloudtasks.googleapis.com/v2/projects/p/locations/europe-west2/queues/"
        "praxis-dev-payment-retries/tasks"
    )
    assert req.headers["Authorization"] == "Bearer bearer-x"
    body = json.loads(req.content)["task"]
    assert body["name"].endswith("/queues/praxis-dev-payment-retries/tasks/retry-abc")
    assert body["scheduleTime"] == "2026-10-08T12:00:00Z"
    http = body["httpRequest"]
    assert http["oidcToken"] == {
        "serviceAccountEmail": "sa@p.iam",
        "audience": "https://svc.example",
    }
    assert http["headers"]["X-Praxis-Task-Token"] == "t0k"
    assert json.loads(base64.b64decode(http["body"])) == {"job_id": "job-a"}
    assert "t0k" not in repr(TARGET)


def test_status_mapping_and_bounded_retries() -> None:
    assert cloud(lambda r: httpx.Response(409)).enqueue(task()) is EnqueueStatus.ALREADY_EXISTS
    assert cloud(lambda r: httpx.Response(404)).cancel("x") is False
    assert cloud(lambda r: httpx.Response(200, json={})).cancel("x") is True
    with pytest.raises(PermanentError):
        cloud(lambda r: httpx.Response(403)).enqueue(task())
    with pytest.raises(PermanentError):
        cloud(lambda r: httpx.Response(400)).cancel("x")
    attempts: list[int] = []

    def busy(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503)

    with pytest.raises(TransientError):
        cloud(busy).enqueue(task())
    assert len(attempts) == 3  # 1 + max_retries

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError, match="ConnectError"):
        cloud(down).cancel("x")
    flaky = iter([httpx.Response(500), httpx.Response(200, json={})])
    assert cloud(lambda r: next(flaky)).enqueue(task()) is EnqueueStatus.CREATED
