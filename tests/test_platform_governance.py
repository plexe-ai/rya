"""PLATFORM_DESIGN §11 items 1-4: the governance and durability gaps that had to
close before the api/worker split could mean anything.

1. The approval path is governed (§11.1) — it used to be a second, ungoverned
   dispatch implementation.
2. Kill switches live in privileged state a bundle cannot write (§11.2).
3. Journal steps are content-keyed and replay fails closed on drift (§11.3/D9).
4. The journal is append-only and billable facts are metered (§11.4/D10).
"""

import asyncio

import pytest
import yaml

from rya.cli import scaffold
from rya.errors import RyaError
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import POLICY_KILLSWITCHES, RuntimeContext, content_key
from rya.store import Store

ENTRYPOINT = '''
from rya import define_agent
agent = define_agent()

@agent.tool("db.write")
async def db_write(input):
    return {"ok": True, "wrote": input.get("value"), "actor": input.get("actor")}

@agent.on_event
async def main(ctx, event):
    res = await ctx.approvals.request(title="write?", body="please",
                                      action={"tool": "db.write",
                                              "input": {"value": 1}})
    return {"resumed": True, "action": res.get("actionResult")}
'''


def _project(tmp_path, tools=None, entrypoint=ENTRYPOINT):
    scaffold.write_project(tmp_path, "gov", template="demo")
    (tmp_path / "src" / "agent.py").write_text(entrypoint)
    p = tmp_path / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = tools if tools is not None else [
        {"id": "db.write", "permission": "approval_required"}]
    p.write_text(yaml.safe_dump(doc))
    manifest = load_manifest(p)
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def _pause(engine):
    engine.run_event("message.received", {"email": "a@x.co"})
    return engine.store.list_approvals(status="pending")[0]


# ---- 1. the approval path is governed ---------------------------------------

def test_approved_action_executes_through_the_governed_path(tmp_path):
    engine = _project(tmp_path)
    approval = _pause(engine)
    run = engine.approve(approval["id"])
    assert run["status"] == "completed", run.get("error")
    assert engine.store.get_approval(approval["id"])["actionResult"] == {
        "ok": True, "wrote": 1, "actor": None}


def test_kill_switch_beats_a_stale_approval(tmp_path):
    """The sharpest case §11.1 opens up: an operator disables a tool while its
    approval sits pending. The old path had no permission check at all, so the
    human's earlier click won and the killed tool ran anyway."""
    engine = _project(tmp_path)
    approval = _pause(engine)

    engine.store.policy_set(POLICY_KILLSWITCHES,
                            {"tool:db.write": {"permission": "disabled"}},
                            actor="ops@acme.io")

    with pytest.raises(RyaError) as e:
        engine.approve(approval["id"])
    assert e.value.code == "E_TOOL_PERMISSION_DENIED"
    # and the side effect genuinely did not happen
    assert engine.store.get_approval(approval["id"])["status"] == "pending"


def test_undeclared_tool_in_an_action_is_refused(tmp_path):
    """@agent.tool is an implementation, not an authorization. The old path fell
    through to the registry and ran whatever the action named."""
    engine = _project(tmp_path, tools=[])
    engine.run_event("message.received", {"email": "a@x.co"})
    approval = engine.store.list_approvals(status="pending")[0]
    with pytest.raises(RyaError) as e:
        engine.approve(approval["id"])
    assert e.value.code == "E_TOOL_NOT_FOUND"


def test_approved_action_applies_server_side_pins(tmp_path):
    """Pins overwrite caller-supplied args from trusted state. The action's input
    was recorded when the approval was created — a pin must still be re-resolved
    at execution time, not trusted from the stored action."""
    engine = _project(tmp_path, tools=[
        {"id": "db.write", "permission": "approval_required",
         "pin": {"actor": "event.payload.email"}}])
    approval = _pause(engine)
    engine.approve(approval["id"])
    result = engine.store.get_approval(approval["id"])["actionResult"]
    assert result["actor"] == "a@x.co"


def test_approved_action_is_scrubbed(tmp_path):
    """guard.scrub applies at the tool boundary on the approval path too, so a
    secret id cannot reach the approval record or the trace."""
    (tmp_path / "rya.guard.yaml").write_text(yaml.safe_dump({
        "default": "allow",
        "secrecy": {"enabled": True,
                    "patterns": [{"id": "acct", "pattern": r"ACC-\d{4}",
                                  "replacement": "«acct»"}]},
    }))
    engine = _project(tmp_path, entrypoint=ENTRYPOINT.replace(
        '"wrote": input.get("value")', '"wrote": "ACC-1234"'))
    approval = _pause(engine)
    engine.approve(approval["id"])
    assert engine.store.get_approval(approval["id"])["actionResult"]["wrote"] == "«acct»"


