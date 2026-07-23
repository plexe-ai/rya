"""Phase-4 gate for csa-counsellor: the approval-gated writes made real (B5).

Three layers:

* **Handler units** load a FRESH agent module and call each gated leaf directly —
  the local CAMS writes (``cams_update_profile``, ``crizac_update_application``,
  ``submit_scholarship_enquiry``, ``loan_apply_for_student``) and the one real
  outbound (``send_email``, mock channel offline).
* **Dispatcher units** exercise ``_gated_action`` — the pure intent→action map —
  proving it bakes the RESOLVED pinned camsId and ignores a camsId the payload
  sub-object carries (the ``gated_action_uses_pinned_camsId`` contract at the
  request-building layer), and that a CAMS-targeting write needs a pinned student.
* **Runtime gates** drive the REAL engine (``RYA_FORCE_MOCK`` so the loop is
  deterministic — the write is triggered by the handler post-loop, not the model)
  through the full ``run_event`` → ``approve`` lifecycle. The four non-negotiable
  Phase-4 gates:
    - a gated tool is NEVER in the loop (absent from EXPOSED_TOOLS; a direct
      ``ctx.tools.call`` is refused);
    - a loan / application update / email SUSPENDS for a human (``waiting_approval``);
    - the approved action's stored input carries the PINNED camsId, never the id
      the payload tried to redirect it to, and the write lands on that student;
    - a loan with an ungrounded money figure in the composed reply is BLOCKED
      before approval is ever requested (grounding gate).
"""

import asyncio
import importlib.util
import shutil
from pathlib import Path

import pytest

from rya.errors import RyaError
from rya.guard import grounding_check
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.sdk.context import RuntimeContext
from rya.store import Store

_CSA = Path(__file__).resolve().parent.parent
_AGENT_PATH = _CSA / "src" / "agent.py"

# Seeded students (data/students.json): Ada Kumar CAMS 1472802, Rahul Nair 1583991.
ADA = "1472802"
RAHUL = "1583991"


def run(coro):
    return asyncio.run(coro)


