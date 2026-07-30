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

from typing import List, Optional

from . import queue as q
from .errors import RyaError


def _summary(run: dict) -> dict:
    from .observability.usage import run_usage
    u = run_usage(run)
    return {"id": run["id"], "status": run["status"], "trigger": run.get("trigger"),
            "pendingApproval": run.get("pendingApproval"), "error": run.get("error"),
            "traceLength": len(run.get("trace", [])),
            "tokens": u["inputTokens"] + u["outputTokens"], "costUsd": u.get("costUsd")}


def create_turn(engine, type: str, payload: dict, *, identity=None,
                max_attempts: int = 3) -> dict:
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
    version = getattr(engine, "version", None) or {}
    metadata = {"versionId": version["id"]} if version.get("id") else None
    job = q.enqueue(engine.store, "chat-turn",
                    {"type": type, "payload": payload, "identity": claims},
                    max_attempts=max_attempts, metadata=metadata,
                    concurrency_key=version.get("id"))
    return {"turnId": job["id"], "status": job["status"]}


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
                               on_trace=on_trace, on_token=on_token, on_ui=on_ui)
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
    ``rya`` worker loop (the durability backstop). Returns the turn ids run."""
    claimed = q.claim(engine.store, worker_id, types=["chat-turn"], limit=limit,
                      lease_seconds=lease_seconds,
                      version_id=(getattr(engine, "version", None) or {}).get("id"))
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
