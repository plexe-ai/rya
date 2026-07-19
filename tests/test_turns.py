"""Durable chat turns: a turn is a leased, reclaimable job whose frames land in
a durable, resumable stream buffer. The point is that an interrupted turn is
retried (not dropped) and a dropped stream resumes from its last seq.

Store-level tests run on the file store always, Postgres when
RYA_TEST_DATABASE_URL is set."""

import os

import pytest
import yaml
from fastapi.testclient import TestClient

from rya import queue as q
from rya import turns
from rya.api.app import build_app
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import FileStore

PG = os.environ.get("RYA_TEST_DATABASE_URL")


def _engine(tmp_path, store):
    scaffold.write_project(tmp_path, "turn-agent")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    return Engine(manifest, agent, store, tmp_path)


@pytest.fixture(params=["file", "postgres"])
def engine(request, tmp_path):
    if request.param == "file":
        s = FileStore(tmp_path)
        s.ensure()
        return _engine(tmp_path, s)
    if not PG:
        pytest.skip("set RYA_TEST_DATABASE_URL to run Postgres turn tests")
    from rya.store_postgres import PostgresStore

    s = PostgresStore(PG)
    s.ensure()
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE rya_stream, rya_queue, rya_runs")
    return _engine(tmp_path, s)


def _kinds(frames):
    return [f["kind"] for f in frames]


# ---- happy path -------------------------------------------------------------

def test_turn_executes_and_streams_to_terminal(engine):
    turn = turns.create_turn(engine, "message.received", {"email": "ada@x.com"})
    tid = turn["turnId"]
    assert turns.read_stream(engine, tid) == []  # nothing until a worker runs it

    ran = turns.execute_pending(engine)
    assert ran == [tid]

    frames = turns.read_stream(engine, tid)
    kinds = _kinds(frames)
    assert "token" in kinds and "trace" in kinds
    assert kinds[-1] == "run", f"terminal frame must be run, got {kinds}"
    assert frames[-1]["data"]["status"] in ("completed", "waiting_approval")
    # seqs are monotonic from 0
    assert [f["seq"] for f in frames] == list(range(len(frames)))


# ---- durability: crash mid-turn is reclaimed, not dropped -------------------

def test_crashed_turn_is_reclaimed_and_completes(engine):
    turn = turns.create_turn(engine, "message.received", {"email": "ada@x.com"})
    tid = turn["turnId"]

    # Simulate a worker that claimed the turn and then died: it holds the job
    # with an already-expired lease and wrote no frames.
    claimed = q.claim(engine.store, "dead-worker", types=["chat-turn"], lease_seconds=0)
    assert claimed and claimed[0]["id"] == tid
    assert turns.read_stream(engine, tid) == []  # nothing streamed - executor died

    # A reclaimer reaps the expired lease and runs the turn to completion.
    ran = turns.execute_pending(engine, worker_id="reclaimer")
    assert tid in ran
    frames = turns.read_stream(engine, tid)
    assert _kinds(frames)[-1] == "run"
    assert frames[-1]["data"]["status"] in ("completed", "waiting_approval")

    job = engine.store.queue_get(tid)
    assert job["status"] == "completed"


# ---- resumability: reconnect from a mid-stream offset -----------------------

def test_stream_resumes_from_last_seq(engine):
    turn = turns.create_turn(engine, "message.received", {"email": "ada@x.com"})
    tid = turn["turnId"]
    turns.execute_pending(engine)

    full = turns.read_stream(engine, tid)
    assert len(full) > 2
    # a client that already consumed up to seq 1 reconnects and sees only the rest
    resumed = turns.read_stream(engine, tid, after_seq=1)
    assert [f["seq"] for f in resumed] == [f["seq"] for f in full if f["seq"] > 1]
    assert _kinds(resumed)[-1] == "run"  # still ends at the authoritative terminal


def test_reexecution_appends_restart_marker(engine):
    turn = turns.create_turn(engine, "message.received", {"email": "ada@x.com"})
    tid = turn["turnId"]

    # Simulate a crash MID-stream: a worker claimed the turn (expired lease) and
    # got a couple of frames out before dying, leaving a partial buffer.
    q.claim(engine.store, "dead-worker", types=["chat-turn"], lease_seconds=0)
    engine.store.stream_append(tid, [{"kind": "token", "data": {"text": "partial"}}])
    partial_len = len(turns.read_stream(engine, tid))
    assert partial_len == 1 and _kinds(turns.read_stream(engine, tid))[-1] != "run"

    # Reclaim re-runs it; because the buffer is non-empty it marks the restart.
    turns.execute_pending(engine, worker_id="reclaimer")
    frames = turns.read_stream(engine, tid)
    assert len(frames) > partial_len
    restart = next(f for f in frames if f["kind"] == "restart")
    assert restart["seq"] == partial_len  # monotonic: appended after the partial frame
    assert _kinds(frames)[-1] == "run"    # terminal still last


# ---- HTTP surface -----------------------------------------------------------

def test_turn_http_roundtrip(tmp_path):
    scaffold.write_project(tmp_path, "turn-http-agent")
    client = TestClient(build_app(tmp_path))

    # POST kicks inline execution via BackgroundTasks (runs after the response
    # in TestClient), so by the time we tail, frames exist in the durable buffer.
    r = client.post("/agents/_/turns", json={"type": "message.received",
                                             "payload": {"email": "ada@x.com"}})
    assert r.status_code == 200
    tid = r.json()["turnId"]

    with client.stream("GET", f"/agents/_/turns/{tid}/stream") as s:
        assert s.status_code == 200
        body = "".join(chunk for chunk in s.iter_text())

    events, ids = [], []
    for line in body.splitlines():
        if line.startswith("event:"):
            events.append(line.split(":", 1)[1].strip())
        elif line.startswith("id:"):
            ids.append(int(line.split(":", 1)[1].strip()))
    assert "token" in events
    assert events[-1] == "run"
    assert ids == sorted(ids)  # monotonic id: fields for Last-Event-ID resume


def test_reclaim_endpoint(tmp_path):
    scaffold.write_project(tmp_path, "turn-reclaim-agent")
    app = build_app(tmp_path)
    client = TestClient(app)

    # Enqueue a turn directly (no inline exec), simulating a crash before run.
    from rya.store import open_store
    from rya.manifest import load_manifest as _lm
    from rya.runtime import Engine as _E, load_agent as _la

    m = _lm(tmp_path / "rya.agent.yaml")
    eng = _E(m, _la(m, tmp_path), open_store(tmp_path), tmp_path)
    turn = turns.create_turn(eng, "message.received", {"email": "ada@x.com"})
    tid = turn["turnId"]

    r = client.post("/agents/_/turns/reclaim")
    assert r.status_code == 200
    assert tid in r.json()["reclaimed"]

    with client.stream("GET", f"/agents/_/turns/{tid}/stream") as s:
        body = "".join(chunk for chunk in s.iter_text())
    assert "event: run" in body
