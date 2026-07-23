"""Phase-3 gate for csa-counsellor: lead creation + retry/repair (A1) + camsId
adoption (A5).

Two layers, both deterministic (no LLM):

* **Handler units** load a FRESH agent module (like the Phase-2 gate) and call the
  leaves directly — create_lead's numeric-id minting, passport idempotency, and
  the repair callback's field mapping.
* **Runtime gates** drive ``ctx.tools.call`` against the REAL manifest + handlers
  (copied into a temp dir so no ``.rya`` lands in the repo), so the declarative
  ``retry:`` / ``adopt:`` policy and the ``memory.student_state.camsId`` pins are
  exercised exactly as the governed loop runs them. These are the four
  non-negotiable Phase-3 gates:
    - a timeout-then-success retry NEVER double-creates (passport idempotency);
    - shortlist_add BEFORE create_lead resolves an empty pin and rejects, then
      adopts the key once the lead exists (order-independent);
    - create_lead → shortlist targets the SAME adopted camsId;
    - an unrecognised destination self-heals via @agent.repair and retries.
"""

import asyncio
import importlib.util
import shutil
from pathlib import Path

import pytest

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import RuntimeContext
from rya.store import Store

_CSA = Path(__file__).resolve().parent.parent
_AGENT_PATH = _CSA / "src" / "agent.py"


def run(coro):
    return asyncio.run(coro)


