"""Durable chat turns.

A chat turn is normally a synchronous request: the run executes while the SSE /
WebSocket connection is open, so a mid-turn server crash strands the run (nothing
re-drives it) and a dropped connection loses the live stream. This module makes a
turn as durable as a background job by inverting that:

1. A turn is enqueued as a ``chat-turn`` job on the durable queue - so it has a
   lease and is RECLAIMED if its executor dies (exactly like any queue job).
2. The executing worker relays every frame (token / trace / message / run) into a
   durable, monotonically-sequenced **stream buffer** on the store.
3. The streaming endpoint TAILS that buffer by seq - it never executes anything.
   A dropped client reconnects with its last seq (Last-Event-ID) and resumes; a
   reclaimed re-run just appends a ``restart`` marker and more frames.

So: turn STATE and the STREAM are both durable, and an interrupted turn is
retried instead of dropped. Crash-retry re-runs the handler fresh (same
idempotency contract as background jobs); an approval PAUSE inside a turn is
durable via the engine's journal replay, as always.
"""

from __future__ import annotations

import uuid
from typing import Any, List, NamedTuple, Optional

from . import queue as q
from .errors import RyaError

# The queue type carrying "a human approved this; someone go finish the run".
# Separate from ``chat-turn`` because the two are claimed by the same worker but
# mean different things: a turn STARTS a run, a resume CONTINUES a paused one and
# must land on a worker holding the version that paused it.
RESUME_JOB = "approval-resume"

# The state an approval sits in between the control plane recording the decision
# and the execution plane carrying it out. Not a cosmetic intermediate: it is what
# makes a second approve a refusal instead of a second resume.
APPROVING = "approving"


class TurnSource(NamedTuple):
    """The three things enqueuing a turn actually needs.

    ``Engine`` satisfies this shape structurally, which is why the functions below
    take either. Under D21 the api has no ``Engine`` — it deliberately imports no
    handler — but it does have exactly these three facts, so it constructs one of
    these instead of an engine it has no business building.
    """

    store: Any
    manifest: Any                    # anything with a `.name`
    version: Optional[dict] = None


def _summary(run: dict) -> dict:
    from .observability.usage import run_usage
    u = run_usage(run)
    return {"id": run["id"], "status": run["status"], "trigger": run.get("trigger"),
            "pendingApproval": run.get("pendingApproval"), "error": run.get("error"),
            "traceLength": len(run.get("trace", [])),
            "tokens": u["inputTokens"] + u["outputTokens"], "costUsd": u.get("costUsd")}


def create_turn(engine, type: str, payload: dict, *, identity=None,
                max_attempts: int = 3, run_id: Optional[str] = None) -> dict:
    """Enqueue a durable chat turn. Returns ``{turnId, runId: None}`` - the turn
    runs on a worker (inline or reclaimed) and its frames land in the stream
    buffer keyed by ``turnId``.

    ``identity`` is the VERIFIED caller, carried on the job so the executing
    worker runs under it. D6 routes every stream through this buffer, which means
    the process that authenticated the request is no longer the process that
    executes the handler — without this the run would silently lose per-user
    scoping (and fall through to workspace-shared credentials).
    """
    claims = None
    if identity is not None:
        claims = identity.to_dict() if hasattr(identity, "to_dict") else identity
    # D12: pin the turn to the version that enqueued it, so a worker on a
    # different version will not claim it (queue.claim's version filter). An
    # unpinned engine (`rya dev`, single-tenant) enqueues without one, and any
    # worker takes it.
    #
    # D22: tag the owning agent too. Version pinning alone did not protect this
    # job — an unpinned worker for another agent would claim it and run it against
    # the wrong handler — and an unpinned engine is the common case, so the agent
    # tag is written whether or not there is a version to pin to.
    version = getattr(engine, "version", None) or {}
    metadata = {}
    if version.get("id"):
        metadata["versionId"] = version["id"]
    agent_name = getattr(getattr(engine, "manifest", None), "name", None)
    if agent_name:
        metadata["agent"] = agent_name
    # `runId` is present when the CONTROL PLANE already created the run record
    # (enqueue_event). The executing worker adopts that id instead of minting a
    # second one, so the id the caller was handed is the id the run ends up with.
    job = q.enqueue(engine.store, "chat-turn",
                    {"type": type, "payload": payload, "identity": claims,
                     **({"runId": run_id} if run_id else {})},
                    max_attempts=max_attempts, metadata=metadata or None,
                    concurrency_key=version.get("id"))
    return {"turnId": job["id"], "status": job["status"]}


