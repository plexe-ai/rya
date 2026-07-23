"""Phase-2 gate for csa-counsellor: CAMS lookups + student workspace tools.

Deterministic, provider-independent — like the A2 secrecy gate in the core
``tests/test_guard.py``, these assert the ported business logic directly rather
than through an LLM eval (the offline mock can't drive multi-tool sequences, and
live evals are non-deterministic). Covers the id-secrecy contract end-to-end:
``cams_lookup_student`` returns the alphanumeric master id and the numeric CAMS
id as SEPARATE fields, and the A2 scrub redacts the master id while preserving
the CAMS id — the exact production bug (CAMS id reported as "KGOr1760073259").

Tool handlers are async; tests wrap them in ``asyncio.run`` (the repo doesn't
depend on pytest-asyncio). Each test loads a FRESH agent module so the in-process
student store the workspace tools mutate never leaks across tests.
"""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_AGENT_PATH = Path(__file__).resolve().parent.parent / "src" / "agent.py"


def _fresh_module():
    spec = importlib.util.spec_from_file_location("csa_agent_p2", _AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def m():
    return _fresh_module()


def run(coro):
    return asyncio.run(coro)


# ---- CAMS lookups ----------------------------------------------------------
def test_lookup_by_numeric_cams_id_returns_master_and_cams_separately(m):
    r = run(m.cams_lookup_student({"camsId": "1472802"}))
    assert r["found"] and r["matchedBy"] == "camsId"
    assert r["masterId"] == "IjmQ1782803306"     # the grouping key...
    assert r["camsIds"] == ["1472802"]           # ...never conflated with the CAMS id
    assert "masterId" not in r["student"]         # student blob carries no raw master id


def test_lookup_by_name(m):
    r = run(m.cams_lookup_student({"name": "Rahul Nair"}))
    assert r["found"] and r["matchedBy"] == "name" and r["camsIds"] == ["1583991"]


def test_lookup_miss_is_graceful(m):
    r = run(m.cams_lookup_student({"name": "Nobody At All"}))
    assert r["found"] is False and "reason" in r


def test_master_id_scrubbed_but_cams_id_preserved_through_lookup(m):
    """The A2 primitive, applied to a real cams_lookup result: master id → token,
    numeric CAMS id and passport untouched. Mirrors utils.test.ts scrubMasterIds."""
    from rya.guard import _compile_secrecy, secrecy_scrub
    policy = {"secrecy": {"enabled": True, "patterns": [
        {"id": "crizac_master_id", "kind": "regex",
         "pattern": r"\b[A-Za-z]{3,8}\d{8,}\b", "replacement": "(id hidden)"}]}}
    scrubbed = secrecy_scrub(run(m.cams_lookup_student({"camsId": "1472802"})),
                             _compile_secrecy(policy))
    blob = json.dumps(scrubbed)
    assert "IjmQ1782803306" not in blob         # master id gone
    assert "(id hidden)" in blob                 # replaced by the safe token
    assert "1472802" in blob                     # numeric CAMS id preserved
    assert "Z1234567" in blob                    # passport (1 letter) preserved


# ---- duplicates + activity -------------------------------------------------
def test_duplicates_present_and_absent_and_skipped(m):
    assert run(m.duplicates_check({"camsId": "IjmQ1782803306"}))["hasDuplicates"] is True
    assert run(m.duplicates_check({"camsId": "YKHw1782723298"}))["hasDuplicates"] is False
    assert run(m.duplicates_check({"camsId": "0000"}))["source"] == "skipped"


def test_activity_timeline(m):
    r = run(m.application_activity({"camsId": "1472802"}))
    assert r["source"] == "local" and len(r["timeline"]) == 3
    assert r["timeline"][0]["kind"] == "status"


# ---- workspace: shortlist / compare / profile ------------------------------
def test_shortlist_add_validates_against_catalogue(m):
    ok = run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs", "fit": "target"}))
    assert ok["ok"] and ok["shortlistSize"] == 1
    assert ok["added"]["universityName"] == "University College London"  # resolved from catalogue
    # an invented id is refused (port of prod's isKnownId guardrail)
    bad = run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": "made-up", "fit": "safe"}))
    assert bad["ok"] is False and "unrecognized" in bad["reason"]
    # dedup
    dup = run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs", "fit": "safe"}))
    assert dup["ok"] is False and dup["reason"] == "Already on shortlist"


def test_shortlist_never_exceeds_cap(m):
    ids = list(m._programme_index().keys())
    added = sum(1 for pid in ids
                if run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": pid, "fit": "target"}))["ok"])
    size = run(m.compare_programmes({"camsId": "IjmQ1782803306"}))["count"]
    assert size == added and size <= m.MAX_SHORTLIST


def test_compare_needs_a_shortlist(m):
    empty = run(m.compare_programmes({"camsId": "YKHw1782723298"}))
    assert empty["ok"] is False
    run(m.shortlist_add({"camsId": "YKHw1782723298", "programmeId": "leeds-msc-ds", "fit": "target"}))
    full = run(m.compare_programmes({"camsId": "YKHw1782723298"}))
    assert full["ok"] and full["count"] == 1
    assert full["universities"] == ["University of Leeds - MSc Data Science and Analytics"]


def test_shortlist_remove(m):
    run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs", "fit": "target"}))
    gone = run(m.shortlist_remove({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs"}))
    assert gone["ok"] and gone["shortlistSize"] == 0
    assert run(m.shortlist_remove({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs"}))["ok"] is False


def test_collect_profile_reports_missing(m):
    r = run(m.collect_profile({"camsId": "IjmQ1782803306"}))
    assert r["ok"] and r["studentName"] == "Ada Kumar"
    assert "Budget cap (USD)" not in r["missing"]   # seeded
    assert "Backlogs?" in r["missing"]              # unset
    assert run(m.collect_profile({"camsId": "nope"}))["ok"] is False


# ---- cards -----------------------------------------------------------------
def test_card_mapping_for_phase2_tools(m):
    dc = run(m.duplicates_check({"camsId": "IjmQ1782803306"}))
    assert m._card_for({"tool": "duplicates_check", "result": dc})[0] == "duplicates"
    # skipped duplicates → no card
    assert m._card_for({"tool": "duplicates_check",
                        "result": run(m.duplicates_check({"camsId": "0"}))}) is None
    pf = run(m.collect_profile({"camsId": "IjmQ1782803306"}))
    assert m._card_for({"tool": "collect_profile", "result": pf})[0] == "profileForm"
    add = run(m.shortlist_add({"camsId": "IjmQ1782803306", "programmeId": "ucl-msc-cs", "fit": "target"}))
    assert m._card_for({"tool": "shortlist_add", "result": add})[0] == "student_refresh"
    cmp = run(m.compare_programmes({"camsId": "IjmQ1782803306"}))
    assert m._card_for({"tool": "compare_programmes", "result": cmp})[0] == "comparison"
