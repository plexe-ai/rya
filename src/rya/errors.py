"""Stable error codes + semantic exit codes.

Every failure surfaced to a coding agent should carry a *stable* string code
(``E_*``) plus a process exit code, so Claude Code / Codex / Cursor can branch on
the failure deterministically instead of scraping human prose.
"""

from __future__ import annotations

from typing import Optional


# Exit code buckets. Kept small and semantic so a coding agent can switch on them.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2  # reserved for Typer usage errors
EXIT_MANIFEST = 3
EXIT_NOT_FOUND = 4
EXIT_PERMISSION = 5
EXIT_STATE = 6  # invalid state transition
EXIT_VALIDATION = 7


# code -> default exit code
_CODE_EXIT = {
    "E_MANIFEST_NOT_FOUND": EXIT_MANIFEST,
    "E_MANIFEST_INVALID": EXIT_MANIFEST,
    "E_ENTRYPOINT_NOT_FOUND": EXIT_MANIFEST,
    "E_AGENT_NOT_DEFINED": EXIT_MANIFEST,
    "E_HANDLER_NOT_FOUND": EXIT_NOT_FOUND,
    "E_TOOL_NOT_FOUND": EXIT_NOT_FOUND,
    "E_TOOL_PERMISSION_DENIED": EXIT_PERMISSION,
    "E_MODEL_NOT_FOUND": EXIT_NOT_FOUND,
    "E_RUN_NOT_FOUND": EXIT_NOT_FOUND,
    "E_APPROVAL_NOT_FOUND": EXIT_NOT_FOUND,
    "E_APPROVAL_NOT_PENDING": EXIT_STATE,
    "E_RUN_NOT_PAUSED": EXIT_STATE,
    "E_JOB_NOT_FOUND": EXIT_NOT_FOUND,
    "E_QUEUE_CONFLICT": EXIT_STATE,
    "E_MODEL_ROUTE_NOT_FOUND": EXIT_NOT_FOUND,
    "E_GROUNDING_BLOCKED": EXIT_PERMISSION,
    "E_VALIDATION": EXIT_VALIDATION,
    "E_NOT_PRODUCTION_READY": EXIT_VALIDATION,
    "E_UNAUTHORIZED": EXIT_PERMISSION,
    "E_BAD_SIGNATURE": EXIT_PERMISSION,
    "E_TIMEOUT": EXIT_GENERIC,
    "E_RUNTIME": EXIT_GENERIC,
}


class RyaError(Exception):
    """An error with a stable code, a human message, and a suggested next action.

    ``hint`` is intentionally first-class: the spec's DevEx requirement is that
    errors tell an agent *what to do next*.
    """

    def __init__(
        self,
        code: str,
        message: str,
        hint: Optional[str] = None,
        *,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code if exit_code is not None else _CODE_EXIT.get(code, EXIT_GENERIC)

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "exit_code": self.exit_code,
            },
        }

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = f"[{self.code}] {self.message}"
        return f"{base} | next: {self.hint}" if self.hint else base