# ---- 2. kill switches are privileged state ----------------------------------

def test_kill_switch_reads_from_the_policy_store(tmp_path):
    engine = _project(tmp_path, tools=[{"id": "db.write", "permission": "allowed"}])
    ctx = _ctx(engine)
    from rya.manifest.schema import Permission
    assert ctx._effective_tool_permission("db.write") == Permission.allowed
    engine.store.policy_set(POLICY_KILLSWITCHES,
                            {"tool:db.write": {"permission": "disabled"}})
    assert ctx._effective_tool_permission("db.write") == Permission.disabled


def test_bundle_cannot_write_a_reserved_memory_scope(tmp_path):
    """Kill switches used to live in the `_runtime_config` memory scope, which
    ctx.memory.set could overwrite — governance a client can edit."""
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    with pytest.raises(RyaError) as e:
        asyncio.run(ctx.memory.set("tool:db.write", {"permission": "allowed"},
                                   scope="_runtime_config"))
    assert e.value.code == "E_POLICY_READONLY"
    # reads stay legal — the legacy read-through in _killswitches needs them
    assert asyncio.run(ctx.memory.get("anything", scope="_runtime_config")) is None


def test_policy_writes_are_audited(tmp_path):
    engine = _project(tmp_path)
    engine.store.policy_set(POLICY_KILLSWITCHES, {"tool:a": {"permission": "disabled"}},
                            actor="ada@acme.io")
    engine.store.policy_set(POLICY_KILLSWITCHES, {"tool:a": {"permission": "allowed"}},
                            actor="grace@acme.io")
    history = engine.store.policy_history(POLICY_KILLSWITCHES)
    assert [h["actor"] for h in history] == ["grace@acme.io", "ada@acme.io"]
    assert history[0]["version"] == 2
    assert history[0]["previous"] == {"tool:a": {"permission": "disabled"}}


def test_unreadable_policy_fails_closed(tmp_path):
    engine = _project(tmp_path, tools=[{"id": "db.write", "permission": "allowed"}])
    ctx = _ctx(engine)

    def boom(_key):
        raise RuntimeError("policy store unreachable")

    ctx.store.policy_get = boom
    from rya.manifest.schema import Permission
    assert ctx._effective_tool_permission("db.write") == Permission.disabled


# ---- 3. content-keyed journal, fail closed on drift -------------------------

def test_content_key_ignores_freshly_generated_fields():
    """`loopId` is minted per agent-loop invocation; including it in the key
    would make every replay a drift."""
    a = content_key("llm.chat", "step 0", {"system": "s", "loopId": "loop_aaa"})
    b = content_key("llm.chat", "step 0", {"system": "s", "loopId": "loop_bbb"})
    assert a == b
    assert a != content_key("llm.chat", "step 0", {"system": "different"})


def test_replay_of_a_matching_step_is_memoized(tmp_path):
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    calls = []

    def once():
        calls.append(1)
        return {"n": len(calls)}

    assert ctx._step("tool.call", "t", once, {"input": {"a": 1}}) == {"n": 1}
    ctx._seq = 0  # a replay starts the sequence over
    assert ctx._step("tool.call", "t", once, {"input": {"a": 1}}) == {"n": 1}
    assert len(calls) == 1


def test_replay_against_drifted_code_fails_closed(tmp_path):
    """The D9 property. Before this, step N matched on a bare ordinal and
    compared nothing — so a bundle that changed what it does at step N silently
    received the OLD step's result."""
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    ctx._step("tool.call", "charge.card", lambda: {"charged": 100}, {"input": {"amt": 100}})

    ctx._seq = 0
    with pytest.raises(RyaError) as e:
        ctx._step("tool.call", "charge.card", lambda: {"charged": 1},
                  {"input": {"amt": 1}})   # same tool, DIFFERENT amount
    assert e.value.code == "E_JOURNAL_DRIFT"
    assert "charge.card" in e.value.message

    ctx._seq = 0
    with pytest.raises(RyaError) as e:
        ctx._step("channel.send", "email", lambda: {}, {"input": {"amt": 100}})
    assert e.value.code == "E_JOURNAL_DRIFT"


def test_legacy_journal_entries_without_a_key_still_replay(tmp_path):
    """Journals written before D9 shipped with their code, so the ordinal was
    sound. Accept them, but still refuse an outright kind change."""
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    ctx.run["journal"]["0"] = {"seq": 0, "kind": "tool.call", "label": "t",
                               "status": "done", "result": {"legacy": True}}
    assert ctx._step("tool.call", "t", lambda: {"fresh": True},
                     {"input": {"whatever": 1}}) == {"legacy": True}
    ctx._seq = 0
    with pytest.raises(RyaError):
        ctx._step("llm.respond", "t", lambda: {}, None)


