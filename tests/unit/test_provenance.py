from __future__ import annotations

import re
import subprocess

import pytest

from praxis import provenance


def test_code_revision_is_a_short_sha_with_optional_dirty_flag() -> None:
    assert re.fullmatch(r"[0-9a-f]{7,12}(-dirty)?|unknown", provenance.code_revision())


def test_code_revision_marks_uncommitted_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"rev-parse": "abc1234", "status": " M src/x.py"}
    monkeypatch.setattr(provenance, "_git", lambda *a: answers[a[0]])
    assert provenance.code_revision() == "abc1234-dirty"
    answers["status"] = ""
    assert provenance.code_revision() == "abc1234"


def test_code_revision_without_git_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert provenance.code_revision() == "unknown"
