"""@agent.tool and manifest tools can declare an input_schema, so the model in
ctx.llm.run gets the argument contract instead of guessing names.

Regression for finding #2 of the CSA-clone experiment: a schemaless tool led
the model to call shortlist_add(programme_id=...) where the code read
programmeId, silently no-opping."""

import asyncio

import yaml

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


def _engine(tmp_path, mutate=None, agent_src=None):
    scaffold.write_project(tmp_path, "schema-agent", template="demo")
    if mutate:
        p = tmp_path / "rya.agent.yaml"
        doc = yaml.safe_load(p.read_text())
        mutate(doc)
        p.write_text(yaml.safe_dump(doc))
    if agent_src:
        (tmp_path / "src" / "agent.py").write_text(agent_src)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def _tool_defs_seen(engine):
    """Run the mock loop and capture the tool defs handed to the provider."""
    captured = {}
    import rya.providers as providers  # ctx.llm.run imports `chat` from the package
    orig = providers.chat

    def spy(**kw):
        captured["tools"] = kw.get("tools")
        return orig(**kw)

    providers.chat = spy
    try:
        from rya.sdk.context import RuntimeContext
        run = engine._new_run("event", engine.make_event("message.received", {"email": "a@x.com"}))
        ctx = RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                             tools=engine.tools, models=engine.models,
                             project_root=engine.project_root, agent=engine.agent)
        asyncio.run(ctx.llm.run(input={"q": "hi"}, system="s", tools=["crm.lookup"]))
    finally:
        providers.chat = orig
    return {t["name"]: t for t in (captured.get("tools") or [])}


def test_manifest_input_schema_reaches_the_model(tmp_path):
    schema = {"type": "object", "required": ["email"],
              "properties": {"email": {"type": "string", "format": "email"}}}

    def mutate(doc):
        for t in doc["tools"]:
            if t["id"] == "crm.lookup":
                t["input_schema"] = schema
    engine = _engine(tmp_path, mutate)
    defs = _tool_defs_seen(engine)
    assert defs["crm.lookup"]["input_schema"] == schema


def test_agent_tool_decorator_input_schema(tmp_path):
    src = '''
from rya import define_agent
agent = define_agent()

CATALOGUE_SCHEMA = {"type": "object", "required": ["programmeId"],
                    "properties": {"programmeId": {"type": "string"}}}

@agent.tool("crm.lookup", input_schema=CATALOGUE_SCHEMA)
async def crm_lookup(input):
    return {"ok": True, "programmeId": input.get("programmeId")}

@agent.on_event
async def handle(ctx, event):
    return {}
'''
    engine = _engine(tmp_path, agent_src=src)
    defs = _tool_defs_seen(engine)
    assert defs["crm.lookup"]["input_schema"]["properties"]["programmeId"]["type"] == "string"


def test_manifest_schema_wins_over_decorator(tmp_path):
    manifest_schema = {"type": "object", "properties": {"fromManifest": {"type": "boolean"}}}

    def mutate(doc):
        for t in doc["tools"]:
            if t["id"] == "crm.lookup":
                t["input_schema"] = manifest_schema
    src = '''
from rya import define_agent
agent = define_agent()

@agent.tool("crm.lookup", input_schema={"type": "object", "properties": {"fromDecorator": {"type": "string"}}})
async def crm_lookup(input):
    return {}

@agent.on_event
async def handle(ctx, event):
    return {}
'''
    engine = _engine(tmp_path, mutate=mutate, agent_src=src)
    defs = _tool_defs_seen(engine)
    assert "fromManifest" in defs["crm.lookup"]["input_schema"]["properties"]
    assert "fromDecorator" not in defs["crm.lookup"]["input_schema"]["properties"]


def test_schemaless_tool_still_defaults(tmp_path):
    engine = _engine(tmp_path)
    defs = _tool_defs_seen(engine)
    assert defs["crm.lookup"]["input_schema"] == {"type": "object"}