def enqueue_event(source, type: str, payload: dict, *, trigger_source: str = "api",
                  identity=None, environment: Optional[str] = None,
                  max_attempts: int = 3) -> dict:
    """Create a **queued run** and hand it to a worker. Returns
    ``{runId, turnId, status, pendingApproval}``.

    What `POST /agents/{id}/events` does now that the api does not execute (D21).
    The run row is written *before* the job exists, and all three consequences are
    the point rather than incidental:

    * **The caller gets a run id synchronously.** `GET /runs/{id}` answers while
      the run is still queued, so a client polls one handle instead of correlating
      a turn id back to a run later.
    * **The pin is decided by the control plane.** The version comes from the
      environment pointer, which only the control plane can read authoritatively.
      Before this the api enqueued unpinned and whichever worker claimed the turn
      stamped its own version — so "which code ran this" was decided by
      scheduling.
    * **The quota refusal reaches the caller.** Admission is an api-side check
      because 429 is an answer to a request. Deferring it to the worker would turn
      an over-quota call into a 200 followed by a silently failed run.
    """
    from .quotas import require_admission
    from .store import now_iso

    store = source.store
    require_admission(store, kind="run")

    version = getattr(source, "version", None) or {}
    manifest = getattr(source, "manifest", None)
    agent_name = getattr(manifest, "name", None)
    event = {"id": "evt_" + uuid.uuid4().hex[:12], "type": type,
             "source": trigger_source, "agentId": agent_name,
             "payload": payload, "createdAt": now_iso()}
    run = {
        "id": store.new_run_id(),
        "agent": agent_name,
        "agentVersion": version.get("manifestVersion") or getattr(manifest, "version", None),
        "versionId": version.get("id"),
        "bundleHash": version.get("bundleHash"),
        "sdkVersion": version.get("sdkVersion"),
        "environment": environment,
        "trigger": "event",
        # Not "running": nothing is running it yet, and a status that claimed
        # otherwise would make a queue backlog indistinguishable from a stuck run.
        "status": "queued",
        "event": event,
        "job": None, "journal": {}, "trace": [],
        "pendingApproval": None, "error": None,
        "scheduledJobs": [], "parentRunId": None,
        "createdAt": now_iso(),
    }
    if identity is not None:
        run["identity"] = identity.to_dict() if hasattr(identity, "to_dict") else identity
    store.save_run(run)

    started = create_turn(source, type, payload, identity=identity,
                          max_attempts=max_attempts, run_id=run["id"])
    return {"runId": run["id"], "turnId": started["turnId"], "status": "queued",
            "pendingApproval": None}


def _identity_from_claims(claims):
    if not claims:
        return None
    from .auth import Identity
    return Identity(sub=claims["sub"], claims=claims)