# ---- handler + dispatcher units (fresh module, no engine) ------------------
def _fresh_module():
    spec = importlib.util.spec_from_file_location("csa_agent_p4", _AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def m():
    return _fresh_module()


def test_cams_update_profile_writes_local(m):
    r = run(m.cams_update_profile({"camsId": ADA, "updates": {"preferredCity": "London",
                                                              "backlogs": 0}}))
    assert r["ok"] and r["camsId"] == ADA          # numeric CAMS id surfaced
    assert r["values"]["preferredCity"] == "London" and r["values"]["backlogs"] == 0


def test_crizac_update_application_sets_status(m):
    r = run(m.crizac_update_application({"camsId": ADA, "status": "Visa Filed",
                                        "comment": "docs verified"}))
    assert r["ok"] and r["status"] == "Visa Filed" and r["camsId"] == ADA


def test_submit_scholarship_enquiry_records(m):
    r = run(m.submit_scholarship_enquiry({"camsId": ADA, "scholarship": "Chevening",
                                         "note": "strong profile"}))
    assert r["ok"] and r["scholarship"] == "Chevening" and r["enquiryCount"] == 1


def test_loan_apply_records(m):
    r = run(m.loan_apply_for_student({"camsId": ADA, "amount": 22000, "currency": "GBP"}))
    assert r["ok"] and r["amount"] == 22000 and r["status"] == "submitted"


def test_send_email_delivers_via_channel(m):
    r = run(m.send_email({"to": "ada@student.test", "subject": "Hi", "body": "your shortlist"}))
    assert r["ok"] and r["delivered"] and r["provider"] == "mock"   # offline outbox
    assert run(m.send_email({"subject": "x"}))["ok"] is False        # no recipient


def test_gated_action_bakes_pinned_cams_and_ignores_payload(m):
    # The counsellor payload tries to redirect the write to another student; the
    # dispatcher must ignore it and use the session's pinned/adopted camsId.
    g = m._gated_action({"updateProfile": {"camsId": "999999", "preferredCity": "Leeds"}},
                        "reply", ADA)
    assert g["tool"] == "cams_update_profile"
    assert g["input"]["camsId"] == ADA
    assert "camsId" not in g["input"]["updates"]

    upd = m._gated_action({"updateApplication": {"camsId": "999999", "status": "Visa Filed"}},
                          "reply", ADA)
    assert upd["input"]["camsId"] == ADA and upd["input"]["status"] == "Visa Filed"


def test_gated_action_requires_pinned_student_for_cams_writes(m):
    # No active student → nothing safe to write, so no approval is requested…
    assert m._gated_action({"updateProfile": {"x": 1}}, "r", None) is None
    assert m._gated_action({"loanApply": True, "loanAmount": 10}, "r", None) is None
    # …but an email is not student-keyed and still routes.
    assert m._gated_action({"sendEmail": True, "studentEmail": "a@b.co"}, "r", None)["tool"] \
        == "send_email"


def test_loan_action_flagged_for_grounding(m):
    g = m._gated_action({"loanApply": True, "loanAmount": 22000}, "r", ADA)
    assert g["tool"] == "loan_apply_for_student" and g["ground"] is True
    # only the loan is a money write; the others skip the grounding gate
    assert m._gated_action({"sendEmail": True}, "r", ADA)["ground"] is False


def test_grounding_blocks_ungrounded_loan_figure():
    # The grounding gate the loan flow runs on the composed reply: a £ figure that
    # no tool output of the run produced is a violation (the model invented it).
    bad = grounding_check("Approved a loan of £22,000 for the student.", tool_outputs=[])
    assert bad["ok"] is False and 22000.0 in bad["violations"]
    # …grounded once a tool surfaced that figure.
    good = grounding_check("Approved a loan of £22,000.",
                           tool_outputs=[{"annualTuition": 22000}])
    assert good["ok"] is True


# ---- runtime gates (real engine, forced-mock loop, full approve lifecycle) --
def _engine(tmp_path):
    proj = tmp_path / "csa"
    shutil.copytree(_CSA, proj, ignore=shutil.ignore_patterns(".rya", "__pycache__", "tests"))
    manifest = load_manifest(proj / "rya.agent.yaml")
    agent = load_agent(manifest, proj)
    store = Store(proj); store.ensure()
    return Engine(manifest, agent, store, proj), agent, store


def _agent_globals(agent):
    return agent.tool_handler("loan_apply_for_student").__globals__


def test_gated_tool_never_in_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_FORCE_MOCK", "1")
    engine, agent, store = _engine(tmp_path)
    gated = ["cams_update_profile", "crizac_update_application", "submit_scholarship_enquiry",
             "loan_apply_for_student", "send_email"]
    # Not exposed to the model loop…
    exposed = set(_agent_globals(agent)["EXPOSED_TOOLS"])
    assert exposed.isdisjoint(gated)
    # …and a direct call is refused by the runtime (approval_required).
    run_rec = engine._new_run("event", engine.make_event("message.received", {}))
    ctx = RuntimeContext(store=store, manifest=engine.manifest, run=run_rec, tools=engine.tools,
                         models=engine.models, project_root=tmp_path / "csa", agent=agent)
    for tool in gated:
        with pytest.raises(RyaError) as ei:
            run(ctx.tools.call(tool, {"camsId": ADA}))
        assert ei.value.code == "E_TOOL_PERMISSION_DENIED"


def test_loan_requires_human_then_executes(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_FORCE_MOCK", "1")
    engine, agent, store = _engine(tmp_path)
    run_rec = engine.run_event("message.received", {
        "email": "c@csa.test", "body": "apply for a loan for this student",
        "camsId": ADA, "loanApply": True, "loanAmount": 22000})
    assert run_rec["status"] == "waiting_approval"       # suspended for a human

    apr_id = run_rec["pendingApproval"]
    appr = store.get_approval(apr_id)
    assert appr["action"]["tool"] == "loan_apply_for_student"
    assert appr["action"]["input"]["camsId"] == ADA      # pinned student baked in

    resumed = engine.approve(apr_id)
    assert resumed["status"] == "completed"
    assert store.get_approval(apr_id)["actionResult"]["status"] == "submitted"
    # the write landed on the pinned student
    loan = _agent_globals(agent)["_STUDENTS"]["IjmQ1782803306"].get("loan")
    assert loan and loan["amount"] == 22000


def test_update_application_requires_human(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_FORCE_MOCK", "1")
    engine, agent, store = _engine(tmp_path)
    run_rec = engine.run_event("message.received", {
        "email": "c@csa.test", "body": "mark the visa as filed", "camsId": ADA,
        "updateApplication": {"status": "Visa Filed"}})
    assert run_rec["status"] == "waiting_approval"
    apr_id = run_rec["pendingApproval"]
    assert store.get_approval(apr_id)["action"]["tool"] == "crizac_update_application"
    resumed = engine.approve(apr_id)
    assert resumed["status"] == "completed"
    app = _agent_globals(agent)["_STUDENTS"]["IjmQ1782803306"]["applications"][0]
    assert app["status"] == "Visa Filed"


def test_gated_action_uses_pinned_camsId(tmp_path, monkeypatch):
    # The turn pins student Ada (camsId), but the write payload tries to redirect
    # to a bogus id. The approved action must target the PINNED student.
    monkeypatch.setenv("RYA_FORCE_MOCK", "1")
    engine, agent, store = _engine(tmp_path)
    run_rec = engine.run_event("message.received", {
        "email": "c@csa.test", "body": "save these profile fields", "camsId": ADA,
        "updateProfile": {"camsId": "999999", "preferredCity": "London"}})
    assert run_rec["status"] == "waiting_approval"
    apr_id = run_rec["pendingApproval"]
    action = store.get_approval(apr_id)["action"]
    assert action["input"]["camsId"] == ADA                  # NOT 999999
    resumed = engine.approve(apr_id)
    assert resumed["status"] == "completed"
    # the write hit Ada's record, never the redirect id
    ada = _agent_globals(agent)["_STUDENTS"]["IjmQ1782803306"]
    assert ada["profile"]["preferredCity"] == "London"


def test_outbound_email_requires_human_and_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_FORCE_MOCK", "1")
    engine, agent, store = _engine(tmp_path)
    run_rec = engine.run_event("message.received", {
        "email": "c@csa.test", "body": "email the student the shortlist",
        "sendEmail": True, "studentEmail": "ada@student.test"})
    assert run_rec["status"] == "waiting_approval"
    apr_id = run_rec["pendingApproval"]
    assert store.get_approval(apr_id)["action"]["tool"] == "send_email"
    resumed = engine.approve(apr_id)
    assert resumed["status"] == "completed"
    assert store.get_approval(apr_id)["actionResult"]["delivered"] is True
