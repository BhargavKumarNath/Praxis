"""Failure taxonomy shared by every layer (control plane, transports, consumers).

Lives at the foundation so the control plane can classify a database failure as
retryable without depending on the streaming layer (enforced by import-linter).
"""

from __future__ import annotations


class TransientError(RuntimeError):
    """A failure that may succeed on retry or redelivery (database down, timeout, deadlock)."""


class PermanentError(RuntimeError):
    """A failure that can never succeed for this input; dead-letter or reject immediately."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
