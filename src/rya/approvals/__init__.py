"""Human approval primitives.

Approval gates are core infrastructure here, not an add-on: a run literally
pauses (the handler coroutine unwinds via ``PausedForApproval``) and resumes
later by replaying the handler against the persisted journal.
"""

from __future__ import annotations

# Lifecycle states from the spec.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELLED = "cancelled"

LIFECYCLE = (PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED)


class PausedForApproval(Exception):
    """Raised inside a run to suspend it until a human resolves an approval."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"run paused for approval {approval_id}")
        self.approval_id = approval_id


class ApprovalRejected(Exception):
    """Raised when a resumed run hits an approval a human rejected."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval {approval_id} was rejected")
        self.approval_id = approval_id


__all__ = [
    "PausedForApproval",
    "ApprovalRejected",
    "LIFECYCLE",
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
]
