"""Durable job queue for EXTERNAL workers - the "bring your own worker" primitive.

The existing ``jobs`` primitive is handler-bound: ``rya worker`` claims a due job
and executes its Python handler in-process. This module is the complement for
polyglot backends (a Node/TS app, another service): the caller enqueues jobs over
HTTP, its own workers claim them with a lease, heartbeat while running, and report
complete/fail. Rya owns durability, retries with backoff, dead-lettering,
idempotent enqueue, per-key concurrency caps, and cancellation signalling.

Designed against a real consumer: Sim's ``JobQueueBackend`` interface
(enqueue / batchEnqueue / getJob / startJob / completeJob / markJobFailed /
cancelJob), so a Sim ``backends/rya.ts`` adapter maps 1:1 onto this API.

Lifecycle::

    pending --claim--> running --complete--> completed
                          |   \--fail (attempts < maxAttempts)--> pending (backoff)
                          |   \--fail (exhausted)---------------> failed (deadLetter)
                          \--lease expiry--> pending | failed     (queue_reap)
    pending --cancel--> cancelled
    running --cancel--> cancelRequested=true (worker sees it on heartbeat and stops;
                        its final complete/fail then lands as cancelled), or
                        force=true to cancel immediately.

All state transitions verify the reporting worker still holds the job, so a
worker whose lease expired (and whose job was reclaimed) gets a conflict instead
of silently clobbering another worker's run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from .errors import RyaError
from .store import now_iso, _new_id

# Retry backoff: base * 2^(attempt-1), capped. Overridable per job.
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 300
DEFAULT_LEASE_SECONDS = 60
MAX_CLAIM_LIMIT = 50


def _iso_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _seq() -> int:
    import time
    return time.time_ns()


def enqueue(store, type: str, payload: Any, *, job_id: Optional[str] = None,
            max_attempts: int = 1, delay_seconds: float = 0, priority: int = 0,
            tags: Optional[List[str]] = None, metadata: Optional[dict] = None,
            concurrency_key: Optional[str] = None, concurrency_limit: Optional[int] = None,
            retry_delay_seconds: Optional[float] = None) -> dict:
    """Enqueue one job. ``job_id`` doubles as an idempotency key: re-enqueueing an
    existing id returns the existing job untouched."""
    if not type:
        raise RyaError("E_VALIDATION", "Queue job 'type' is required.",
                       hint="POST /queue/jobs with {\"type\": ..., \"payload\": ...}.")
    if job_id:
        existing = store.queue_get(job_id)
        if existing is not None:
            return existing
    job = {
        "id": job_id or _new_id("qj"),
        "type": type,
        "payload": payload,
        "status": "pending",
        "attempts": 0,
        "maxAttempts": max(1, int(max_attempts)),
        "priority": int(priority),
        "runAt": _iso_plus(delay_seconds) if delay_seconds else now_iso(),
        "seq": _seq(),
        "tags": list(tags or []),
        "metadata": metadata or {},
        "concurrencyKey": concurrency_key,
        "concurrencyLimit": int(concurrency_limit) if concurrency_limit else None,
        "retryDelaySeconds": retry_delay_seconds,
        "workerId": None,
        "leaseExpiresAt": None,
        "cancelRequested": False,
        "deadLetter": False,
        "output": None,
        "error": None,
        "lastError": None,
        "createdAt": now_iso(),
        "startedAt": None,
        "completedAt": None,
    }
    store.queue_save(job)
    return job


def enqueue_batch(store, type: str, items: List[dict]) -> List[dict]:
    """Enqueue many jobs; dispatch order within the batch follows input order
    (monotonic ``seq``)."""
    return [
        enqueue(
            store, type, item.get("payload"),
            job_id=item.get("jobId"),
            max_attempts=item.get("maxAttempts", 1),
            delay_seconds=item.get("delaySeconds", 0),
            priority=item.get("priority", 0),
            tags=item.get("tags"),
            metadata=item.get("metadata"),
            concurrency_key=item.get("concurrencyKey"),
            concurrency_limit=item.get("concurrencyLimit"),
            retry_delay_seconds=item.get("retryDelaySeconds"),
        )
        for item in items
    ]


def claim(store, worker_id: str, *, types: Optional[List[str]] = None, limit: int = 1,
          lease_seconds: float = DEFAULT_LEASE_SECONDS) -> List[dict]:
    """Claim up to ``limit`` due jobs for ``worker_id``. Reaps expired leases
    first, then claims one at a time so per-key concurrency caps hold even
    within a single call."""
    if not worker_id:
        raise RyaError("E_VALIDATION", "workerId is required to claim jobs.")
    now = now_iso()
    store.queue_reap(now)
    lease = _iso_plus(max(0, lease_seconds))
    claimed = []
    for _ in range(max(1, min(int(limit), MAX_CLAIM_LIMIT))):
        job = store.queue_claim_one(worker_id, now, lease, types)
        if job is None:
            break
        claimed.append(job)
    return claimed


def _get_or_raise(store, job_id: str) -> dict:
    job = store.queue_get(job_id)
    if job is None:
        raise RyaError("E_JOB_NOT_FOUND", f"Queue job '{job_id}' not found.")
    return job


def _check_holder(job: dict, worker_id: str) -> None:
    if job.get("status") != "running" or job.get("workerId") != worker_id:
        raise RyaError(
            "E_QUEUE_CONFLICT",
            f"Job '{job['id']}' is {job.get('status')} and held by "
            f"{job.get('workerId') or 'nobody'}, not '{worker_id}'.",
            hint="The lease may have expired and been reclaimed. Stop working on this job.")


def heartbeat(store, job_id: str, worker_id: str,
              extend_seconds: float = DEFAULT_LEASE_SECONDS) -> dict:
    """Extend the lease and report back the cancellation flag. Workers should
    heartbeat well inside their lease and abort when cancelRequested is true."""
    job = _get_or_raise(store, job_id)
    _check_holder(job, worker_id)
    job["leaseExpiresAt"] = _iso_plus(max(0, extend_seconds))
    store.queue_save(job)
    return {"ok": True, "leaseExpiresAt": job["leaseExpiresAt"],
            "cancelRequested": bool(job.get("cancelRequested"))}


def complete(store, job_id: str, worker_id: str, output: Any = None) -> dict:
    job = _get_or_raise(store, job_id)
    if job.get("status") == "cancelled":
        return job  # cancel won; the late result is dropped
    _check_holder(job, worker_id)
    job["status"] = "cancelled" if job.get("cancelRequested") else "completed"
    job["output"] = output
    job["completedAt"] = now_iso()
    job["leaseExpiresAt"] = None
    store.queue_save(job)
    return job


def fail(store, job_id: str, worker_id: str, error: str) -> dict:
    """Failed attempt: retry with exponential backoff until maxAttempts, then
    dead-letter. A cancel-requested job goes straight to cancelled."""
    job = _get_or_raise(store, job_id)
    if job.get("status") == "cancelled":
        return job
    _check_holder(job, worker_id)
    job["lastError"] = str(error)[:2000]
    job["workerId"] = None
    job["leaseExpiresAt"] = None
    if job.get("cancelRequested"):
        job["status"] = "cancelled"
        job["completedAt"] = now_iso()
    elif int(job.get("attempts") or 0) >= int(job.get("maxAttempts") or 1):
        job["status"] = "failed"
        job["deadLetter"] = True
        job["error"] = job["lastError"]
        job["completedAt"] = now_iso()
    else:
        rds = job.get("retryDelaySeconds")
        delay = float(rds) if rds is not None else min(
            BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (int(job["attempts"]) - 1))
        job["status"] = "pending"
        job["runAt"] = _iso_plus(delay) if delay else now_iso()
    store.queue_save(job)
    return job


def cancel(store, job_id: str, force: bool = False) -> dict:
    """Cancel a job. Pending jobs cancel immediately; running jobs get
    cancelRequested (graceful, observed via heartbeat) unless ``force``.
    Unknown ids resolve quietly so callers can cancel from stale state."""
    job = store.queue_get(job_id)
    if job is None:
        return {"ok": True, "found": False, "jobId": job_id}
    if job["status"] == "pending":
        job["status"] = "cancelled"
        job["cancelRequested"] = True
        job["completedAt"] = now_iso()
    elif job["status"] == "running":
        job["cancelRequested"] = True
        if force:
            job["status"] = "cancelled"
            job["workerId"] = None
            job["leaseExpiresAt"] = None
            job["completedAt"] = now_iso()
    # terminal states are left as-is
    store.queue_save(job)
    return {"ok": True, "found": True, "jobId": job_id, "status": job["status"],
            "cancelRequested": bool(job.get("cancelRequested"))}


def retry(store, job_id: str) -> dict:
    """Requeue a dead-lettered / failed / cancelled job with a fresh attempt budget."""
    job = _get_or_raise(store, job_id)
    if job["status"] not in ("failed", "cancelled"):
        raise RyaError("E_QUEUE_CONFLICT",
                       f"Job '{job_id}' is {job['status']}; only failed or cancelled "
                       "jobs can be retried.")
    job.update({"status": "pending", "attempts": 0, "runAt": now_iso(),
                "workerId": None, "leaseExpiresAt": None, "cancelRequested": False,
                "deadLetter": False, "error": None, "output": None,
                "completedAt": None, "seq": _seq()})
    store.queue_save(job)
    return job


def stats(store) -> dict:
    return {"counts": store.queue_counts()}
