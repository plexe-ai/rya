r"""Durable job queue for EXTERNAL workers - the "bring your own worker" primitive.

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
    # §11.12: the queue-depth quota is checked AFTER the idempotency short-circuit,
    # so a retried enqueue of an existing job never trips it — a client retrying
    # through a timeout must not be told it is over quota for work already
    # accepted. D14 keeps this surface SDK-free, and a foreign caller gets the
    # same E_QUOTA_EXCEEDED as everyone else.
    from .quotas import require_admission
    require_admission(store, kind="job")
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
    (monotonic ``seq``).

    Quota-wise a batch is admitted or refused **as a unit**: the depth check runs
    once, against the depth the batch would produce, rather than per item. Two
    reasons — a per-item check would issue one count query per job, and a batch
    accepted halfway is worse than one refused outright, since the caller has no
    way to know where the fan-out stopped.
    """
    from .quotas import _usage_for, require_admission
    usage = _usage_for(store, {"queueDepth", "tokensToday", "costUsdToday"})
    if "queueDepth" in usage:
        # Charge the batch up front, so 1 free slot does not admit 500 jobs.
        usage = {**usage, "queueDepth": usage["queueDepth"] + max(0, len(items) - 1)}
    require_admission(store, kind="job", usage=usage)

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


def version_of(job: dict) -> Optional[str]:
    """The version a job is pinned to, if any (PLATFORM_DESIGN D12).

    Carried in ``metadata`` rather than as a column so the SDK-free ``/queue/*``
    surface (D14) stays unchanged for foreign workers: a TypeScript worker that
    knows nothing about versions enqueues without one and claims without one.
    """
    return (job.get("metadata") or {}).get("versionId")


def agent_of(job: dict) -> Optional[str]:
    """The agent a job belongs to, if any (MULTITENANT_DESIGN D22).

    Same carrier and the same reason as :func:`version_of` — metadata, not a
    column, so D14's SDK-free surface is unchanged for foreign consumers.
    """
    return (job.get("metadata") or {}).get("agent")


