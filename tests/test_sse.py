"""SSE streaming endpoint: POST /agents/{id}/events/stream.

The default client transport for chat frontends - token/trace/message frames,
always terminated by a `run` frame."""

import json

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold


def _parse_sse(text: str) -> list:
    """Parse an SSE body into [(event, data), ...], ignoring comments."""
    frames = []
    event, data = None, []
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and event:
            frames.append((event, json.loads("\n".join(data)) if data else None))
            event, data = None, []
    return frames


def test_event_stream_tokens_then_terminal_run(tmp_path):
    scaffold.write_project(tmp_path, "sse-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    with client.stream("POST", "/agents/_/events/stream",
                       json={"type": "message.received",
                             "payload": {"email": "ada@x.com"}}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in r.iter_text())

    frames = _parse_sse(body)
    kinds = [k for k, _ in frames]

    assert "token" in kinds, f"no token frames in {kinds}"
    assert "trace" in kinds
    assert kinds[-1] == "run", "run must be the terminal frame"

    streamed = "".join(d["text"] for k, d in frames if k == "token")
    assert "[mock-llm]" in streamed

    run = frames[-1][1]
    assert run["status"] in ("completed", "waiting_approval")
    assert run["id"].startswith("run_")


def test_event_stream_chat_message_frames(tmp_path):
    scaffold.write_project(tmp_path, "sse-chat-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    with client.stream("POST", "/agents/_/events/stream",
                       json={"type": "message.received",
                             "payload": {"email": "ada@x.com", "channel": "web",
                                         "externalId": "ada@x.com",
                                         "body": "help me"}}) as r:
        body = "".join(chunk for chunk in r.iter_text())

    frames = _parse_sse(body)
    kinds = [k for k, _ in frames]
    # assistant session replies arrive before the terminal run frame
    if "message" in kinds:
        assert kinds.index("message") < kinds.index("run")
    assert kinds[-1] == "run"


def test_event_stream_survives_unknown_event_type(tmp_path):
    scaffold.write_project(tmp_path, "sse-any-agent", template="demo")
    client = TestClient(build_app(tmp_path))
    with client.stream("POST", "/agents/_/events/stream",
                       json={"type": "totally.unknown", "payload": {}}) as r:
        body = "".join(chunk for chunk in r.iter_text())
    frames = _parse_sse(body)
    # the scaffold's on_event handler receives every event type; the stream
    # must still terminate with a run (or error) frame, never hang
    assert frames[-1][0] in ("run", "error")