def _run_turn(engine, job: dict, worker_id: str) -> None:
    """Execute one claimed chat-turn job, relaying frames to its stream buffer."""
    store = engine.store
    turn_id = job["id"]
    ev = job.get("payload") or {}

    # Re-run (reclaim after a crash) appends to the same buffer; mark the seam so
    # a tailing client can see the stream restarted rather than silently jumping.
    if store.stream_read(turn_id, -1):
        store.stream_append(turn_id, [{"kind": "restart", "data": {"attempt": job.get("attempts")}}])

    def on_trace(evt):
        store.stream_append(turn_id, [{"kind": "trace", "data": evt}])

    def on_token(chunk):
        store.stream_append(turn_id, [{"kind": "token", "data": {"text": chunk}}])

    def on_ui(frame):
        store.stream_append(turn_id, [{"kind": "ui", "data": frame}])

    try:
        run = engine.run_event(ev.get("type", "message.received"), ev.get("payload", {}),
                               source="turn", identity=_identity_from_claims(ev.get("identity")),
                               on_trace=on_trace, on_token=on_token, on_ui=on_ui,
                               run_id=ev.get("runId"))
        if run["status"] == "waiting_approval":
            # Tag the paused run with its turn so the approval resolution can
            # stream the continuation onto this same buffer (resolve_on_stream).
            run["turnId"] = turn_id
            store.save_run(run)
        _append_session_replies(engine, turn_id, ev.get("payload") or {}, run)
        store.stream_append(turn_id, [{"kind": "run", "data": _summary(run)}])
        q.complete(store, turn_id, worker_id, {"runId": run["id"], "status": run["status"]})
    except RyaError as e:
        store.stream_append(turn_id, [{"kind": "error", "data": e.to_dict()["error"]}])
        q.fail(store, turn_id, worker_id, e.message)
    except Exception as e:  # never leave the buffer without a terminal frame
        store.stream_append(turn_id, [{"kind": "error", "data": {"code": "E_RUNTIME", "message": str(e)}}])
        q.fail(store, turn_id, worker_id, str(e))


def execute_pending(engine, worker_id: str = "turn-worker", limit: int = 10,
                    lease_seconds: float = 120) -> List[str]:
    """Claim and run due chat-turns. ``q.claim`` reaps expired leases first, so
    this reclaims turns whose executor died AND runs freshly-enqueued ones.
    Call it inline after enqueue (low latency) and/or from a periodic sweeper /
    ``rya`` worker loop (the durability backstop). Returns the turn ids run.

    D22: the engine's own agent is passed as the claim filter, so this never
    picks up a sibling agent's turn. It is taken from the engine rather than a
    parameter because every caller — inline api, `rya worker`, the reclaimer —
    already has exactly one, and letting a caller pass a different one would
    reintroduce the hole this closes.
    """
    claimed = q.claim(engine.store, worker_id, types=["chat-turn"], limit=limit,
                      lease_seconds=lease_seconds,
                      version_id=(getattr(engine, "version", None) or {}).get("id"),
                      agent=getattr(getattr(engine, "manifest", None), "name", None))
    for job in claimed:
        _run_turn(engine, job, worker_id)
    return [j["id"] for j in claimed]


def _append_session_replies(engine, turn_id: str, payload: dict, run: dict) -> None:
    """Chat agents: surface assistant session replies before the run frame."""
    store = engine.store
    if hasattr(store, "find_session") and payload.get("channel") and payload.get("externalId"):
        sess = store.find_session(engine.manifest.name, payload["channel"], payload["externalId"])
        if sess:
            for m in store.list_messages(sess["id"]):
                if m.get("runId") == run["id"] and m.get("role") in ("assistant", "agent"):
                    store.stream_append(turn_id, [{"kind": "message", "data": m}])


def resolve_on_stream(engine, approval_id: str, approve: bool = True,
                      actor: Optional[dict] = None) -> dict:
    """Approve/reject an approval; if its run belongs to a turn, stream the
    POST-approval continuation onto that turn's buffer, ending with a NEW
    terminal run frame. Memoized pre-approval steps don't re-stream. Falls back
    to a plain approve/reject when the run isn't turn-bound."""
    store = engine.store
    approval = store.get_approval(approval_id)
    run = store.get_run(approval["runId"]) if approval else None
    turn_id = (run or {}).get("turnId")

    if not turn_id:
        return (engine.approve(approval_id, actor=actor) if approve
                else engine.reject(approval_id, actor=actor))

    store.stream_append(turn_id, [{
        "kind": "trace",
        "data": {"kind": "approval.approved" if approve else "approval.rejected",
                 "label": (approval or {}).get("title"),
                 "data": {"approvalId": approval_id, "actor": actor}},
    }])
    if approve:
        resumed = engine.approve(
            approval_id, actor=actor,
            on_trace=lambda ev: store.stream_append(turn_id, [{"kind": "trace", "data": ev}]),
            on_token=lambda ch: store.stream_append(turn_id, [{"kind": "token", "data": {"text": ch}}]),
            on_ui=lambda frame: store.stream_append(turn_id, [{"kind": "ui", "data": frame}]),
        )
        ev = resumed.get("event") or {}
        _append_session_replies(engine, turn_id, ev.get("payload") or {}, resumed)
    else:
        resumed = engine.reject(approval_id, actor=actor)
    store.stream_append(turn_id, [{"kind": "run", "data": _summary(resumed)}])
    return resumed