def claim(store, worker_id: str, *, types: Optional[List[str]] = None, limit: int = 1,
          lease_seconds: float = DEFAULT_LEASE_SECONDS,
          version_id: Optional[str] = None,
          agent: Optional[str] = None) -> List[dict]:
    """Claim up to ``limit`` due jobs for ``worker_id``. Reaps expired leases
    first, then claims one at a time so per-key concurrency caps hold even
    within a single call.

    ``version_id`` is version-pinned claiming (D12): a worker serving version A
    must not execute a run pinned to version B, because replay is only sound
    against the code that wrote the journal. A pinned worker claims jobs pinned
    to its version plus unpinned ones; an UNpinned worker (the `rya dev` /
    single-tenant case, and any foreign `/queue/*` consumer) claims anything, so
    D14's SDK-free surface is untouched.

    ``agent`` is D22, and it is **not** the same shape as the version filter even
    though it looks like it. Version pinning is opt-in on both sides, which left a
    hole: a worker that is merely *unpinned* — the common case, since pinning
    needs a published version — claimed anything, including a ``chat-turn``
    enqueued for a different agent, and then executed it against its own handler.
    Under D17 that is a cross-tenant execution path, not a mix-up.

    So the agent filter is applied independently of the version filter: a claimer
    that names its agent takes only that agent's jobs plus untagged ones, whatever
    its pinning state. A claimer that names no agent still takes anything, which
    is what keeps D14's foreign consumers working.

    Filtering happens after the claim rather than in SQL: releasing a wrongly
    matched job is cheap and correct on both backends, whereas pushing a JSONB
    predicate into ``queue_claim_one`` would fork the two store implementations
    for a case that only arises while several versions are live at once.

    **That "only arises" stopped being true at the wide claimer scope**, where several
    agents' turns are interleaved in one queue by design and every dispatch filters
    against one of them. Correctness is restored below by retrying past a release
    instead of counting it as an item; the *cost* is not — a fork can now walk up to
    ``MAX_CLAIM_LIMIT`` sibling jobs to reach its own. That is bounded and it is not
    free, and if it shows up in practice the answer is the SQL predicate this
    paragraph declined, not a bigger constant. Recorded as a re-plan trigger rather
    than pre-optimised, because the shape of the fix depends on whether the pressure
    is depth (one agent with a huge backlog) or breadth (many agents, shallow each).

    **D18: a mediated store claims through the broker instead.** Everything above
    describes filtering that happens *here*, in the caller's process — which is
    correct while the caller is platform code and wrong once D27 puts this loop
    inside a fork with the tenant's bundle imported into it. A hostile handler would
    simply not release the sibling job it was handed. So when the store is a
    ``BrokerStore``, the whole operation is performed on the platform's side of the
    boundary with the identity arguments taken from the capability, and the four
    arguments this function was given are *ignored on purpose* — a fork does not get
    to name its own agent, its own version, its own worker id or its own lease.
    """
    mediated = getattr(store, "broker_claim", None)
    if mediated is not None:
        return mediated(types=types, lease_seconds=lease_seconds)
    if not worker_id:
        raise RyaError("E_VALIDATION", "workerId is required to claim jobs.")
    now = now_iso()
    store.queue_reap(now)
    lease = _iso_plus(max(0, lease_seconds))
    claimed: List[dict] = []
    released: List[dict] = []
    wanted = max(1, min(int(limit), MAX_CLAIM_LIMIT))
    # ATTEMPTS, not items. A job this caller may not run is released rather than
    # executed, and counting that release against ``limit`` was a starvation bug the
    # wide claimer scope (#19-8b) made systematic: with several agents' turns
    # interleaved in one queue, a fork asking for one item claimed whichever job was
    # oldest, released it because it belonged to a sibling, and then reported "nothing
    # to do" — so with N agents active a dispatch had roughly a 1-in-N chance of
    # finding its own work, and the deepest backlog decided whose.
    #
    # Safe to retry precisely because released jobs are held until the loop ends: they
    # stay `running` under this worker id for the duration, so ``queue_claim_one``
    # cannot hand back the same row twice and the loop cannot spin on it. The bound is
    # ``MAX_CLAIM_LIMIT`` attempts, which is also what stops a queue full of one
    # agent's work from making another agent's fork walk it end to end.
    for _ in range(MAX_CLAIM_LIMIT):
        if len(claimed) >= wanted:
            break
        job = store.queue_claim_one(worker_id, now, lease, types)
        if job is None:
            break
        pinned = version_of(job)
        if version_id is not None and pinned is not None and pinned != version_id:
            released.append(job)
            continue
        owner = agent_of(job)
        if agent is not None and owner is not None and owner != agent:
            released.append(job)
            continue
        claimed.append(job)
    for job in released:
        # Hand it back unchanged so the worker on ITS version — or, under D22, its
        # agent — can take it. The attempt that queue_claim_one incremented is
        # rolled back: refusing a job you are not allowed to run must not consume
        # its retry budget.
        job["status"] = "pending"
        job["workerId"] = None
        job["leaseExpiresAt"] = None
        job["attempts"] = max(0, int(job.get("attempts") or 1) - 1)
        store.queue_save(job)
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
    heartbeat well inside their lease and abort when cancelRequested is true.

    D18: mediated stores extend server-side, where the extension is also clamped —
    see the note on :func:`claim`. ``_check_holder`` is caller-side code, so a fork
    checking whether it holds its own lease is not a check.
    """
    mediated = getattr(store, "broker_heartbeat", None)
    if mediated is not None:
        return mediated(job_id, extend_seconds)
    job = _get_or_raise(store, job_id)
    _check_holder(job, worker_id)
    job["leaseExpiresAt"] = _iso_plus(max(0, extend_seconds))
    store.queue_save(job)
    return {"ok": True, "leaseExpiresAt": job["leaseExpiresAt"],
            "cancelRequested": bool(job.get("cancelRequested"))}


def complete(store, job_id: str, worker_id: str, output: Any = None) -> dict:
    mediated = getattr(store, "broker_complete", None)
    if mediated is not None:
        return mediated(job_id, output)
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
    mediated = getattr(store, "broker_fail", None)
    if mediated is not None:
        return mediated(job_id, error)
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
