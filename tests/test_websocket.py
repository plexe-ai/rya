"""The real-time WebSocket channel: drive a run, stream its trace, replay, auth."""

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold


def _client(tmp_path, monkeypatch, token=None):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    if token:
        monkeypatch.setenv("RYA_TOKEN", token)
    scaffold.write_project(tmp_path, "ws-agent", template="demo")
    return TestClient(build_app(tmp_path))


def test_ws_event_streams_trace_then_run(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "event", "eventType": "message.received",
                      "payload": {"email": "ada@x.com"}})
        traces, run = [], None
        while True:
            m = ws.receive_json()
            if m["type"] == "trace":
                traces.append(m["event"])
            elif m["type"] == "run":
                run = m["run"]
                break
        # a real run produced a streamed trace and a terminal summary. The live
        # stream carries every step the handler executes (the seed `run.started`
        # marker, written at run creation, is in traceLength but not streamed).
        assert run["id"].startswith("run_")
        assert run["status"] in ("completed", "waiting_approval")
        assert len(traces) == run["traceLength"] - 1
        kinds = [t["kind"] for t in traces]
        assert any(k.startswith(("tool.", "model.", "memory.")) for k in kinds)
        assert kinds[-1] in ("run.completed", "approval.requested")


def test_ws_message_threads_into_session_and_replies(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "message", "channel": "web", "externalId": "user-7",
                      "content": "my dashboard is down"})
        replies, run = [], None
        # `run` is always the terminal frame; reply frames (if any) precede it.
        while True:
            m = ws.receive_json()
            if m["type"] == "message":
                replies.append(m["message"])
            elif m["type"] == "run":
                run = m["run"]
                break
        assert run is not None
        # any reply frames that arrived were the agent's own messages
        assert all(r["role"] in ("assistant", "agent") for r in replies)


def test_ws_replay_and_ping(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    # first create a run over HTTP
    rid = c.post("/inbound", json={"email": "ada@example.com"}).json()["runId"]
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "replay", "runId": rid})
        msgs, run = [], None
        while True:
            m = ws.receive_json()
            if m["type"] == "trace":
                msgs.append(m)
            elif m["type"] == "run":
                run = m["run"]
                break
        assert run["id"] == rid and len(msgs) == run["traceLength"]


def test_ws_requires_token_when_set(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, token="sek")
    # no token → first frame is the auth error, then close
    with c.websocket_connect("/ws") as ws:
        m = ws.receive_json()
        assert m["type"] == "error" and m["code"] == "E_UNAUTHORIZED"
    # correct token → ready
    with c.websocket_connect("/ws?token=sek") as ws:
        assert ws.receive_json()["type"] == "ready"