def read_stream(engine, turn_id: str, after_seq: int = -1) -> List[dict]:
    return engine.store.stream_read(turn_id, after_seq)


# ---- approval resume: the decision and the execution, split -----------------
#
# Approving does two things that need tenant code — it runs the approved action
# and then replays the handler against the resolved journal — so the api process
# must not do it (D13/D21). But the *decision* needs the authenticated human, who
# is only present in the api. Splitting them is what lets both be true.
#
# The split also fixes `E_JOURNAL_DRIFT` on a published run, and that is not a
# side effect. Before this, the api resumed with whatever code it imported at
# boot: its working tree, against a journal written by the promoted bundle. The
# resume job is pinned to `run["versionId"]`, so the process that continues a run
# is on the same content hash as the one that paused it, by construction.
def enqueue_resume(store, approval_id: str, *, actor: Optional[dict] = None,
                   max_attempts: int = 3) -> dict:
    """Record an approval and hand its resume to a worker. Returns the job.

    Every precondition `Engine.approve` checks is checked HERE, before the job
    exists, so an invalid approval is a 409 to the caller rather than a job that
    fails on a worker where nobody is watching. The decision is durable before
    the job is enqueued, which is what makes a concurrent second approve a
    refusal instead of a second resume.
    """
    approval = store.get_approval(approval_id)
    if approval is None:
        raise RyaError("E_APPROVAL_NOT_FOUND", f"Approval '{approval_id}' not found.")
    if approval["status"] != "pending":
        raise RyaError(
            "E_APPROVAL_NOT_PENDING",
            f"Approval '{approval_id}' is '{approval['status']}', not pending.",
            hint="Only pending approvals can be approved."
                 if approval["status"] != APPROVING else
                 "It has already been approved and a worker is resuming the run.")
    run = store.get_run(approval["runId"])
    if run is None:
        raise RyaError("E_RUN_NOT_FOUND", f"Run '{approval['runId']}' not found.")
    if run["status"] != "waiting_approval":
        raise RyaError("E_RUN_NOT_PAUSED",
                       f"Run '{run['id']}' is '{run['status']}', not waiting_approval.")

    approval["status"] = APPROVING
    approval["resolvedBy"] = actor
    store.save_approval(approval)

    # D22 + D12: the resume belongs to this run's agent and this run's version.
    # The version pin is the load-bearing half — an unpinned worker replaying a
    # journal written by a different bundle is precisely `E_JOURNAL_DRIFT`.
    metadata = {}
    if run.get("agent"):
        metadata["agent"] = run["agent"]
    if run.get("versionId"):
        metadata["versionId"] = run["versionId"]
    return q.enqueue(store, RESUME_JOB,
                     {"approvalId": approval_id, "actor": actor},
                     max_attempts=max_attempts, metadata=metadata or None,
                     concurrency_key=run.get("versionId"))


def _run_resume(engine, job: dict, worker_id: str) -> None:
    payload = job.get("payload") or {}
    approval_id = payload.get("approvalId") or ""
    try:
        run = resolve_on_stream(engine, approval_id, approve=True,
                                actor=payload.get("actor"))
        q.complete(engine.store, job["id"], worker_id,
                   {"runId": run["id"], "status": run["status"]})
    except RyaError as e:
        q.fail(engine.store, job["id"], worker_id, e.message)
    except Exception as e:  # a resume must never leave the job leased forever
        q.fail(engine.store, job["id"], worker_id, str(e))


