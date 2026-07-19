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


def create_turn(engine, type: str, payload: dict, *, max_attempts: int = 3) -> dict:
    """Enqueue a durable chat turn. Returns ``{turnId, runId: None}`` - the turn
    runs on a worker (inline or reclaimed) and its frames land in the stream
    buffer keyed by ``turnId``."""
    job = q.enqueue(engine.store, "chat-turn", {"type": type, "payload": payload},
                    max_attempts=max_attempts)
    return {"turnId": job["id"], "status": job["status"]}


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

    try:
        run = engine.run_event(ev.get("type", "message.received"), ev.get("payload", {}),
                               source="turn", on_trace=on_trace, on_token=on_token)
        # Chat agents: surface assistant session replies before the terminal frame.
        p = ev.get("payload") or {}
        if hasattr(store, "find_session") and p.get("channel") and p.get("externalId"):
            sess = store.find_session(engine.manifest.name, p["channel"], p["externalId"])
            if sess:
                for m in store.list_messages(sess["id"]):
                    if m.get("runId") == run["id"] and m.get("role") in ("assistant", "agent"):
                        store.stream_append(turn_id, [{"kind": "message", "data": m}])
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
                      lease_seconds=lease_seconds)
    for job in claimed:
        _run_turn(engine, job, worker_id)
    return [j["id"] for j in claimed]


def read_stream(engine, turn_id: str, after_seq: int = -1) -> List[dict]:
    return engine.store.stream_read(turn_id, after_seq)


def is_terminal(frames: List[dict]) -> Optional[dict]:
    """The terminal frame (run|error) in a batch, if present."""
    for f in frames:
        if f.get("kind") in ("run", "error"):
            return f
    return None
