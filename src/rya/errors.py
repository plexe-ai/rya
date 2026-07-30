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
    # Both were already being RETURNED by the api and the CLI without being
    # declared here, so they fell through to EXIT_GENERIC — `rya connections
    # revoke <unknown-id>` exited 1 instead of 4, which is exactly the
    # branch-on-the-code contract this table exists to keep.
    "E_NOT_FOUND": EXIT_NOT_FOUND,
    "E_SESSION_NOT_FOUND": EXIT_NOT_FOUND,
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
    "E_NO_CONNECTION": EXIT_PERMISSION,
    "E_SCOPE_DENIED": EXIT_PERMISSION,
    "E_NO_IDENTITY": EXIT_PERMISSION,
    "E_CONNECTION_EXPIRED": EXIT_PERMISSION,
    "E_TIMEOUT": EXIT_GENERIC,
    "E_TOOL_UPSTREAM": EXIT_GENERIC,
    "E_TOOL_RECOVERABLE": EXIT_GENERIC,
    "E_RUNTIME": EXIT_GENERIC,
    # ---- platform: deployments, versions, workers (PLATFORM_DESIGN D9/D12, §6) --
    # D9: replay is only sound against the code that wrote the journal, so a step
    # whose content key does not match the recorded one fails CLOSED rather than
    # returning another step's memoized result.
    "E_JOURNAL_DRIFT": EXIT_STATE,
    # D12: a run pinned to a version that no longer exists cannot be replayed.
    "E_VERSION_NOT_FOUND": EXIT_NOT_FOUND,
    "E_VERSION_RETIRED": EXIT_STATE,
    # §6: retiring a version with live pinned runs, or starting a worker whose
    # bundle does not match the version it claims to serve.
    "E_VERSION_IN_USE": EXIT_STATE,
    "E_BUNDLE_MISMATCH": EXIT_STATE,
    "E_BUNDLE_NOT_FOUND": EXIT_NOT_FOUND,
    "E_HANDLER_SET_INCOMPLETE": EXIT_VALIDATION,
    "E_ENVIRONMENT_NOT_FOUND": EXIT_NOT_FOUND,
    # D7/§11.2: policy is privileged platform state; a bundle must not write it.
    "E_POLICY_READONLY": EXIT_PERMISSION,
    # §9: the readiness gate and eval gate are server-side ADMISSION checks, not
    # client-side courtesies — promotion into a gated environment is refused.
    "E_PROMOTION_BLOCKED": EXIT_VALIDATION,
    # §6/D13: a workspace's quota (concurrent runs, runs, tokens or cost per
    # window) is exhausted. Fairness and quotas are how one tenant is stopped
    # from starving another; this is the code that says so.
    "E_QUOTA_EXCEEDED": EXIT_STATE,
    # The object store backing bundle archives is unreachable or misconfigured.
    # Distinct from E_BUNDLE_NOT_FOUND: the artifact may well exist, we cannot
    # reach the place it lives, and the operator fix is different.
    "E_BUNDLE_STORE": EXIT_GENERIC,
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
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code if exit_code is not None else _CODE_EXIT.get(code, EXIT_GENERIC)
        # Upstream HTTP status (set by HTTP tools) so the retry primitive can
        # classify a failure as a transient 5xx without scraping the message.
        self.http_status = http_status

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


class RyaRecoverableToolError(RyaError):
    """A tool failure the agent can *self-heal* rather than surface.

    A handler raises this when the upstream rejected the input for a reason a
    registered ``@agent.repair("<tool>")`` callback can fix and retry — e.g. an
    unrecognised destination country the repair can snap to the closest valid one,
    or a misspelled home state. It carries a machine-readable ``reason`` (the
    repair callback switches on it) plus the upstream ``detail`` for observability.

    The runtime invokes the repair callback ONCE with ``(input, error)``; the
    callback returns a patched input and the tool is retried. If no repair is
    registered, or the repair re-raises, the error surfaces like any other.
    """

    def __init__(self, reason: str, message: Optional[str] = None,
                 detail: Optional[object] = None, hint: Optional[str] = None) -> None:
        super().__init__("E_TOOL_RECOVERABLE", message or reason, hint=hint)
        self.reason = reason
        self.detail = detail
