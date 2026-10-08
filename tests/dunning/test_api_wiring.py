"""Retry-task endpoint (auth, validation, status mapping) and the cloud wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from praxis.api.app import create_app
from praxis.api.tasks import TOKEN_HEADER
from praxis.config import Settings
from praxis.dunning.executor import ExecResult, ExecStatus, RetryExecutor
from praxis.dunning.wiring import build_cloud_executor, load_recovery_artifact
from praxis.errors import TransientError
from tests.recovery.conftest import build_artifact

TOKEN = "task-token-" + "x" * 8


def client(handler: object = None, token: str | None = TOKEN) -> TestClient:
    settings = Settings(tasks_token=SecretStr(token) if token else None)
    return TestClient(create_app(settings, retry_handler=handler))  # type: ignore[arg-type]


def ok(status: ExecStatus) -> object:
    return lambda job_id: ExecResult(status, job_id)


def post(c: TestClient, body: object, token: str | None = TOKEN) -> object:
    headers = {TOKEN_HEADER: token} if token else {}
    return c.post("/v1/tasks/payment-retry", json=body, headers=headers)


def test_handled_statuses_are_acknowledged() -> None:
    for status in (
        ExecStatus.SUCCEEDED,
        ExecStatus.FAILED,
        ExecStatus.STALE,
        ExecStatus.CANCELLED,
        ExecStatus.EXPIRED,
    ):
        r = post(client(ok(status)), {"job_id": "job-1"})
        assert r.status_code == 200 and r.json() == {"status": status.value, "job_id": "job-1"}  # type: ignore[attr-defined]


def test_rejections_and_retryable_answers() -> None:
    c = client(ok(ExecStatus.SUCCEEDED))
    assert post(c, {"job_id": "job-1"}, token=None).status_code == 401  # type: ignore[attr-defined]
    assert post(c, {"job_id": "job-1"}, token="wrong").status_code == 401  # type: ignore[attr-defined]
    assert post(c, {"job_id": "bad id; drop"}).status_code == 422  # type: ignore[attr-defined]
    assert post(c, {}).status_code == 422  # type: ignore[attr-defined]
    assert post(client(ok(ExecStatus.TOO_EARLY)), {"job_id": "j"}).json()["error"] == "too_early"  # type: ignore[attr-defined]

    def flaky(job_id: str) -> ExecResult:
        raise TransientError("db down")

    r = post(client(flaky), {"job_id": "j"})
    assert r.status_code == 503 and r.json()["error"] == "transient_failure"  # type: ignore[attr-defined]
    assert post(client(None), {"job_id": "j"}).status_code == 503  # type: ignore[attr-defined]
    assert post(client(ok(ExecStatus.SUCCEEDED), token=None), {"job_id": "j"}).status_code == 503  # type: ignore[attr-defined]


FULL = {
    "database_url": SecretStr("postgresql+psycopg://u@127.0.0.1:1/x"),
    "stripe_secret_key": SecretStr("sk_test_" + "a" * 24),
    "tasks_token": SecretStr(TOKEN),
    "gcp_project_id": "praxis-dev",
    "tasks_target_url": "https://svc.example/v1/tasks/payment-retry",
    "tasks_service_account": "dunning@praxis-dev.iam.gserviceaccount.com",
}


def test_wiring_needs_every_setting_and_builds_the_cloud_stack(tmp_path: Path) -> None:
    assert build_cloud_executor(Settings()) is None
    partial = {k: v for k, v in FULL.items() if k != "tasks_service_account"}
    assert build_cloud_executor(Settings(**partial)) is None  # type: ignore[arg-type]
    built = build_cloud_executor(Settings(**FULL), token_factory=lambda: lambda: "t")  # type: ignore[arg-type]
    assert isinstance(built, RetryExecutor) and set(built.chargers) == {"stripe"}
    assert built.service.decider.artifact is None  # no model dir: baseline policy
    queue = built.service.queue
    assert queue.target.queue == "praxis-local-payment-retries"  # type: ignore[attr-defined]


def test_artifact_loading_is_optional_and_safe(tmp_path: Path) -> None:
    assert load_recovery_artifact(None) is None
    assert load_recovery_artifact(tmp_path) is None
    path = build_artifact(tmp_path / "models")
    assert load_recovery_artifact(tmp_path / "models") is None  # a root never auto-promotes
    assert load_recovery_artifact(path) is not None
    (path / "survival.json").write_text("{}")
    assert load_recovery_artifact(path) is None  # corrupt -> baseline, never a crash


def test_app_without_task_settings_answers_503() -> None:
    c = TestClient(create_app(Settings()))
    assert c.post("/v1/tasks/payment-retry", json={"job_id": "j"}).status_code == 503


@pytest.mark.parametrize("field", ["tasks_token", "stripe_secret_key"])
def test_secrets_never_render(field: str) -> None:
    assert "x" * 8 not in repr(Settings(**{field: SecretStr("x" * 8)}))  # type: ignore[arg-type]