def execute_resumes(engine, worker_id: str = "turn-worker", limit: int = 10,
                    lease_seconds: float = 120) -> List[str]:
    """Claim and run due approval resumes. Same claim discipline as
    ``execute_pending`` — expired leases are reaped first, so a resume whose
    worker died is retried rather than stranding a run in ``waiting_approval``
    with an already-approved approval."""
    claimed = q.claim(engine.store, worker_id, types=[RESUME_JOB], limit=limit,
                      lease_seconds=lease_seconds,
                      version_id=(getattr(engine, "version", None) or {}).get("id"),
                      agent=getattr(getattr(engine, "manifest", None), "name", None))
    for job in claimed:
        _run_resume(engine, job, worker_id)
    return [j["id"] for j in claimed]


def reject_approval(store, approval_id: str, actor: Optional[dict] = None) -> dict:
    """Reject an approval and fail its run — **without** any tenant code.

    Lives here rather than on `Engine` because it never needed an engine: unlike
    approving, rejecting executes no action and replays no handler, it only marks
    two records and appends a trace step. That asymmetry is why `/reject` stays
    synchronous in every mode while `/approve` is handed to a worker.
    `Engine.reject` delegates here so there is one implementation.
    """
    from .store import now_iso

    approval = store.get_approval(approval_id)
    if approval is None:
        raise RyaError("E_APPROVAL_NOT_FOUND", f"Approval '{approval_id}' not found.")
    if approval["status"] not in ("pending", APPROVING):
        raise RyaError("E_APPROVAL_NOT_PENDING",
                       f"Approval '{approval_id}' is '{approval['status']}', not pending.")
    run = store.get_run(approval["runId"])
    if run is None:
        raise RyaError("E_RUN_NOT_FOUND", f"Run '{approval['runId']}' not found.")

    approval["status"] = "rejected"
    approval["resolvedAt"] = now_iso()
    approval["resolvedBy"] = actor
    store.save_approval(approval)

    for entry in run["journal"].values():
        if entry.get("kind") == "approval" and \
                (entry.get("result") or {}).get("approvalId") == approval_id:
            entry["status"] = "rejected"
            break
    run["status"] = "rejected"
    run["error"] = {"code": "E_APPROVAL_REJECTED", "approvalId": approval_id}
    run["pendingApproval"] = None
    run["trace"].append({
        "seq": len(run["trace"]), "ts": now_iso(), "kind": "approval.rejected",
        "label": approval["title"], "data": {"approvalId": approval_id, "actor": actor},
    })
    store.save_run(run)
    return run


def reject_on_stream(store, approval_id: str, actor: Optional[dict] = None) -> dict:
    """`reject_approval`, plus the turn-buffer frames a tailing client needs.

    The reject half of `resolve_on_stream`, without the engine — so the api can
    keep answering rejections itself (see `reject_approval`).
    """
    approval = store.get_approval(approval_id)
    run = store.get_run(approval["runId"]) if approval else None
    turn_id = (run or {}).get("turnId")
    if turn_id:
        store.stream_append(turn_id, [{
            "kind": "trace",
            "data": {"kind": "approval.rejected", "label": (approval or {}).get("title"),
                     "data": {"approvalId": approval_id, "actor": actor}},
        }])
    rejected = reject_approval(store, approval_id, actor=actor)
    if turn_id:
        store.stream_append(turn_id, [{"kind": "run", "data": _summary(rejected)}])
    return rejected


TERMINAL_RUN_STATUSES = ("completed", "failed", "rejected", "needs_reconnect")


def is_terminal(frames: List[dict]) -> Optional[dict]:
    """The FINAL terminal frame in a batch: an error, or a run frame whose
    status is terminal (a run frame with waiting_approval is a pause marker -
    the approval resolution appends the real terminal frame later)."""
    for f in frames:
        if f.get("kind") == "error":
            return f
        if f.get("kind") == "run" and (f.get("data") or {}).get("status") in TERMINAL_RUN_STATUSES:
            return f
    return None
