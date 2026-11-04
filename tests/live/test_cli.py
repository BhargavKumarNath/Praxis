"""``python -m praxis.live``: exit codes, refusals and the root resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from praxis.live import __main__ as cli
from praxis.live.loop import LiveError, StepFailed, StepResult


def _out(capsys: pytest.CaptureFixture[str]) -> Any:
    text = capsys.readouterr().out
    return json.loads(text[text.rindex("\n{") + 1 :] if "\n{" in text else text)


def test_root_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_LIVE_ROOT", raising=False)
    with pytest.raises(SystemExit) as err:
        cli.main(["status"])
    assert err.value.code == 2


def test_status_reads_state_and_newest_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRAXIS_LIVE_ROOT", str(tmp_path))
    assert cli.main(["status"]) == 0
    assert _out(capsys) == {"last_as_of": None, "newest_report": None}
    (tmp_path / "state.json").write_text('{"last_as_of": "2026-11-03"}')
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "2026-11-02.json").write_text('{"n": 2}')
    (tmp_path / "reports" / "2026-11-03.json").write_text('{"n": 3}')
    (tmp_path / "reports" / "bootstrap-2026-08-30.json").write_text('{"n": 0}')  # sorts last
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    assert _out(capsys) == {"last_as_of": "2026-11-03", "newest_report": {"n": 3}}


class _Loop:
    behaviour: Any = None

    def __init__(self, *_: Any) -> None:
        self.results = [StepResult("simulate", 0, 1.0, None)]

    def tick(self, as_of: Any) -> dict[str, Any]:
        return self._do({"tick": str(as_of)})

    def bootstrap(self, approver: str) -> dict[str, Any]:
        return self._do({"approver": approver})

    def republish(self) -> dict[str, Any]:
        return self._do({"status": "republished"})

    def _do(self, ok: dict[str, Any]) -> dict[str, Any]:
        if isinstance(_Loop.behaviour, BaseException):
            raise _Loop.behaviour
        return ok


@pytest.mark.parametrize(
    ("behaviour", "code", "key"),
    [
        (None, 0, "tick"),
        (LiveError("not bootstrapped"), 2, "refused"),
        (StepFailed(StepResult("dbt", 1, 2.0, None)), 1, "failed"),
    ],
)
def test_tick_exit_codes(
    behaviour: Any, code: int, key: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setattr(cli, "LiveLoop", _Loop)
    _Loop.behaviour = behaviour
    assert cli.main(["--root", str(tmp_path), "tick", "--as-of", "2026-11-04"]) == code
    out = _out(capsys)
    assert key in out
    if key == "failed":
        assert out["failed"]["step"] == "dbt" and out["steps"][0]["step"] == "simulate"


def test_bootstrap_passes_the_approver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "LiveLoop", _Loop)
    _Loop.behaviour = None
    assert cli.main(["--root", str(tmp_path), "bootstrap", "--approver", "owner"]) == 0
    assert _out(capsys) == {"approver": "owner"}


def test_republish_is_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "LiveLoop", _Loop)
    _Loop.behaviour = None
    assert cli.main(["--root", str(tmp_path), "republish"]) == 0
    assert _out(capsys) == {"status": "republished"}
