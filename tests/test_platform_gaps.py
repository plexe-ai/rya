"""The five platform gaps surfaced by real production agent workloads:
per-purpose model routes, server-side arg pinning, runtime kill switches,
token streaming, and the grounding gate."""

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.errors import RyaError
from rya.guard import grounding_check
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import RuntimeContext
from rya.store import Store


def make_engine(tmp_path, mutate=None) -> Engine:
    scaffold.write_project(tmp_path, "gap-agent")
    if mutate:
        p = tmp_path / "rya.agent.yaml"
        doc = yaml.safe_load(p.read_text())
        mutate(doc)
        p.write_text(yaml.safe_dump(doc))
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def make_ctx(engine: Engine, on_token=None, payload=None) -> RuntimeContext:
    event = engine.make_event("message.received", payload or {"email": "ada@x.com"})
    run = engine._new_run("event", event)
    return RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                          tools=engine.tools, models=engine.models,
                          project_root=engine.project_root, agent=engine.agent,
                          on_token=on_token)


# ---- 1. model routes --------------------------------------------------------

def test_llm_route_selects_named_model(tmp_path):
    engine = make_engine(tmp_path, lambda d: d["model"].update(
        {"routes": {"extract": {"model": "mock-extract"},
                    "classify": {"model": "mock-classify", "temperature": 0.0}}}))
    ctx = make_ctx(engine)

    default = asyncio.run(ctx.llm.respond(system="s", input={}))
    assert default.model == "mock-llm"

    routed = asyncio.run(ctx.llm.respond(system="s", input={}, route="extract"))
    assert routed.model == "mock-extract"

    # the journal label carries the route so traces distinguish purposes
    labels = [e["label"] for e in ctx.run["journal"].values() if e["kind"] == "llm.respond"]
    assert "extract:mock-extract" in labels


def test_llm_unknown_route_raises(tmp_path):
    engine = make_engine(tmp_path)
    ctx = make_ctx(engine)
    with pytest.raises(RyaError) as e:
        asyncio.run(ctx.llm.respond(system="s", input={}, route="nope"))
    assert e.value.code == "E_MODEL_ROUTE_NOT_FOUND"


# ---- 2. server-side arg pinning ----------------------------------------------

def _add_pin(doc):
    for t in doc["tools"]:
        if t["id"] == "crm.lookup":
            t["pin"] = {"email": "event.payload.email", "region": "ap-south-1"}


def test_pinned_args_overwrite_model_supplied(tmp_path):
    engine = make_engine(tmp_path, _add_pin)
    ctx = make_ctx(engine, payload={"email": "real@x.com"})

    # A confused/malicious model supplies someone else's email; the pin wins.
    result = asyncio.run(ctx.tools.call("crm.lookup", {"email": "attacker@evil.com"}))
    assert result["email"] == "real@x.com"

    entry = next(e for e in ctx.run["trace"] if e["kind"] == "tool.call")
    assert entry["data"]["input"]["email"] == "real@x.com"
    assert entry["data"]["input"]["region"] == "ap-south-1"  # literal pin
    assert entry["data"]["pinnedArgs"] == ["email", "region"]


def test_pin_from_memory_scope(tmp_path):
    def mutate(doc):
        for t in doc["tools"]:
            if t["id"] == "crm.lookup":
                t["pin"] = {"email": "memory.agent.owner_email"}
    engine = make_engine(tmp_path, mutate)
    mem = engine.store.load_memory("agent")
    mem["kv"]["owner_email"] = "owner@x.com"
    engine.store.save_memory("agent", mem)

    ctx = make_ctx(engine)
    result = asyncio.run(ctx.tools.call("crm.lookup", {"email": "whatever@x.com"}))
    assert result["email"] == "owner@x.com"


# ---- 3. runtime kill switches --------------------------------------------------

def test_kill_switch_disables_tool_without_redeploy(tmp_path):
    engine = make_engine(tmp_path)
    client = TestClient(build_app(tmp_path))

    # works before the switch
    ctx = make_ctx(engine)
    asyncio.run(ctx.tools.call("crm.lookup", {"email": "a@x.com"}))

    r = client.put("/tools/crm.lookup/permission",
                   json={"permission": "disabled", "reason": "incident 42"})
    assert r.status_code == 200 and r.json()["version"] == 1

    with pytest.raises(RyaError) as e:
        asyncio.run(make_ctx(engine).tools.call("crm.lookup", {"email": "a@x.com"}))
    assert e.value.code == "E_TOOL_PERMISSION_DENIED"

    tools = {t["id"]: t for t in client.get("/tools").json()["tools"]}
    assert tools["crm.lookup"]["effectivePermission"] == "disabled"
    assert tools["crm.lookup"]["permission"] == "allowed"  # manifest unchanged

    # clear -> back to the manifest permission, history keeps both entries
    r = client.put("/tools/crm.lookup/permission", json={"clear": True})
    assert r.json()["version"] == 2
    asyncio.run(make_ctx(engine).tools.call("crm.lookup", {"email": "a@x.com"}))


