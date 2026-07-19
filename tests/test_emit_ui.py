"""ctx.emit_ui: a first-class UI frame on the turn stream (cards/forms/charts),
so the client never scrapes tool-call traces to build custom UI. Journaled, so
a replay after an approval pause does NOT re-emit."""

import asyncio

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store
from rya import turns


UI_AGENT = '''
from rya import define_agent
agent = define_agent()

@agent.tool("email.send")
async def send(input):
    return {"messageId": "m1"}

@agent.on_event
async def handle(ctx, event):
    # emit a UI card, then gate an action behind approval so we can test that
    # the pre-approval UI frame is NOT re-emitted on resume
    ctx.emit_ui("recommendation_cards", {"cards": [{"id": "P-1", "fee": 18250}]})
    if event.payload.get("gate"):
        await ctx.approvals.request(title="Send", body="x",
            action={"tool": "email.send", "input": {"to": "a@b.c"}})
        ctx.emit_ui("sent_confirmation", {"ok": True})
    return {"done": True}
'''


def _engine(tmp_path) -> Engine:
    scaffold.write_project(tmp_path, "ui-agent", template="demo")
    (tmp_path / "src" / "agent.py").write_text(UI_AGENT)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def test_emit_ui_reaches_a_subscriber(tmp_path):
    engine = _engine(tmp_path)
    frames = []
    run = engine.run_event("message.received", {"email": "a@x.com"},
                           on_ui=lambda f: frames.append(f))
    assert run["status"] == "completed"
    assert frames == [{"component": "recommendation_cards",
                       "data": {"cards": [{"id": "P-1", "fee": 18250}]}}]


def test_emit_ui_lands_on_turn_buffer(tmp_path):
    engine = _engine(tmp_path)
    tid = turns.create_turn(engine, "message.received", {"email": "a@x.com"})["turnId"]
    turns.execute_pending(engine)
    ui = [f for f in turns.read_stream(engine, tid) if f["kind"] == "ui"]
    assert len(ui) == 1
    assert ui[0]["data"]["component"] == "recommendation_cards"


def test_ui_not_reemitted_after_approval_replay(tmp_path):
    engine = _engine(tmp_path)
    tid = turns.create_turn(engine, "message.received", {"email": "a@x.com", "gate": True})["turnId"]
    turns.execute_pending(engine)
    # paused: exactly one ui frame so far (the pre-approval card)
    pre = [f for f in turns.read_stream(engine, tid) if f["kind"] == "ui"]
    assert len(pre) == 1 and pre[0]["data"]["component"] == "recommendation_cards"

    approval = engine.store.list_approvals("pending")[0]
    turns.resolve_on_stream(engine, approval["id"], approve=True)
    ui = [f for f in turns.read_stream(engine, tid) if f["kind"] == "ui"]
    comps = [f["data"]["component"] for f in ui]
    # the pre-approval card is NOT re-emitted (memoized); the post-approval one is
    assert comps == ["recommendation_cards", "sent_confirmation"], comps


def test_emit_ui_streams_as_sse_frame(tmp_path):
    scaffold.write_project(tmp_path, "ui-sse", template="demo")
    (tmp_path / "src" / "agent.py").write_text(UI_AGENT)
    client = TestClient(build_app(tmp_path))
    with client.stream("POST", "/agents/_/events/stream",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}) as r:
        body = "".join(r.iter_text())
    assert "event: ui" in body
    assert "recommendation_cards" in body
