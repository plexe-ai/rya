"""The built-in web console: aggregate endpoint, served page, and auth gating."""

import pytest
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.snapshot import build_console
from rya.store import Store


def _client(tmp_path, monkeypatch):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "console-agent", template="demo")
    return TestClient(build_app(tmp_path)), tmp_path


def test_build_console_shape(tmp_path):
    scaffold.write_project(tmp_path, "agg", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    engine.run_event("message.received", {"email": "ada@x.com"})  # pauses for approval

    c = build_console(manifest, engine.store, agent, tmp_path)
    assert c["agent"]["name"] == "agg"
    assert c["stats"]["runs"] == 1
    assert c["stats"]["approvalsPending"] == 1
    assert any(t["id"] == "email.send" and t["permission"] == "approval_required" for t in c["tools"])
    assert c["tools"][0]["calls"] >= 0
    assert c["manifestYaml"].startswith("name:")
    assert len(c["approvals"]) == 1


def test_console_page_is_served(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Rya" in r.text and "rya context" in r.text
    assert c.get("/console.html").status_code == 200


def test_console_json_is_live(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    c.post("/inbound", json={"email": "ada@example.com"})  # create a real run
    body = c.get("/console").json()
    assert body["ok"] is True
    assert body["stats"]["runs"] == 1
    assert body["stats"]["approvalsPending"] == 1
    assert body["runs"][0]["status"] == "waiting_approval"
    # infra basics computed from the running process
    inf = body["infra"]
    assert inf["version"] and inf["python"] and inf["pid"]
    assert inf["store"]["backend"] == "file"
    assert inf["auth"]["mode"] == "open (local dev)"
    assert "controlPlane" in inf["planes"]


def test_console_json_respects_token(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("RYA_TOKEN", "sek")
    # page stays public so the browser can load it...
    assert c.get("/").status_code == 200
    # ...but the data endpoint requires the token.
    assert c.get("/console").status_code == 401
    assert c.get("/console", headers={"Authorization": "Bearer sek"}).status_code == 200


def test_console_and_api_expose_connections_without_secret(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    from rya.store import Store
    store = Store(root); store.ensure()
    store.create_connection("github", ["issues:write"], secret="ghtok_secret_abc", label="gh")
    body = c.get("/console").json()
    assert body["connections"][0]["provider"] == "github"
    assert "secret" not in body["connections"][0] and body["connections"][0]["secretSet"] is True
    api = c.get("/connections").json()["connections"]
    assert api[0]["provider"] == "github" and "secret" not in api[0]


def test_cors_is_locked_down_by_default(tmp_path, monkeypatch):
    # No wildcard CORS on the control plane: same-origin only unless explicitly opted in.
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/console", headers={"Origin": "http://evil.example"})
    assert r.headers.get("access-control-allow-origin") in (None, "")
    # security headers are present on every response
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_cors_allowlist_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_CORS_ORIGINS", "http://localhost:4321")
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/console", headers={"Origin": "http://localhost:4321"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:4321"


def test_console_security_headers_and_assets(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    page = c.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers.get("content-security-policy", "")
    assert page.headers.get("x-frame-options") == "DENY"
    # the icon library is served locally (no CDN dependency) and the favicon resolves
    assert c.get("/lucide.min.js").status_code == 200
    assert "javascript" in c.get("/lucide.min.js").headers.get("content-type", "")
    assert c.get("/favicon.ico").status_code == 200


def test_stats_report_models_actually_used(tmp_path):
    """The console must not call real Bedrock runs 'mock LLM': stats carry the
    model ids seen in traces so the UI can label the LLM truthfully."""
    scaffold.write_project(tmp_path, "agg2", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    engine.run_event("message.received", {"email": "ada@x.com"})

    run = engine.store.list_runs(manifest.name)[0]
    for ev in run["trace"]:
        if ev["kind"] == "llm.respond":
            ev["data"]["result"]["model"] = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    engine.store.save_run(run)

    c = build_console(manifest, engine.store, agent, tmp_path)
    assert c["stats"]["models"] == ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]


def test_governance_surface_in_console(tmp_path):
    """The control-plane governance section: policy hash, real enforcement
    flags, kill-switch state, and violations aggregated from run traces."""
    scaffold.write_project(tmp_path, "gov", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    engine.run_event("message.received", {"email": "ada@x.com"})

    # plant a grounding block + an egress block in a trace
    run = engine.store.list_runs(manifest.name)[0]
    run["trace"].append({"seq": 99, "ts": "2026-07-25T10:00:00Z",
                         "kind": "guard.grounding_blocked", "label": "email",
                         "data": {"violations": [4200000.0]}})
    run["trace"].append({"seq": 100, "ts": "2026-07-25T10:01:00Z",
                         "kind": "run.failed", "label": "E_EGRESS_BLOCKED",
                         "data": {"message": "https://evil.example denied by rule"}})
    engine.store.save_run(run)

    c = build_console(manifest, engine.store, agent, tmp_path)
    g = c["governance"]
    assert len(g["policy"]["hash"]) == 16
    assert set(g["enforcement"]) == {"egressGuard", "groundingGate", "approverIdentity",
                                     "perUserIdentity", "multiTenantRls", "secretsSealed"}
    kinds = {v["kind"] for v in g["violations"]}
    assert kinds == {"guard.grounding_blocked", "E_EGRESS_BLOCKED"}
    assert g["switches"]["active"] == []

    # same policy -> same hash; changed enforcement -> different hash
    import os
    c2 = build_console(manifest, engine.store, agent, tmp_path)
    assert c2["governance"]["policy"]["hash"] == g["policy"]["hash"]
    os.environ["RYA_REQUIRE_APPROVER_IDENTITY"] = "1"
    try:
        c3 = build_console(manifest, engine.store, agent, tmp_path)
        assert c3["governance"]["policy"]["hash"] != g["policy"]["hash"]
    finally:
        del os.environ["RYA_REQUIRE_APPROVER_IDENTITY"]


def test_branding_from_project_env(tmp_path):
    scaffold.write_project(tmp_path, "brand", template="demo")
    (tmp_path / ".env").write_text("RYA_BRAND_NAME=Crizac\nRYA_BRAND_TAGLINE=Making education easy\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    c = build_console(manifest, engine.store, agent, tmp_path)
    assert c["branding"] == {"name": "Crizac", "tagline": "Making education easy"}


def test_no_branding_by_default(tmp_path):
    scaffold.write_project(tmp_path, "plain", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    assert build_console(manifest, engine.store, agent, tmp_path)["branding"] is None