def test_kill_switch_removes_tool_from_agent_loop(tmp_path):
    engine = make_engine(tmp_path)
    rc = engine.store.load_memory("_runtime_config")
    rc.setdefault("kv", {})["tool:crm.lookup"] = {"permission": "disabled", "version": 1}
    engine.store.save_memory("_runtime_config", rc)

    ctx = make_ctx(engine)
    out = asyncio.run(ctx.llm.run(input={"q": "look up ada"}, system="s"))
    assert all(c["tool"] != "crm.lookup" for c in out["toolCalls"])


def test_permission_api_rejects_unknown_tool_and_value(tmp_path):
    make_engine(tmp_path)
    client = TestClient(build_app(tmp_path))
    assert client.put("/tools/nope/permission", json={"permission": "disabled"}).status_code == 404
    assert client.put("/tools/crm.lookup/permission", json={"permission": "sudo"}).status_code == 400


# ---- 4. token streaming ----------------------------------------------------------

def test_llm_respond_streams_tokens(tmp_path):
    engine = make_engine(tmp_path)
    chunks = []
    ctx = make_ctx(engine, on_token=chunks.append)
    res = asyncio.run(ctx.llm.respond(system="draft a reply", input={"customer": {"name": "Ada"}}))
    assert len(chunks) > 1
    assert "".join(chunks) == res.text
    # tokens are not journaled - only the final response is
    entry = next(e for e in ctx.run["journal"].values() if e["kind"] == "llm.respond")
    assert entry["result"]["text"] == res.text


def test_websocket_emits_token_frames(tmp_path):
    scaffold.write_project(tmp_path, "ws-agent")
    client = TestClient(build_app(tmp_path))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "event", "eventType": "message.received",
                      "payload": {"email": "ada@x.com"}})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "run":
                break
        kinds = {f["type"] for f in frames}
        assert "token" in kinds, f"no token frames in {sorted(kinds)}"
        streamed = "".join(f["text"] for f in frames if f["type"] == "token")
        assert "[mock-llm]" in streamed


# ---- 5. grounding gate --------------------------------------------------------------

def test_grounding_check_unit():
    ok = grounding_check("The total is $1,299.00 per month.", [{"price": 1299.0}])
    assert ok["ok"] is True and ok["figures"] == [1299.0]

    bad = grounding_check("Only $999 today! Plus EUR 50 fee.", [{"price": 1299.0}])
    assert bad["ok"] is False and set(bad["violations"]) == {999.0, 50.0}

    none = grounding_check("No numbers with currency here, just 42 things.", [])
    assert none["ok"] is True  # bare numbers are not money figures


def test_ctx_grounding_uses_this_runs_tool_outputs(tmp_path):
    engine = make_engine(tmp_path)
    ctx = make_ctx(engine)
    asyncio.run(ctx.tools.call("crm.lookup", {"email": "a@x.com"}))  # mrr: 199
    assert ctx.guard.check_grounding("Your plan is $199.")["ok"] is True
    assert ctx.guard.check_grounding("Upgrade for $299!")["ok"] is False


def _enable_grounding(root):
    p = root / "rya.guard.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["grounding"] = {"enabled": True}
    p.write_text(yaml.safe_dump(doc))


def test_channel_send_blocks_ungrounded_money(tmp_path):
    engine = make_engine(tmp_path)
    _enable_grounding(tmp_path)
    ctx = make_ctx(engine)
    with pytest.raises(RyaError) as e:
        asyncio.run(ctx.channels.send("email", {"subject": "Offer", "body": "Pay only $9,999 now"}))
    assert e.value.code == "E_GROUNDING_BLOCKED"
    assert any(t["kind"] == "guard.grounding_blocked" for t in ctx.run["trace"])


def test_channel_send_allows_grounded_money(tmp_path):
    engine = make_engine(tmp_path)
    _enable_grounding(tmp_path)
    ctx = make_ctx(engine)
    asyncio.run(ctx.tools.call("crm.lookup", {"email": "a@x.com"}))  # mrr: 199
    res = asyncio.run(ctx.channels.send("email", {"body": "Your plan is $199 per month."}))
    assert res is not None
