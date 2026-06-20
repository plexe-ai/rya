"""Exercise the MCP operations layer (no transport needed)."""

from rya.mcp import ops


def test_mcp_full_flow(tmp_path):
    # create -> validate -> trigger -> approve, all via the MCP ops.
    created = ops.create_agent("mcp-agent", str(tmp_path))
    assert created["ok"] is True
    proj = created["path"]

    valid = ops.validate_manifest(proj)
    assert valid["ok"] and valid["ready"] is True
    assert "email.send" in valid["tools"]

    run = ops.trigger_event("message.received", {"email": "ada@example.com"}, project_dir=proj)
    assert run["ok"] and run["status"] == "waiting_approval"
    assert "guidance" in run
    approval_id = run["pendingApproval"]

    pend = ops.list_approvals("pending", proj)
    assert pend["count"] == 1
    assert pend["approvals"][0]["id"] == approval_id

    done = ops.approve_action(approval_id, proj)
    assert done["ok"] and done["runStatus"] == "completed"

    runs = ops.list_runs(project_dir=proj)
    assert runs["count"] == 1


def test_mcp_provision(tmp_path):
    created = ops.create_agent("mcp-prov", str(tmp_path))
    rep = ops.provision(created["path"], target="local")
    assert rep["ok"] is True
    keys = {c["key"] for c in rep["components"]}
    assert {"database", "auth", "guardrails", "realtime", "jobs", "scale"} <= keys
    assert rep["connection"]["websocket"].endswith("/ws")


def test_mcp_connect_vaults_secret(tmp_path):
    created = ops.create_agent("mcp-conn", str(tmp_path))
    proj = created["path"]
    r = ops.connect("github", scopes=["issues:write"], token="ghtok_secret_xyz", project_dir=proj)
    assert r["ok"] and "secret" not in r["connection"] and r["connection"]["secretSet"] is True
    assert r["connection"]["encrypted"] is True  # encrypted at rest
    listed = ops.list_connections(proj)
    assert listed["count"] == 1 and "secret" not in listed["connections"][0]
    # reseal is idempotent once everything is already encrypted
    re = ops.reseal_connections(proj)
    assert re["ok"] and re["resealed"] == 0 and re["alreadyEncrypted"] == 1


def test_mcp_returns_structured_error(tmp_path):
    created = ops.create_agent("err-agent", str(tmp_path))
    res = ops.get_run_trace("run_nope", created["path"])
    assert res["ok"] is False
    assert res["error"]["code"] == "E_RUN_NOT_FOUND"
    assert res["error"]["hint"]


def test_mcp_register_tool_rejects_duplicate(tmp_path):
    created = ops.create_agent("dup-agent", str(tmp_path))
    proj = created["path"]
    ok = ops.register_tool("billing.refund", "approval_required", proj)
    assert ok["ok"] and ok["registered"] == "billing.refund"
    dup = ops.register_tool("billing.refund", "allowed", proj)
    assert dup["ok"] is False and dup["error"]["code"] == "E_VALIDATION"


def test_skill_modules_split():
    from rya.skills import SKILLS
    assert set(SKILLS) == {"rya", "rya-ops"}
    assert SKILLS["rya"].startswith("---") and "name: rya" in SKILLS["rya"]
    assert "name: rya-ops" in SKILLS["rya-ops"]
    # progressive disclosure: ops skill leads agents to call context first
    assert "rya context" in SKILLS["rya-ops"]


def test_mcp_context_snapshot(tmp_path):
    created = ops.create_agent("ctx-agent", str(tmp_path))
    proj = created["path"]
    ops.trigger_event("message.received", {"email": "ada@example.com"}, project_dir=proj)
    snap = ops.context(proj)
    assert snap["ok"] is True
    assert snap["agent"]["name"] == "ctx-agent"
    assert any(t["id"] == "email.send" and t["permission"] == "approval_required" for t in snap["tools"])
    assert snap["handlers"]["event"] is True
    assert snap["approvals"]["pendingCount"] == 1
    assert snap["runs"]["total"] == 1
    assert snap["rules"] and snap["next"]  # rules + suggested next actions present
