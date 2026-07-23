"""Phase-5 gate for csa-counsellor: prediction + drafting + the discovery tail,
and the A3 live-connection wiring (login-mint + the manifest's live `url:` switch).

* **Tail-leaf units** load a FRESH agent module and call each new leaf directly:
  the discovery tail (programme_detail, university_search/_recommendations,
  scholarship_search + dedup, crizac_filters, visa_requirements), offer_prediction
  (graceful when Plexe is unconfigured — the `offer_prediction_graceful_when_down`
  contract), and draft_sop/draft_lor (grounded context, no fabrication).
* **Card unit** proves scholarship_search maps to a `scholarships` frame.
* **A3 wiring** proves the Crizac tools declare a live connection (provider +
  require_user + url), the gated Crizac writes stay handler-run (no url), and the
  login-mint upserts a per-counsellor bearer.

The E_CONNECTION_EXPIRED → needs_reconnect gate lives in the core suite
(tests/test_connection_a3.py) — no offline provider can synthesise a 401.
"""

import asyncio
import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import RuntimeContext
from rya.store import Store

_CSA = Path(__file__).resolve().parent.parent
_AGENT_PATH = _CSA / "src" / "agent.py"

ADA = "1472802"


def run(coro):
    return asyncio.run(coro)


def _fresh_module():
    spec = importlib.util.spec_from_file_location("csa_agent_p5", _AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def m():
    return _fresh_module()


# ---- discovery tail units --------------------------------------------------
def test_programme_detail_found_and_missing(m):
    idx = m._programme_index()
    pid = next(iter(idx))
    r = run(m.programme_detail({"programmeId": pid}))
    assert r["found"] and r["programme"]["programmeId"] == pid
    assert run(m.programme_detail({"programmeId": "nope-999"}))["found"] is False


def test_university_search_matches_and_falls_back(m):
    hit = run(m.university_search({"query": "computer science United Kingdom"}))
    assert hit["source"] == "local" and hit["total"] >= 1
    assert all("name" in u and "country" in u for u in hit["universities"])
    # a query that matches nothing falls back to the full list (never empty)
    assert run(m.university_search({"query": "zzzzz nothing"}))["total"] >= 1


def test_university_recommendations_filters_by_country_and_budget(m):
    uk = run(m.university_recommendations({"universityCountry": "UK"}))
    assert uk["total"] >= 1 and all(u["country"] == "United Kingdom" for u in uk["universities"])
    cheap = run(m.university_recommendations({"tuitionFeeMax": 1}))
    assert cheap["total"] == 0  # nothing that cheap in the catalogue


def test_scholarship_search_filters_and_dedups(m):
    uk = run(m.scholarship_search({"country": "United Kingdom"}))
    assert uk["count"] >= 1 and all(s["country"] == "United Kingdom" for s in uk["scholarships"])
    names = [s["name"] for s in uk["scholarships"]]
    assert len(names) == len(set(names))  # deduped
    # an unmatched country falls back to the full list rather than empty
    assert run(m.scholarship_search({"country": "Narnia"}))["count"] >= 1


def test_crizac_filters_each_kind(m):
    assert {o["name"] for o in run(m.crizac_filters({"kind": "destination-countries"}))["options"]} \
        >= {"United Kingdom", "United States"}
    assert run(m.crizac_filters({"kind": "home-countries"}))["count"] >= 1
    assert run(m.crizac_filters({"kind": "intakes"}))["count"] >= 1
    assert run(m.crizac_filters({"kind": "course-types"}))["count"] >= 1
    assert run(m.crizac_filters({"kind": "bogus"}))["ok"] is False


def test_visa_requirements_found_and_missing(m):
    uk = run(m.visa_requirements({"country": "UK"}))
    assert uk["found"] and uk["country"] == "United Kingdom" and uk["officialSource"]
    miss = run(m.visa_requirements({"country": "Narnia"}))
    assert miss["found"] is False and miss["availableCountries"]


def test_offer_prediction_graceful_when_unconfigured(m, monkeypatch):
    monkeypatch.delenv("PLEXE_API_KEY", raising=False)
    r = run(m.offer_prediction({"application": {"english_score": 7.0, "course_cost": 38000}}))
    assert r["ok"] is False and "Plexe" in r["reason"]   # graceful, not a raise


def test_draft_sop_and_lor_return_grounded_context(m):
    sop = run(m.draft_sop({"camsId": ADA, "workExperience": "2y SWE"}))
    assert sop["ok"] and sop["source"] == "context" and sop["context"]["name"]
    lor = run(m.draft_lor({"camsId": ADA, "recommenderName": "Dr X"}))
    assert lor["ok"] and lor["recommender"]["name"] == "Dr X" and lor["context"]["name"]


def test_scholarship_search_emits_card(m):
    result = run(m.scholarship_search({"country": "United Kingdom"}))
    card = m._card_for({"tool": "scholarship_search", "result": result})
    assert card and card[0] == "scholarships" and card[1]["scholarships"]


# ---- A3 live-connection wiring ---------------------------------------------
def test_crizac_tools_declare_live_connection():
    manifest = load_manifest(_CSA / "rya.agent.yaml")
    tools = {t.id: t for t in manifest.tools}
    # read/create CRM tools: provider + require_user + a live url (handler wins offline)
    for tid in ["cams_lookup_student", "duplicates_check", "application_activity",
                "course_catalogue", "programme_detail", "crizac_filters",
                "create_lead", "create_leads_bulk"]:
        t = tools[tid]
        assert t.provider == "crizac" and t.require_user is True and t.url, tid
    # gated Crizac writes: provider for governance, but NO url (approved action runs
    # the local handler via engine._execute_action, which would take a url first).
    for tid in ["crizac_update_application", "submit_scholarship_enquiry", "cams_update_profile"]:
        t = tools[tid]
        assert t.provider == "crizac" and not t.url, tid


def test_extract_crizac_token_order(m):
    assert m._extract_crizac_token({"data": {"token": "T"}}) == "T"
    assert m._extract_crizac_token({"data": {"accessToken": "A"}}) == "A"
    assert m._extract_crizac_token({"token": "X"}) == "X"
    assert m._extract_crizac_token({"accessToken": "Y"}) == "Y"
    assert m._extract_crizac_token({"nope": 1}) is None


def _engine(tmp_path):
    proj = tmp_path / "csa"
    shutil.copytree(_CSA, proj, ignore=shutil.ignore_patterns(".rya", "__pycache__", "tests"))
    manifest = load_manifest(proj / "rya.agent.yaml")
    agent = load_agent(manifest, proj)
    store = Store(proj); store.ensure()
    return Engine(manifest, agent, store, proj), agent, store, proj


def test_login_mint_upserts_per_counsellor_bearer(tmp_path, monkeypatch):
    engine, agent, store, proj = _engine(tmp_path)
    glb = agent.tool_handler("create_lead").__globals__
    # stub the network login (the real POST would be blocked by the deny-by-default
    # Action Guard, and we're testing the upsert wiring, not urllib).
    glb["_crizac_login"] = lambda base, basic, email, pw, tenant="1": (
        "CRZ-TOKEN", {"agentId": "A1", "counsellorNumber": "C9"})
    monkeypatch.setenv("CRIZAC_BASE_URL", "https://crizac-api.example")
    monkeypatch.setenv("CRIZAC_BASIC_AUTH", "Basic test")
    monkeypatch.setenv("CRIZAC_TENANT_ID", "1")

    run_rec = engine._new_run("event", engine.make_event("message.received", {}))
    ctx = RuntimeContext(store=store, manifest=engine.manifest, run=run_rec, tools=engine.tools,
                         models=engine.models, project_root=proj, agent=agent)
    run(glb["_maybe_mint_crizac"](ctx, {"crizacLogin": {"email": "c@csa.test", "password": "pw"}}))

    conn = store.get_connection("crizac")
    assert conn and conn["secret"] == "CRZ-TOKEN"
    assert "crm:write" in conn["scopes"]


def test_login_mint_skips_when_unconfigured(tmp_path, monkeypatch):
    engine, agent, store, proj = _engine(tmp_path)
    for k in ("CRIZAC_BASE_URL", "CRIZAC_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    glb = agent.tool_handler("create_lead").__globals__
    run_rec = engine._new_run("event", engine.make_event("message.received", {}))
    ctx = RuntimeContext(store=store, manifest=engine.manifest, run=run_rec, tools=engine.tools,
                         models=engine.models, project_root=proj, agent=agent)
    run(glb["_maybe_mint_crizac"](ctx, {"crizacLogin": {"email": "c@csa.test", "password": "pw"}}))
    assert store.get_connection("crizac") is None   # nothing minted, no crash
