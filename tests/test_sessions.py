"""Conversation handling: sessions + messages store, ctx.sessions, and API."""

import asyncio

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import RuntimeContext
from rya.snapshot import build_console
from rya.store import Store
from rya.tools.registry import default_registry as tools_registry
from rya.models.registry import default_registry as models_registry


def test_filestore_sessions_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.ensure()

    s = store.create_session(agent="a", channel="slack", external_id="T1/C2", title="Ada")
    assert s["id"].startswith("ses_")
    assert store.find_session("a", "slack", "T1/C2")["id"] == s["id"]
    # idempotent lookup key — different thread is a different session
    assert store.find_session("a", "slack", "other") is None

    m1 = store.append_message(s["id"], "user", "hi", runId="run_1")
    m2 = store.append_message(s["id"], "assistant", "hello")
    assert m1["seq"] == 0 and m2["seq"] == 1
    assert m1["runId"] == "run_1"

    full = store.get_session(s["id"])
    assert full["messageCount"] == 2
    assert [m["content"] for m in full["messages"]] == ["hi", "hello"]
    assert store.list_messages(s["id"])[1]["role"] == "assistant"
    assert store.list_sessions("a")[0]["id"] == s["id"]
    assert store.list_sessions("other-agent") == []


def _ctx(tmp_path):
    scaffold.write_project(tmp_path, "convo", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path)
    store.ensure()
    run = {"id": "run_test", "journal": {}, "trace": []}
    return RuntimeContext(
        store=store, manifest=manifest, run=run,
        tools=tools_registry(), models=models_registry(), project_root=tmp_path,
    ), store


def test_ctx_sessions_get_or_create_and_history(tmp_path):
    ctx, store = _ctx(tmp_path)

    async def go():
        a = await ctx.sessions.get_or_create("email", "thread-9", title="Ada")
        b = await ctx.sessions.get_or_create("email", "thread-9")
        assert a["id"] == b["id"]  # same thread → same session
        await ctx.sessions.append(a["id"], "user", "where is my order?")
        await ctx.sessions.append(a["id"], "assistant", "let me check")
        hist = await ctx.sessions.history(a["id"], limit=10)
        assert [m["role"] for m in hist] == ["user", "assistant"]
        # message is tied back to the producing run
        assert hist[0]["runId"] == "run_test"
        hits = await ctx.sessions.search(a["id"], "order")
        assert len(hits) == 1 and "order" in hits[0]["content"]
        return a["id"]

    sid = asyncio.run(go())
    # durable across a fresh context (separate process simulation)
    assert store.get_session(sid)["messageCount"] == 2


def test_console_and_api_expose_sessions(tmp_path, monkeypatch):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "convo", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    s = store.create_session(agent="convo", channel="slack", external_id="C1", title="Bug")
    store.append_message(s["id"], "user", "it crashed")
    store.append_message(s["id"], "assistant", "on it")

    console = build_console(manifest, store, agent, tmp_path)
    assert console["stats"]["sessions"] == 1
    assert console["stats"]["messages"] == 2
    assert console["sessions"][0]["id"] == s["id"]

    c = TestClient(build_app(tmp_path))
    listed = c.get("/sessions").json()["sessions"]
    assert listed[0]["id"] == s["id"]
    one = c.get(f"/sessions/{s['id']}").json()
    assert [m["content"] for m in one["messages"]] == ["it crashed", "on it"]
    assert c.get("/sessions/ses_missing").status_code == 404
