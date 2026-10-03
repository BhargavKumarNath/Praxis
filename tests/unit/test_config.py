from __future__ import annotations

import pytest
from pydantic import ValidationError

from praxis.config import Environment, Settings, get_settings


def test_defaults_are_local_and_need_no_secrets() -> None:
    s = Settings()
    assert s.environment is Environment.LOCAL
    assert s.database_url is None
    assert s.gcp_resource_prefix == "praxis"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PRAXIS_CLOUD_RUN_MAX_INSTANCES", "3")
    s = Settings()
    assert s.log_level.value == "DEBUG"
    assert s.cloud_run_max_instances == 3


def test_non_local_requires_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_ENVIRONMENT", "dev")
    with pytest.raises(ValidationError, match="GCP_PROJECT_ID"):
        Settings()
    monkeypatch.setenv("PRAXIS_GCP_PROJECT_ID", "praxis-dev")
    assert Settings().gcp_project_id == "praxis-dev"


@pytest.mark.parametrize("value", ["0", "11", "abc"])
def test_max_instances_bounded(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PRAXIS_CLOUD_RUN_MAX_INSTANCES", value)
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_environment_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_STRIPE_SECRET_KEY", "sk_" + "test_supersecretvalue")
    monkeypatch.setenv("PRAXIS_DATABASE_URL", "postgresql://" + "u:hunter2@h/db")
    text = repr(Settings())
    assert "supersecretvalue" not in text
    assert "hunter2" not in text


def test_settings_are_frozen_and_cached() -> None:
    s = get_settings()
    assert get_settings() is s
    with pytest.raises(ValidationError):
        s.service_name = "x"  # type: ignore[misc]