# ---- handler units (fresh module, no engine) -------------------------------
def _fresh_module():
    spec = importlib.util.spec_from_file_location("csa_agent_p3", _AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def m():
    return _fresh_module()


def test_create_lead_mints_numeric_cams_id_and_stores(m):
    before = len(m._STUDENTS)
    r = run(m.create_lead({"name": "New Student", "passport": "X9990001",
                           "destinationCountry": "UK", "courseType": "Postgraduate"}))
    assert r["ok"] and r["created"]
    assert r["camsId"].isdigit()                 # surfaced id is numeric
    assert r["destination"] == "United Kingdom"  # alias resolved
    assert len(m._STUDENTS) == before + 1


def test_create_lead_is_idempotent_by_passport(m):
    a = run(m.create_lead({"name": "Ada Two", "passport": "P1230001",
                          "destinationCountry": "UK", "courseType": "Postgraduate"}))
    n = len(m._STUDENTS)
    b = run(m.create_lead({"name": "Ada Two", "passport": "P1230001",
                          "destinationCountry": "UK", "courseType": "Postgraduate"}))
    assert b["alreadyExists"] and b["reason"] == "passportAlreadyExists"
    assert b["camsId"] == a["camsId"]        # same lead, not a new one
    assert len(m._STUDENTS) == n             # nothing created on the second call


def test_create_lead_raises_recoverable_for_bad_destination(m):
    from rya import RyaRecoverableToolError
    with pytest.raises(RyaRecoverableToolError) as ei:
        run(m.create_lead({"name": "X", "passport": "Q1", "destinationCountry": "Narnia",
                           "courseType": "Postgraduate"}))
    assert ei.value.reason == "invalid_destination"


def test_repair_callback_maps_each_reason(m):
    from rya import RyaRecoverableToolError
    dest = m.repair_create_lead({"destinationCountry": "Untied Kingdon"},
                                RyaRecoverableToolError("invalid_destination"))
    assert dest["destinationCountry"] == "United Kingdom"
    ct = m.repair_create_lead({"courseType": "Postgrad"},
                              RyaRecoverableToolError("invalid_course_type"))
    assert ct["courseType"] == "Postgraduate"
    hs = m.repair_create_lead({"homeState": "Kerela"},  # misspelt Kerala
                              RyaRecoverableToolError("invalid_home_state"))
    assert hs["homeState"] == "Kerala"


def test_bulk_creates_idempotent_and_skips_invalid(m):
    r = run(m.create_leads_bulk({"leads": [
        {"name": "A", "passport": "B0001", "destinationCountry": "UK"},
        {"name": "B", "passport": "B0002", "destinationCountry": "Atlantis"},  # invalid
    ]}))
    assert r["count"] == 1
    kinds = {x["passport"]: x for x in r["results"]}
    assert kinds["B0001"]["created"] and kinds["B0002"]["ok"] is False
    # re-running the valid one is idempotent
    again = run(m.create_leads_bulk({"leads": [{"name": "A", "passport": "B0001",
                                                "destinationCountry": "UK"}]}))
    assert again["results"][0]["alreadyExists"]


# ---- runtime gates (real manifest + handlers via ctx.tools.call) -----------
def _harness(tmp_path):
    proj = tmp_path / "csa"
    shutil.copytree(_CSA, proj, ignore=shutil.ignore_patterns(".rya", "__pycache__", "tests"))
    manifest = load_manifest(proj / "rya.agent.yaml")
    agent = load_agent(manifest, proj)
    store = Store(proj); store.ensure()
    engine = Engine(manifest, agent, store, proj)
    event = engine.make_event("message.received", {})
    run_rec = engine._new_run("event", event)
    ctx = RuntimeContext(store=store, manifest=manifest, run=run_rec, tools=engine.tools,
                         models=engine.models, project_root=proj, agent=agent)
    return ctx, agent, store


def _globals(agent):
    return agent.tool_handler("create_lead").__globals__


def _a_programme_id(agent):
    return next(iter(_globals(agent)["_programme_index"]()))


def _kinds(ctx):
    return [e["kind"] for e in ctx.run["trace"]]


def test_timeout_then_success_never_double_creates(tmp_path):
    ctx, agent, store = _harness(tmp_path)
    g = _globals(agent)
    g["_FAULTS"]["create_lead_timeout_after_write"] = 1  # commit, then lose the response
    before = len(g["_STUDENTS"])

    async def body():
        return await ctx.tools.call("create_lead", {
            "name": "Dup Guard", "passport": "D5550001",
            "destinationCountry": "UK", "courseType": "Postgraduate"})

    res = run(body())
    assert res["ok"] and res.get("alreadyExists")     # the retry hit idempotency
    assert len(g["_STUDENTS"]) - before == 1          # exactly ONE lead, no duplicate
    assert "tool.retry" in _kinds(ctx)                # the transient retry fired


def test_create_lead_then_shortlist_same_key(tmp_path):
    ctx, agent, store = _harness(tmp_path)
    pid = _a_programme_id(agent)

    async def body():
        lead = await ctx.tools.call("create_lead", {
            "name": "Adopt Me", "passport": "A7770001",
            "destinationCountry": "UK", "courseType": "Postgraduate"})
        sl = await ctx.tools.call("shortlist_add", {"programmeId": pid, "fit": "target"})
        cmp = await ctx.tools.call("compare_programmes", {})
        return lead, sl, cmp

    lead, sl, cmp = run(body())
    cams = lead["camsId"]
    assert sl["ok"] and cmp["ok"]
    # adoption wrote the new camsId, and both pinned tools resolved to it
    assert store.load_memory("student_state")["kv"]["camsId"] == cams
    assert cmp["camsId"] == cams and cmp["count"] == 1
    assert "tool.adopt" in _kinds(ctx)


def test_shortlist_before_create_rejects_then_adopts(tmp_path):
    ctx, agent, store = _harness(tmp_path)
    pid = _a_programme_id(agent)

    async def body():
        early = await ctx.tools.call("shortlist_add", {"programmeId": pid, "fit": "target"})
        await ctx.tools.call("create_lead", {
            "name": "Order Free", "passport": "O2220001",
            "destinationCountry": "UK", "courseType": "Postgraduate"})
        late = await ctx.tools.call("shortlist_add", {"programmeId": pid, "fit": "target"})
        return early, late

    early, late = run(body())
    assert early["ok"] is False and "create the lead" in early["reason"].lower()
    assert late["ok"] is True                          # adopted after the lead exists


def test_bad_destination_self_heals_end_to_end(tmp_path):
    ctx, agent, store = _harness(tmp_path)

    async def body():
        return await ctx.tools.call("create_lead", {
            "name": "Typo", "passport": "T3330001",
            "destinationCountry": "Untied Kingdon", "courseType": "Postgraduate"})

    res = run(body())
    assert res["ok"] and res["created"]
    assert res["destination"] == "United Kingdom"      # snapped to the closest valid
    assert "tool.repair" in _kinds(ctx)
