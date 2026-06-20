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


SCOPED_MANIFEST = """name: ai
runtime: python
entrypoint: agent.py
model:
  default: mock-llm
tools:
  - id: gh.read
    permission: allowed
    provider: github
    scopes: [repo:read]
"""


def test_governance_applies_inside_the_loop(tmp_path):
    # The model decides to call a scoped tool, but there is NO github connection →
    # the runtime blocks it (E_NO_CONNECTION) exactly as it would for a direct call.
    ctx, _ = _ctx(tmp_path, SCOPED_MANIFEST)

    async def go():
        await ctx.llm.run(input={"q": "list issues"}, tools=["gh.read"])

    with pytest.raises(RyaError) as e:
        asyncio.run(go())
    assert e.value.code == "E_NO_CONNECTION"
