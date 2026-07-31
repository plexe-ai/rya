"""The LLM-first layer: structured output + the governed tool-calling agent loop.

The model can reason and act, but every tool call still flows through Rya's
permissions / scoped credentials / Action Guard — governance holds even when the
model is the one deciding what to do.
"""

import asyncio

import pytest

from rya.errors import RyaError
from rya.manifest import load_manifest
from rya.runtime import load_agent
from rya.sdk.context import RuntimeContext
from rya.store import Store
from rya.models.registry import default_registry as models_registry
from rya.tools.registry import default_registry as tools_registry

AGENT_PY = """
from rya import define_agent
agent = define_agent()

@agent.tool("lookup")
async def lookup(inp):
    return {"found": True, "name": "Ada", "plan": "enterprise"}

@agent.tool("gh.read")
async def gh_read(inp):
    return {"issues": 3}

@agent.on_event
async def h(ctx, e):
    return {}
"""


def _ctx(tmp_path, manifest_yaml):
    (tmp_path / "rya.agent.yaml").write_text(manifest_yaml)
    (tmp_path / "agent.py").write_text(AGENT_PY)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()
    run = {"id": "run_llm", "journal": {}, "trace": []}
    return RuntimeContext(store=store, manifest=manifest, run=run, tools=tools_registry(),
                          models=models_registry(), project_root=tmp_path, agent=agent), store


BASE_MANIFEST = """name: ai
runtime: python
entrypoint: agent.py
model:
  default: mock-llm
tools:
  - id: lookup
    permission: allowed
  - id: danger
    permission: approval_required
    url: https://api.example.com/do
"""


def test_structured_output(tmp_path):
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)

    async def go():
        schema = {"type": "object", "required": ["sentiment", "score"],
                  "properties": {"sentiment": {"type": "string", "enum": ["positive", "negative"]},
                                 "score": {"type": "number"}}}
        r = await ctx.llm.respond(system="classify the message", input={"text": "love it"}, schema=schema)
        assert r.json is not None
        assert r.json["sentiment"] == "positive" and r.json["score"] == 0  # deterministic mock
        # required keys present + types respected
        assert set(r.json) >= {"sentiment", "score"}

    asyncio.run(go())


def test_governed_agent_loop_calls_tool_then_answers(tmp_path):
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)

    async def go():
        out = await ctx.llm.run(input={"q": "look up Ada"}, system="you find customers", tools=["lookup"])
        assert out["toolCalls"], "the loop should have called a tool"
        call = out["toolCalls"][0]
        assert call["tool"] == "lookup" and call["result"]["name"] == "Ada"
        assert "done" in out["text"].lower()
        return out

    out = asyncio.run(go())
    # the tool call is in the run trace (governed + audited like any ctx.tools.call)
    kinds = [(e["kind"], e.get("label")) for e in ctx.run["trace"]]
    assert ("tool.call", "lookup") in kinds
    assert any(k == "llm.chat" for k, _ in kinds)


def test_loop_never_autonomously_calls_gated_tools(tmp_path):
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)

    async def go():
        # `danger` is approval_required → it is NOT exposed to the model at all
        out = await ctx.llm.run(input={"do": "something risky"}, tools=["danger"])
        return out

    out = asyncio.run(go())
    assert out["toolCalls"] == []  # gated action never ran autonomously
    assert not any(e.get("label") == "danger" for e in ctx.run["trace"])


def test_run_replays_history_before_current_message(tmp_path, monkeypatch):
    # Prior turns from ctx.sessions.history(...) are seeded into the loop ahead of
    # the current message, so follow-ups ("#2", "compare those") have context.
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)
    captured = {}
    import rya.providers as providers

    def fake_chat(*, messages, **_kw):
        captured["messages"] = messages
        return {"text": "done", "toolCalls": []}

    monkeypatch.setattr(providers, "chat", fake_chat)

    async def go():
        history = [
            {"role": "user", "content": "find UK data science"},
            {"role": "assistant", "content": "here are 3 options"},
        ]
        await ctx.llm.run(input={"q": "compare #2 and #3"}, system="s",
                          tools=["lookup"], history=history)

    asyncio.run(go())
    msgs = captured["messages"]
    assert msgs[0] == {"role": "user", "content": "find UK data science"}
    assert msgs[1] == {"role": "assistant", "content": "here are 3 options"}
    # current message comes last, carrying the raw input dict
    assert msgs[2]["role"] == "user" and msgs[2]["content"] == {"q": "compare #2 and #3"}


