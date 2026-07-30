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


# --------------------------------------------------------------------------- #
# the deployment hierarchy the console renders: agent -> environment -> version
# -> runs (PLATFORM_DESIGN §11 item 12). These assert the DATA the console pages
# read, not their markup: a string check on the HTML would pass while every panel
# rendered "unavailable".
# --------------------------------------------------------------------------- #
def _deployed(root, *, env="prod", actor="ada@example.com"):
    """Record a version, promote it, and start a run pinned to it — the way
    `rya deploy` + a version-pinned worker do (D12, §9)."""
    from rya.bundles import build_bundle
    from rya.deployments import create_version, promote

    store = Store(root)
    store.ensure()
    version = create_version(store, agent="console-agent", bundle=build_bundle(root), actor=actor)
    promote(store, environment=env, agent="console-agent", version_id=version["id"], actor=actor)
    manifest = load_manifest(root / "rya.agent.yaml")
    engine = Engine(manifest, load_agent(manifest, root), store, root,
                    version=version, environment=env)
    run = engine.run_event("message.received", {"email": "ada@example.com"})
    return store, version, run


def test_console_hierarchy_environment_to_version_to_runs(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    _store, version, run = _deployed(root)

    # agent -> environments
    envs = c.get("/agents/_/environments").json()["environments"]
    assert [e["name"] for e in envs] == ["prod"]

    # environment -> the version it points at, and who promoted it (§12 risk 7)
    env = c.get("/agents/_/environments/prod").json()
    assert env["currentVersionId"] == version["id"]
    assert env["currentVersion"]["bundleHash"] == version["bundleHash"]
    assert env["actor"] == "ada@example.com" and env["updatedAt"]
    assert env["pinnedRuns"] == {}  # nothing older is being retained yet

    # ...and the promote/rollback audit trail behind that pointer
    hist = c.get("/agents/_/environments/prod/history").json()["history"]
    assert hist[0]["versionId"] == version["id"] and hist[0]["current"] is True
    assert hist[0]["actor"] == "ada@example.com"

    # version -> identity + state (the version page's spec cards)
    v = c.get(f"/versions/{version['id']}").json()
    assert v["bundleHash"] == version["bundleHash"] and v["state"] == "active"
    assert v["sdkVersion"] and v["entrypoint"] and v["createdBy"] == "ada@example.com"

    # version -> runs, and which of them still hold the version open
    body = c.get(f"/versions/{version['id']}/runs").json()
    assert [r["id"] for r in body["runs"]] == [run["id"]]
    assert body["runs"][0]["environment"] == "prod" and body["runs"][0]["pinned"] is True
    assert body["count"] == 1 and body["pinnedCount"] == 1
    assert [r["id"] for r in c.get(f"/versions/{version['id']}/pinned-runs").json()["runs"]] == [run["id"]]


def test_version_runs_keeps_terminal_runs_that_pinned_runs_drops(tmp_path, monkeypatch):
    # The two routes answer different questions: `pinned-runs` is "what blocks a
    # retire" (§6) and excludes terminal runs; the console's version page needs
    # the finished ones too.
    c, root = _client(tmp_path, monkeypatch)
    _store, version, run = _deployed(root)
    approval = c.get("/approvals").json()["approvals"][0]
    assert c.post(f"/approvals/{approval['id']}/approve").status_code == 200
    assert c.get(f"/runs/{run['id']}").json()["status"] == "completed"

    body = c.get(f"/versions/{version['id']}/runs").json()
    assert [r["id"] for r in body["runs"]] == [run["id"]]
    assert body["runs"][0]["pinned"] is False and body["pinnedCount"] == 0
    assert c.get(f"/versions/{version['id']}/pinned-runs").json()["count"] == 0
    assert c.get("/versions/ver_nope/runs").status_code == 404


def test_environment_page_shows_retained_older_versions(tmp_path, monkeypatch):
    # §9's drain step, which is the least obvious part of the model: promoting v2
    # does not free v1 while a run is still pinned to it (D12).
    c, root = _client(tmp_path, monkeypatch)
    store, v1, run = _deployed(root)

    (root / "extra.py").write_text("# a second bundle, therefore a second hash\n")
    from rya.bundles import build_bundle
    from rya.deployments import create_version, promote
    v2 = create_version(store, agent="console-agent", bundle=build_bundle(root), actor="bob@example.com")
    assert v2["bundleHash"] != v1["bundleHash"]
    promote(store, environment="prod", agent="console-agent", version_id=v2["id"], actor="bob@example.com")

    env = c.get("/agents/_/environments/prod").json()
    assert env["currentVersionId"] == v2["id"] and env["actor"] == "bob@example.com"
    assert env["pinnedRuns"] == {v1["id"]: 1}      # v1 is retained, and the page says why
    assert env["historyDepth"] == 1
    assert [h["versionId"] for h in c.get("/agents/_/environments/prod/history").json()["history"]] \
        == [v2["id"], v1["id"]]
    # retiring the retained version fails closed while that run is live (§6)
    assert c.post(f"/versions/{v1['id']}/retire").status_code == 409

    # the versions list carries both, with the pointer resolvable back to prod
    versions = c.get("/agents/_/versions").json()["versions"]
    assert {v["id"] for v in versions} == {v1["id"], v2["id"]}
    assert all(v["state"] == "active" for v in versions)
    assert c.get("/agents/_/versions", params={"state": "retired"}).json()["versions"] == []


def test_version_page_shows_gate_evidence(tmp_path, monkeypatch):
    c, root = _client(tmp_path, monkeypatch)
    store, version, _run = _deployed(root, env="staging")
    from rya.gates import attest_evals, attest_readiness

    attest_readiness(store, version, {"ready": True, "summary": {"blocks": 0, "warnings": 1}},
                     actor="ci@example.com")
    attest_evals(store, version, {"ok": True, "total": 3, "passed": 3, "score": 1.0,
                                  "hasEvals": True, "results": []}, actor="ci@example.com")

    att = c.get(f"/versions/{version['id']}/attestations").json()
    assert att["count"] == 2
    assert [a["kind"] for a in att["attestations"]] == ["readiness", "evals"]
    assert all(a["ok"] and a["actor"] == "ci@example.com" for a in att["attestations"])
    assert c.get("/versions/ver_nope/attestations").status_code == 404

    # the gate panel: an unconfigured environment is "open", not broken
    gates = {g["environment"]: g for g in c.get("/gate").json()["gates"]}
    assert gates["staging"]["enforced"] is False
    assert c.put("/gate", json={"environments": {"staging": {"requireEvals": True}}}).status_code == 200
    assert c.get("/gate?env=staging").json()["gates"][0]["enforced"] is True
    check = c.get("/gate/check", params={"env": "staging"}).json()
    assert check["versionId"] == version["id"] and check["allowed"] is True
    assert [c_["check"] for c_ in check["checks"]] == ["evals"]


def test_deployment_panels_are_calm_on_a_fresh_install(tmp_path, monkeypatch):
    # The first thing a new user sees. Nothing is deployed, nothing is running,
    # and every panel must still answer with a shape the console can render.
    c, _root = _client(tmp_path, monkeypatch)
    assert c.get("/agents/_/environments").json() == {"environments": []}
    assert c.get("/agents/_/versions").json() == {"versions": []}
    # scale-to-zero (§6) is the designed steady state, not an error
    assert c.get("/workers").status_code == 200
    assert c.get("/workers").json() == {"workers": []}
    # an environment that was never promoted into is a clean 404 with a stable code
    missing = c.get("/agents/_/environments/prod")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "E_ENVIRONMENT_NOT_FOUND"
    assert c.get("/gate/check", params={"env": "prod"}).status_code == 404
    assert c.get("/gate").json()["default"]["enforced"] is False
    # quota + usage answer even with no policy and no meter records
    q = c.get("/quotas").json()
    assert q["quota"]["enforced"] is False and q["admission"] == []
    assert q["usage"]["runsToday"] == 0 and q["usage"]["workers"] == 0
    assert c.get("/usage").json()["usage"]["calls"] == 0


def test_workers_view_reports_a_live_process(tmp_path, monkeypatch):
    # §6: the console has to show which process serves which version, its cold
    # start and its heartbeat.
    c, root = _client(tmp_path, monkeypatch)
    store, version, _run = _deployed(root)
    store.worker_register({"id": "wrk_console1", "agent": "console-agent",
                           "versionId": version["id"], "bundleHash": version["bundleHash"],
                           "handlers": ["event"], "pid": 4242, "host": "node-a",
                           "coldStartMs": 312, "sdkVersion": version["sdkVersion"]})
    workers = c.get("/workers").json()["workers"]
    assert [w["id"] for w in workers] == ["wrk_console1"]
    assert workers[0]["versionId"] == version["id"] and workers[0]["coldStartMs"] == 312
    assert workers[0]["status"] == "alive" and workers[0]["lastHeartbeatAt"]
    # the version page filters the fleet down to this version
    assert c.get("/workers", params={"version_id": version["id"]}).json()["workers"]
    assert c.get("/workers", params={"version_id": "ver_other"}).json()["workers"] == []
    # ...and quota usage counts it (§11.12)
    assert c.get("/quotas").json()["usage"]["workers"] == 1


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
