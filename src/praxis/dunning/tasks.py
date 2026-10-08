"""Scheduled retry tasks: the Cloud Tasks abstraction (ADR 0015).

``TaskQueue`` is the only scheduling interface the dunning service sees. It mirrors the Cloud
Tasks semantics Praxis relies on (checked against the official docs on 2026-10-08):

* a task has an explicit name; creating a name that exists, or that was deleted or executed
  recently, fails with ``ALREADY_EXISTS`` (names stay reserved for up to 24 hours), which
  makes enqueueing idempotent;
* a scheduled or dispatched task can be deleted; an executed one cannot;
* tasks cannot be updated: a reschedule is a delete plus a create under a NEW name;
* delivery is at least once: the handler must be idempotent (``RetryExecutor`` is).

Implementations:

* ``LocalTaskQueue``: in-process, driven by an explicit clock (tests, local development,
  synthetic scale runs); it can also re-dispatch a task to exercise duplicate delivery.
* ``CloudTasksQueue``: thin REST client (httpx, bounded timeouts and retries) creating HTTP
  tasks with an OIDC token for the ``/v1/tasks/payment-retry`` endpoint. Never used in tests
  against the real service: the HTTP boundary is mocked.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

import httpx

from praxis.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)


class EnqueueStatus(StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"  # same name: the earlier create took effect


@dataclass(frozen=True, slots=True)
class RetryTask:
    name: str  # stable, hashed (Cloud Tasks recommends non-sequential names)
    job_id: str
    run_at: datetime

    def body(self) -> bytes:
        return json.dumps({"job_id": self.job_id}, separators=(",", ":")).encode()


class TaskQueue(Protocol):
    def enqueue(self, task: RetryTask) -> EnqueueStatus: ...

    def cancel(self, name: str) -> bool:
        """Delete a scheduled task; False when it does not exist (gone, executed, unknown)."""
        ...


# ------------------------------------------------------------------------------- local
@dataclass
class _Local:
    task: RetryTask
    attempts: int = 0


Handler = Callable[[str], bool]  # job_id -> done (False / exception = retry later)


@dataclass
class LocalTaskQueue:
    """Deterministic in-process queue with Cloud Tasks naming and deletion semantics."""

    retry_backoff: timedelta = timedelta(minutes=5)
    max_dispatch_attempts: int = 10
    tasks: dict[str, _Local] = field(default_factory=dict)
    tombstones: set[str] = field(default_factory=set)  # deleted or executed names
    dispatched: list[str] = field(default_factory=list)  # job ids, in dispatch order
    exhausted: list[str] = field(default_factory=list)
    history: dict[str, RetryTask] = field(default_factory=dict)  # every task ever created

    def enqueue(self, task: RetryTask) -> EnqueueStatus:
        if task.name in self.tasks or task.name in self.tombstones:
            return EnqueueStatus.ALREADY_EXISTS
        self.tasks[task.name] = _Local(task)
        self.history[task.name] = task
        return EnqueueStatus.CREATED

    def cancel(self, name: str) -> bool:
        if name not in self.tasks:
            return False
        del self.tasks[name]
        self.tombstones.add(name)
        return True

    def due(self, now: datetime) -> list[RetryTask]:
        return sorted(
            (t.task for t in self.tasks.values() if t.task.run_at <= now),
            key=lambda t: (t.run_at, t.name),
        )

    def next_run_at(self) -> datetime | None:
        return min((t.task.run_at for t in self.tasks.values()), default=None)

    def run_due(self, now: datetime, handler: Handler) -> int:
        """Dispatch every due task once; failures stay queued with backoff (at least once)."""
        done = 0
        for task in self.due(now):
            entry = self.tasks[task.name]
            entry.attempts += 1
            self.dispatched.append(task.job_id)
            try:
                ok = handler(task.job_id)
            except TransientError:
                ok = False
            if ok:
                del self.tasks[task.name]
                self.tombstones.add(task.name)
                done += 1
            elif entry.attempts >= self.max_dispatch_attempts:
                del self.tasks[task.name]
                self.tombstones.add(task.name)
                self.exhausted.append(task.job_id)
            else:
                entry.task = RetryTask(task.name, task.job_id, now + self.retry_backoff)
        return done

    def redeliver(self, name: str, handler: Handler) -> bool:
        """Dispatch a task again (Cloud Tasks may deliver a task more than once)."""
        task = self.history[name]
        self.dispatched.append(task.job_id)
        return handler(task.job_id)


# ------------------------------------------------------------------------- Cloud Tasks
@dataclass(frozen=True)
class CloudTasksTarget:
    project: str
    location: str
    queue: str
    url: str  # https://<service>/v1/tasks/payment-retry
    service_account_email: str  # OIDC identity Cloud Tasks uses to call the endpoint
    audience: str | None = None
    dispatch_deadline_s: int = 60
    task_token: str | None = field(default=None, repr=False)  # X-Praxis-Task-Token

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/queues/{self.queue}"


TokenProvider = Callable[[], str]
_API = "https://cloudtasks.googleapis.com/v2"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def google_token_provider() -> TokenProvider:  # pragma: no cover - needs real credentials
    """Application Default Credentials; imported lazily (no import-time network)."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(Request())  # type: ignore[no-untyped-call]
        return str(credentials.token)

    return token


class CloudTasksQueue:
    def __init__(
        self,
        target: CloudTasksTarget,
        token: TokenProvider,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self.target = target
        self._token = token
        self._client = client or httpx.Client(timeout=timeout_s)
        self._max_retries = max_retries

    def _request(
        self, method: str, url: str, payload: dict[str, object] | None = None
    ) -> httpx.Response:
        last: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                continue
            if response.status_code in _RETRYABLE:
                last = TransientError(f"cloud tasks {method} {response.status_code}")
                continue
            return response
        raise TransientError(f"cloud tasks unavailable: {type(last).__name__}") from last

    def task_path(self, name: str) -> str:
        return f"{self.target.parent}/tasks/{name}"

    def enqueue(self, task: RetryTask) -> EnqueueStatus:
        t = self.target
        oidc: dict[str, str] = {"serviceAccountEmail": t.service_account_email}
        if t.audience:
            oidc["audience"] = t.audience
        headers = {"Content-Type": "application/json"}
        if t.task_token:
            headers["X-Praxis-Task-Token"] = t.task_token
        payload: dict[str, object] = {
            "task": {
                "name": self.task_path(task.name),
                "scheduleTime": task.run_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "dispatchDeadline": f"{t.dispatch_deadline_s}s",
                "httpRequest": {
                    "httpMethod": "POST",
                    "url": t.url,
                    "headers": headers,
                    "body": base64.b64encode(task.body()).decode(),
                    "oidcToken": oidc,
                },
            }
        }
        response = self._request("POST", f"{_API}/{t.parent}/tasks", payload)
        if response.status_code == 409:
            return EnqueueStatus.ALREADY_EXISTS
        if response.is_success:
            return EnqueueStatus.CREATED
        raise PermanentError("cloud_tasks_create", f"HTTP {response.status_code}")

    def cancel(self, name: str) -> bool:
        response = self._request("DELETE", f"{_API}/{self.task_path(name)}")
        if response.status_code == 404:
            return False
        if response.is_success:
            return True
        raise PermanentError("cloud_tasks_delete", f"HTTP {response.status_code}")
