"""The minimal (default) scaffold: real seams only, no mocked domain data.

The demo template (mock CRM domain) still exists behind --template demo and is
what most of the suite exercises; THIS file pins the promise that what a new
user scaffolds by default contains no mocks."""

import pytest
import yaml

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store

MOCK_TOOL_IDS = {"crm.lookup", "calendar.read", "email.send"}


def make_engine(tmp_path) -> Engine:
    scaffold.write_project(tmp_path, "clean-agent")  # default template = minimal
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def test_default_scaffold_declares_no_mock_tools(tmp_path):
    scaffold.write_project(tmp_path, "clean-agent")
    doc = yaml.safe_load((tmp_path / "rya.agent.yaml").read_text())
    declared = {t["id"] for t in doc["tools"]}
    assert declared.isdisjoint(MOCK_TOOL_IDS), f"mock tools leaked into default scaffold: {declared}"
    assert "web.fetch" in declared          # the real built-in
    assert doc.get("models") in (None, [])  # no mock custom models
    # and no mock-registry stub files
    assert not (tmp_path / "src" / "tools.py").exists()
    assert not (tmp_path / "src" / "models.py").exists()


def test_default_scaffold_runs_to_completion_offline(tmp_path):
    engine = make_engine(tmp_path)
    run = engine.run_event("message.received", {"email": "ada@x.com", "body": "hello"})
    assert run["status"] == "completed"
    kinds = [e["kind"] for e in run["trace"]]
    assert "llm.respond" in kinds
    assert "tool.call" in kinds  # notes.save - the project-defined REAL tool
    # the project tool executed via the agent handler, not the mock registry
    tool_ev = next(e for e in run["trace"] if e["kind"] == "tool.call")
    assert tool_ev["data"].get("impl") == "agent"


def test_console_snapshot_flags_no_mocks_for_minimal(tmp_path):
    from rya.snapshot import build_console
    engine = make_engine(tmp_path)
    engine.run_event("message.received", {"email": "a@x.com", "body": "hi"})
    snap = build_console(engine.manifest, engine.store, engine.agent, engine.project_root)
    assert all(not t["mockImpl"] for t in snap["tools"]), snap["tools"]


def test_console_snapshot_flags_mocks_for_demo(tmp_path):
    from rya.snapshot import build_console
    scaffold.write_project(tmp_path, "demo-agent", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    snap = build_console(manifest, store, agent, tmp_path)
    flags = {t["id"]: t["mockImpl"] for t in snap["tools"]}
    assert flags["crm.lookup"] is True
    assert flags["email.send"] is True


def test_unknown_template_rejected(tmp_path):
    with pytest.raises(ValueError):
        scaffold.write_project(tmp_path, "x", template="fancy")