def test_approval_drift_does_not_inherit_a_human_answer(tmp_path):
    """Resuming a run whose code now asks a DIFFERENT question must not reuse the
    approval a human granted for the old one."""
    engine = _project(tmp_path)
    approval = _pause(engine)
    run = engine.store.get_run(approval["runId"])
    entry = next(e for e in run["journal"].values() if e["kind"] == "approval")
    entry["status"] = "approved"
    entry["result"] = {"approvalId": approval["id"], "status": "approved"}
    engine.store.save_run(run)

    ctx = RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                         tools=engine.tools, models=engine.models,
                         project_root=engine.project_root, agent=engine.agent)
    with pytest.raises(RyaError) as e:
        ctx._request_approval("wire $1M?", "please",
                              {"tool": "db.write", "input": {"value": 1_000_000}})
    assert e.value.code == "E_JOURNAL_DRIFT"


# ---- 4. append-only journal + durable meter ---------------------------------

def test_journal_is_appended_not_rewritten(tmp_path):
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    ctx._step("memory.set", "a:1", lambda: 1, {"scope": "a", "key": "1"})
    ctx._step("memory.set", "a:2", lambda: 2, {"scope": "a", "key": "2"})

    rows = engine.store.journal_read(ctx.run["id"])
    assert sorted(rows) == ["0", "1"]
    assert rows["0"]["kind"] == "memory.set" and rows["0"]["contentKey"]
    assert all(r["revision"] == 0 for r in rows.values())


def test_a_revised_step_keeps_its_predecessor(tmp_path):
    """An approval's pending -> approved transition adds a revision rather than
    destroying the prior row, so the ledger stays auditable."""
    engine = _project(tmp_path)
    approval = _pause(engine)
    run_id = approval["runId"]
    before = engine.store.journal_revisions(run_id)
    engine.store.journal_append(run_id, {**before[-1], "status": "approved"})

    revisions = engine.store.journal_revisions(run_id)
    assert len(revisions) == len(before) + 1
    assert [r["status"] for r in revisions[-2:]] == ["pending", "approved"]
    assert engine.store.journal_read(run_id)[str(before[-1]["seq"])]["status"] == "approved"


def test_model_calls_land_in_the_durable_meter(tmp_path):
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    ctx._step("llm.respond", "mock-llm",
              lambda: {"text": "hi", "model": "mock-llm", "provider": "mock",
                       "usage": {"input": 120, "output": 30}},
              {"system": "s"})

    facts = engine.store.meter_read(run_id=ctx.run["id"])
    assert len(facts) == 1
    assert facts[0]["inputTokens"] == 120 and facts[0]["outputTokens"] == 30
    assert facts[0]["model"] == "mock-llm" and facts[0]["kind"] == "llm.respond"


def test_usage_prefers_the_meter_over_the_trace(tmp_path, monkeypatch):
    """D10: money comes from the ledger, not from run['trace'] — which is
    redacted, truncated and rewritten."""
    from rya.observability.usage import run_usage
    engine = _project(tmp_path)
    ctx = _ctx(engine)
    ctx._step("llm.respond", "mock-llm",
              lambda: {"text": "hi", "model": "priced-model",
                       "usage": {"input": 1_000_000, "output": 1_000_000}},
              {"system": "s"})

    env = {"RYA_PRICE_PRICED_MODEL_IN": "3", "RYA_PRICE_PRICED_MODEL_OUT": "15"}
    run = engine.store.get_run(ctx.run["id"])
    run["trace"] = []          # the trace is gone; the ledger is not
    assert run_usage(run, env, store=engine.store)["costUsd"] == 18.0
    assert run_usage(run, env)["costUsd"] is None   # trace-only fallback sees nothing


def test_workspace_usage_aggregates_the_ledger(tmp_path):
    from rya.observability.usage import workspace_usage
    engine = _project(tmp_path)
    for _ in range(3):
        ctx = _ctx(engine)
        ctx._step("llm.chat", "step 0",
                  lambda: {"text": "x", "model": "m", "usage": {"input": 10, "output": 5}},
                  {"system": "s"})
    totals = workspace_usage(engine.store)
    assert totals["calls"] == 3 and totals["inputTokens"] == 30


def _ctx(engine: Engine) -> RuntimeContext:
    event = engine.make_event("message.received", {"email": "a@x.co"})
    run = engine._new_run("event", event)
    return RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                          tools=engine.tools, models=engine.models,
                          project_root=engine.project_root, agent=engine.agent)