def test_run_history_filters_blanks_and_unknown_roles(tmp_path, monkeypatch):
    # Storage may hold blank assistant turns or non-user/assistant rows; those must
    # never reach the provider (Anthropic rejects empty content / stray roles).
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)
    captured = {}
    import rya.providers as providers

    def fake_chat(*, messages, **_kw):
        captured["messages"] = messages
        return {"text": "done", "toolCalls": []}

    monkeypatch.setattr(providers, "chat", fake_chat)

    history = [
        {"role": "assistant", "content": ""},       # blank → skip
        {"role": "assistant", "content": "   "},     # whitespace-only → skip
        {"role": "system", "content": "noise"},      # non user/assistant → skip
        {"role": "tool", "content": "result"},       # tool role → skip
        {"role": "user", "content": "keep me"},      # kept
    ]
    asyncio.run(ctx.llm.run(input={"q": "now"}, tools=["lookup"], history=history))
    assert captured["messages"] == [
        {"role": "user", "content": "keep me"},
        {"role": "user", "content": {"q": "now"}},
    ]


def test_run_without_history_is_unchanged(tmp_path, monkeypatch):
    # Backward compatibility: no history → only the current message is seeded.
    ctx, _ = _ctx(tmp_path, BASE_MANIFEST)
    captured = {}
    import rya.providers as providers

    def fake_chat(*, messages, **_kw):
        captured["messages"] = messages
        return {"text": "done", "toolCalls": []}

    monkeypatch.setattr(providers, "chat", fake_chat)
    asyncio.run(ctx.llm.run(input={"q": "solo"}, tools=["lookup"]))
    assert captured["messages"] == [{"role": "user", "content": {"q": "solo"}}]


SCOPED_MANIFEST = """name: ai
runtime: python
entrypoint: agent.py
model:
  default: mock-llm
tools:
  # Deliberately NOT implemented by @agent.tool in AGENT_PY: a scoped credential
  # is only resolved for tools that actually egress with it (a `url:` tool or a
  # registry backend). A local leaf handler never receives the secret, so a
  # `provider:` on one is governance metadata and requiring a live connection
  # for an offline leaf would be wrong — see _Tools.prepare.
  - id: gh_issues
    permission: allowed
    url: https://api.github.com/issues
    provider: github
    scopes: [repo:read]
"""


def test_governance_applies_inside_the_loop(tmp_path):
    # The model decides to call a scoped tool, but there is NO github connection →
    # the runtime blocks it (E_NO_CONNECTION) exactly as it would for a direct call.
    ctx, _ = _ctx(tmp_path, SCOPED_MANIFEST)

    async def go():
        await ctx.llm.run(input={"q": "list issues"}, tools=["gh_issues"])

    with pytest.raises(RyaError) as e:
        asyncio.run(go())
    assert e.value.code == "E_NO_CONNECTION"


def test_a_local_leaf_with_a_provider_needs_no_connection(tmp_path):
    """The other half of that rule, pinned so the two cannot drift: a tool the
    bundle implements in-process is offline, so its `provider:` is metadata and
    the loop runs it without a live connection."""
    ctx, _ = _ctx(tmp_path, SCOPED_MANIFEST.replace(
        "  - id: gh_issues\n    permission: allowed\n    url: https://api.github.com/issues\n",
        "  - id: gh.read\n    permission: allowed\n"))
    out = asyncio.run(ctx.llm.run(input={"q": "list issues"}, tools=["gh.read"]))
    assert out["toolCalls"][0]["result"] == {"issues": 3}
