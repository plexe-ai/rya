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


def test_a_pending_approval_carries_everything_needed_to_decide(tmp_path):
    """An approval is the only irreversible human gate, so the snapshot must ship the
    case for it — the body, the tool, and the ARGUMENTS the operator is consenting to.

    All three were already here; the console rendered none of them and drew a mail icon
    instead (audit §4.4). This pins the payload so a future trim of `build_console`
    cannot quietly take the operator's evidence away again.
    """
    scaffold.write_project(tmp_path, "agg", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
    engine.run_event("message.received", {"email": "ada@x.com"})

    a = build_console(manifest, engine.store, agent, tmp_path)["approvals"][0]
    assert a["title"] and a["runId"]
    assert a["body"], "the human-readable case for the action"
    assert a["action"] and a["action"].get("tool"), "which tool runs on approve"
    assert isinstance(a["action"].get("input"), dict), "and with what arguments"


def test_each_approval_says_which_agent_it_belongs_to(tmp_path):
    """`list_approvals` is workspace-wide by design — `GET /approvals` is an inbox and
    `app.py` says so. The snapshot's other keys are scoped to ONE agent, so every row
    carries `agent` and the console marks the ones that are not the selected agent.

    Narrowing here instead would be worse: hiding a pending gate because a different
    agent happens to be selected is how a run waits forever.
    """
    scaffold.write_project(tmp_path, "agg", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    engine = Engine(manifest, agent, store, tmp_path)
    engine.run_event("message.received", {"email": "ada@x.com"})

    # A second agent's paused run, in the same workspace.
    other = {"id": store.new_run_id(), "agent": "other-agent", "status": "waiting_approval",
             "trace": [], "journal": {}, "createdAt": "2026-08-07T00:00:00Z"}
    store.save_run(other)
    store.create_approval(other["id"], "Refund #9", "duplicate charge",
                          {"tool": "payments.refund", "input": {"amount": 500000}})

    rows = {a["title"]: a for a in build_console(manifest, store, agent, tmp_path)["approvals"]}
    assert len(rows) == 2, "the inbox stays workspace-wide"
    assert rows["Refund #9"]["agent"] == "other-agent"
    assert next(v["agent"] for k, v in rows.items() if k != "Refund #9") == "agg"


def test_console_page_is_served(tmp_path, monkeypatch):
    """`/` serves the React bundle's index.html — the one console there is.

    The legacy single-file SPA that used to answer here (and at `/console.html`) is
    deleted: every view it had is a React component now. `/console.html` was only ever
    an alias for it and is gone with it; `/console` (the JSON aggregate) is unrelated
    and still very much alive.
    """
    from rya.api import app as app_mod

    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/")
    assert "text/html" in r.headers["content-type"]
    if app_mod._CONSOLE_DIST is None:
        # A checkout that never ran `npm run build`: an ordinary state, and it must
        # name the fix rather than 404.
        assert r.status_code == 503
        assert "npm run build" in r.text
    else:
        assert r.status_code == 200
        assert '<div id="root">' in r.text
    assert c.get("/console.html").status_code == 404


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


def _preflight(client, path, method, origin="http://localhost:4321"):
    """The browser's actual first request for a non-simple method.

    Worth doing on the wire rather than reading `allow_methods` back out of the
    middleware: a preflight the middleware REJECTS is a 400 with no
    `access-control-allow-*` at all, and that 400 — not the later DELETE — is what
    the operator sees. Any path works, because CORS answers preflights ahead of
    routing.
    """
    return client.options(path, headers={
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization",
    })


def test_cors_preflights_the_destructive_verbs(tmp_path, monkeypatch):
    """Revoking an API key and removing a member are `@api.delete`, and DELETE was
    missing from `allow_methods` — so cross-origin, both died at the preflight with a
    400 before the request was ever made. Invisible in both normal setups: `rya serve`
    is same-origin, and the Vite dev server proxies.
    """
    monkeypatch.setenv("RYA_CORS_ORIGINS", "http://localhost:4321")
    c, _ = _client(tmp_path, monkeypatch)

    r = _preflight(c, "/v1/workspaces/ws_x/keys/key_y", "DELETE")
    assert r.status_code != 400, "the preflight itself was refused: DELETE is not allowed"
    assert r.status_code == 200
    allowed = {m.strip() for m in r.headers.get("access-control-allow-methods", "").split(",")}
    assert "DELETE" in allowed

    # PATCH stays out: there is no @api.patch route, and this is an allow-list, so a
    # verb nothing serves has no business being advertised. Guards a future widening,
    # and passed before the DELETE fix too.
    assert _preflight(c, "/v1/workspaces/ws_x/keys/key_y", "PATCH").status_code == 400


def _router_methods(app):
    """Every HTTP method the router actually exposes.

    HEAD and OPTIONS are excluded: Starlette adds HEAD to every GET route for free,
    and OPTIONS is the preflight the CORS middleware answers itself — neither is a
    verb anyone declared, so neither belongs in a hand-written allow-list.
    """
    methods = set()
    for route in app.routes:
        methods |= set(getattr(route, "methods", None) or ())
    return methods - {"HEAD", "OPTIONS"}


def test_cors_allowlist_covers_every_method_the_router_exposes(tmp_path, monkeypatch):
    """The drift alarm, and the actual fix for §5.18.

    `allow_methods` in `create_app` is hand-written and derived from nothing, so it
    silently stopped matching the router the moment the first `@api.delete` landed.
    It cannot be derived at the call site (the router is still half-registered there,
    and moving the registration would reorder the middleware), so instead: walk the
    finished router, ask the running middleware what it permits, and fail loudly on
    the next verb someone adds. Preflights again — the wire is the contract.
    """
    monkeypatch.setenv("RYA_CORS_ORIGINS", "http://localhost:4321")
    c, _ = _client(tmp_path, monkeypatch)

    declared = _router_methods(c.app)
    assert {"GET", "POST", "PUT", "DELETE"} <= declared, \
        "sanity: these verbs are all in use, so a walk that misses one is broken"

    refused = sorted(m for m in declared if _preflight(c, "/console", m).status_code == 400)
    assert not refused, (
        f"the router serves {refused} but CORS rejects the preflight for them — add them "
        "to allow_methods in create_app (and check nothing else drifted)")


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
    assert missing.json()["error"]["code"] == "E_ENVIRONMENT_NOT_FOUND"
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
    # The self-hosted icon library is gone with the legacy console: icons are now
    # `lucide-react` imports compiled into the bundle, so there is no separate asset
    # to serve and no CDN dependency either way.
    assert c.get("/lucide.min.js").status_code == 404
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


# ---------------------------------------------------------------------------
# The React console at `/` (source: web/console/, built to src/rya/console/dist).
#
# It served /v2 while the legacy single-file SPA held `/`; that migration is done and
# the legacy file is deleted, so this is now the only console and `/v2` is a redirect.
#
# `dist/` is gitignored build output, so these tests must pass BOTH on a machine that
# has run `npm run build` and on one that never installed Node. Each test below states
# which arm it covers rather than skipping silently.
# ---------------------------------------------------------------------------

def test_console_is_never_a_404(tmp_path, monkeypatch):
    """`/` is the bundle, or a 503 that names the build command — never a 404.

    This matters more than it did at /v2: there is no second console to fall back to,
    so an unbuilt frontend has to explain itself.
    """
    from rya.api import app as app_mod

    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/")
    if app_mod._CONSOLE_DIST is None:
        assert r.status_code == 503
        assert "npm run build" in r.text
        assert "API is unaffected" in r.text
    else:
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert '<div id="root">' in r.text


def test_v2_redirects_to_the_root(tmp_path, monkeypatch):
    """/v2 was the console's address for the whole migration, so it is in bookmarks
    and in docs. It redirects rather than 404s, permanently and without changing the
    method."""
    c, _ = _client(tmp_path, monkeypatch)
    for path in ("/v2", "/v2/"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 308, path
        assert r.headers["location"] == "/", path


def test_the_console_needs_no_inline_script(tmp_path, monkeypatch):
    """The build step's security win, now unconditional.

    The legacy console was one big inline <script>, so `/` had to allow
    'unsafe-inline' for scripts. The React bundle loads external modules, so with the
    legacy file deleted the allowance is GONE from the policy rather than merely
    unused by one of two consoles.
    """
    c, _ = _client(tmp_path, monkeypatch)

    def script_src(resp):
        csp = resp.headers["content-security-policy"]
        return next(d for d in csp.split(";") if d.strip().startswith("script-src"))

    # True on both arms: the header is a constant, not a function of the bundle.
    assert "'unsafe-inline'" not in script_src(c.get("/"))


def test_console_assets_carry_security_headers(tmp_path, monkeypatch):
    """A mount bypasses route-level headers, so the StaticFiles subclass must add them."""
    import re

    from rya.api import app as app_mod

    if app_mod._CONSOLE_DIST is None:
        pytest.skip("frontend not built (run `cd web/console && npm run build`)")

    c, _ = _client(tmp_path, monkeypatch)
    refs = [a for a in re.findall(r'(?:src|href)="([^"]+)"', c.get("/").text)
            if a.startswith("/assets/")]
    assert refs, "the built index.html should reference at least one hashed asset"
    for ref in refs:
        r = c.get(ref)
        assert r.status_code == 200, ref
        assert r.headers.get("content-security-policy"), f"no CSP on {ref}"
        assert r.headers.get("x-content-type-options") == "nosniff", ref


def test_the_console_csp_permits_no_cross_origin_socket(tmp_path, monkeypatch):
    """`connect-src` was `'self' ws: wss:`, and bare `ws:`/`wss:` are SCHEME sources —
    a socket to any host on the internet, i.e. an exfiltration channel, sitting in the
    directive whose only job is containment. Nothing in the console wanted it: no
    `WebSocket`, no `EventSource` (the turn-stream inspector is `fetch` + `getReader()`
    over SSE, which is a plain `connect-src` fetch to 'self').
    """
    c, _ = _client(tmp_path, monkeypatch)
    csp = c.get("/").headers["content-security-policy"]

    directives = {}
    for d in csp.split(";"):
        parts = d.split()
        if parts:
            directives[parts[0]] = parts[1:]
    # Exactly 'self' and nothing beside it. Compared as a token LIST, not with `in`:
    # "'self'" appears in five other directives, so a substring test over the whole
    # header would pass with the scheme sources still present.
    assert directives.get("connect-src") == ["'self'"], csp
    # And no scheme source smuggled into any other directive either. Tokenised for the
    # same reason in reverse: `"ws:" in csp` is satisfied by the "wss:" it is meant to
    # be a separate check for, so it can never fail alone.
    tokens = {t for v in directives.values() for t in v}
    assert not tokens & {"ws:", "wss:"}, csp


def test_console_cache_policy_is_immutable_assets_and_a_revalidated_index(tmp_path, monkeypatch):
    """Shipped backwards, and the index half is an outage.

    `index.html` names the current asset hashes and carried NO `Cache-Control` and no
    validator at all, so an intermediary picks a heuristic TTL and pins it; after a
    deploy the pinned index asks for hashes that no longer exist, the bundle 404s, and
    the operator gets a blank page with nothing in any log. Meanwhile the assets —
    content-hashed, so a change is a new URL — carried only a validator, buying a
    conditional request on every page load for files that can never change.
    """
    import re

    from rya.api import app as app_mod

    if app_mod._CONSOLE_DIST is None:
        pytest.skip("frontend not built (run `cd web/console && npm run build`)")

    c, _ = _client(tmp_path, monkeypatch)
    index = c.get("/")
    assert index.status_code == 200
    # Storable, but never reusable without asking first.
    assert index.headers.get("cache-control") == "no-cache"

    refs = [a for a in re.findall(r'(?:src|href)="([^"]+)"', index.text)
            if a.startswith("/assets/")]
    assert refs, "the built index.html should reference at least one hashed asset"
    for ref in refs:
        r = c.get(ref)
        assert r.status_code == 200, ref
        cc = r.headers.get("cache-control", "")
        assert "immutable" in cc and "max-age=31536000" in cc, f"{ref}: {cc!r}"
        # The security headers are still shared: only the cache policy differs.
        assert r.headers.get("content-security-policy"), ref


def test_the_unbuilt_bundle_explainer_is_never_stored(tmp_path, monkeypatch):
    """A cached 503 outliving the build that fixes it is the pinned-index failure with
    the sign flipped: a working deployment reporting itself broken, and no way to tell
    it to stop.

    `_CONSOLE_DIST` is a module global resolved at import; the `/assets` mount is
    conditioned on it at build time and `_console_index()` reads it per call, so the
    patch has to land before `build_app`.
    """
    from rya.api import app as app_mod

    monkeypatch.setattr(app_mod, "_CONSOLE_DIST", None)
    c, _ = _client(tmp_path, monkeypatch)

    r = c.get("/")
    assert r.status_code == 503, "this test is worthless unless it took the unbuilt branch"
    assert "npm run build" in r.text
    assert r.headers.get("cache-control") == "no-store"
    # Same policy as every other console response — an error page is still a page.
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


def test_console_dist_detection_requires_an_index(tmp_path, monkeypatch):
    """A dist/ dir without index.html counts as not-built, not as a broken bundle.

    Guards the fresh-clone path: a stale or half-written dist/ must fall back to
    the 503 explainer rather than mounting a directory with nothing to serve.
    """
    import importlib.resources

    from rya.api import app as app_mod

    dist = tmp_path / "console" / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setattr(importlib.resources, "files", lambda _pkg: tmp_path)

    assert app_mod._console_dist_dir() is None  # dist/ exists but is empty

    (dist / "index.html").write_text("<!doctype html>")
    assert app_mod._console_dist_dir() is not None  # index.html present -> built


# ---------------------------------------------------------------------------
# Governance reads the sources that ENFORCE (audit §4.5)
# ---------------------------------------------------------------------------
# Both of this view's governed sources had moved and neither read moved with
# them: kill switches came from the pre-§11.2 `_runtime_config` memory scope
# that nothing writes, and egress came from `rya.guard.yaml` while the editor
# writes the store. The tests below are written against the ENFORCEMENT path —
# they set a switch through the real endpoint and read the guard through the
# real resolver — because a governance dashboard that agrees with a fixture but
# not with the runtime is exactly the defect.


def _gov(client, agent="console-agent"):
    return client.get(f"/console?agent={agent}").json()["governance"]


def test_a_live_kill_switch_appears_in_governance(tmp_path, monkeypatch):
    """Kill a tool through the API; the screen an operator opens to confirm it must
    not say "No overrides."."""
    c, _ = _client(tmp_path, monkeypatch)
    r = c.put("/tools/email.send/permission",
              json={"permission": "disabled", "reason": "incident 42"})
    assert r.status_code == 200

    g = _gov(c)
    assert [o["tool"] for o in g["switches"]["active"]] == ["email.send"]
    assert g["switches"]["active"][0]["permission"] == "disabled"
    assert g["switches"]["active"][0]["reason"] == "incident 42"
    assert g["switches"]["error"] is None
    # The document version, since the log versions the whole map and not each tool.
    assert g["switches"]["version"] == 1


def test_governance_counts_effective_permissions_not_declared_ones(tmp_path, monkeypatch):
    """"Denied: 0" while a tool is killed is the same lie as an empty table."""
    c, _ = _client(tmp_path, monkeypatch)
    before = _gov(c)["policy"]
    assert before["toolsGated"] == 1 and before["toolsDenied"] == 0
    assert before["toolsOverridden"] == 0

    c.put("/tools/email.send/permission", json={"permission": "disabled"})

    after = _gov(c)["policy"]
    assert after["toolsDenied"] == 1, "the killed tool must count as denied"
    assert after["toolsGated"] == 0, "and must stop counting as merely gated"
    assert after["toolsOverridden"] == 1
    # The hash is described as the version an auditor pins a run to, so it has to
    # cover the override. It was computed from manifest permissions alone.
    assert after["hash"] != before["hash"]


def test_kill_switch_history_is_derived_from_the_policy_log(tmp_path, monkeypatch):
    """The log stores document snapshots; the console wants per-tool transitions.

    Nothing records WHICH tool an operator touched, so the rows are diffed out of
    each record's before/after. `actor` rides along — §12 risk 7 calls "who changed
    this" a feature, and it was written on every record and shown nowhere.
    """
    c, _ = _client(tmp_path, monkeypatch)
    c.put("/tools/email.send/permission", json={"permission": "disabled", "reason": "incident 42"})
    c.put("/tools/email.send/permission", json={"clear": True})

    hist = _gov(c)["switches"]["history"]
    assert len(hist) == 2, hist
    newest, oldest = hist  # newest first
    assert oldest["tool"] == "email.send"
    assert oldest["previous"] is None and oldest["permission"] == "disabled"
    assert oldest["reason"] == "incident 42" and oldest["cleared"] is False
    # Cleared: the override is gone and the MANIFEST governs again, so the row says
    # what it went back to rather than leaving the operator to guess.
    assert newest["cleared"] is True
    assert newest["previous"] == "disabled"
    assert newest["permission"] == "approval_required"
    assert "actor" in newest and "version" in newest
    # …and the switch really is gone, not merely logged.
    assert _gov(c)["switches"]["active"] == []


def test_a_record_that_changed_another_tool_yields_no_row(tmp_path, monkeypatch):
    """One record can hold several tools; only the ones that moved are transitions."""
    c, _ = _client(tmp_path, monkeypatch)
    c.put("/tools/email.send/permission", json={"permission": "disabled"})
    c.put("/tools/crm.lookup/permission", json={"permission": "disabled"})

    hist = _gov(c)["switches"]["history"]
    # Two writes, two rows — NOT three. The second record carries email.send in both
    # its before and its after, unchanged.
    assert [h["tool"] for h in hist] == ["crm.lookup", "email.send"]


def test_governance_reads_the_guard_the_runtime_resolves(tmp_path, monkeypatch):
    """Store first, file second — the precedence `_guard_source` writes under.

    Two views a click apart used to describe the firewall from different sources.
    """
    from rya.guard import policy_key
    from rya.store import Store

    c, root = _client(tmp_path, monkeypatch)
    file_rules = _gov(c)["policy"]["egressRules"]
    assert _gov(c)["policy"]["egressSource"].startswith("file:")
    assert file_rules > 1, "the scaffolded guard file ships several rules"

    Store(root).policy_set(policy_key("console-agent"), {"policy": {
        "default": "allow", "rules": [{"host": "api.stripe.com", "action": "allow"}]}})

    p = _gov(c)["policy"]
    assert p["egressSource"] == "store"
    assert p["egressRules"] == 1 != file_rules
    assert p["egressDefault"] == "allow"
    assert p["egressVersion"], "the guard's own version, so the two screens can be matched"


def test_the_policy_hash_moves_when_the_live_allowlist_changes(tmp_path, monkeypatch):
    """It is described as the version an auditor pins a run to. Hashing the FILE
    meant it never moved for a store-backed policy — an auditable pin to a document
    nobody was enforcing."""
    from rya.guard import policy_key
    from rya.store import Store

    c, root = _client(tmp_path, monkeypatch)
    store = Store(root)
    store.policy_set(policy_key("console-agent"),
                     {"policy": {"default": "deny", "rules": [{"host": "a.example", "action": "allow"}]}})
    before = _gov(c)["policy"]["hash"]

    r = c.put("/guard", json={"policy": {"default": "deny",
                                         "rules": [{"host": "attacker.example", "action": "allow"}]}})
    assert r.status_code == 200
    assert _gov(c)["policy"]["hash"] != before


def test_a_published_bundle_with_no_guard_file_still_reports_its_policy(tmp_path, monkeypatch):
    """A bundle need not ship `rya.guard.yaml`. Reading the file reported
    "off · 0 rules · not configured" over a deny-default policy that was actively
    refusing requests — the inverse error, and just as unusable."""
    from rya.guard import policy_key
    from rya.store import Store

    c, root = _client(tmp_path, monkeypatch)
    (root / "rya.guard.yaml").unlink()
    Store(root).policy_set(policy_key("console-agent"), {"policy": {
        "default": "deny",
        "rules": [{"host": "api.stripe.com", "action": "allow"}],
        "grounding": {"enabled": True}}})

    g = _gov(c)
    assert g["enforcement"]["egressGuard"] is True
    assert g["enforcement"]["groundingGate"] is True
    assert g["policy"]["egressRules"] == 1
    assert g["policy"]["egressDefault"] == "deny"


def test_no_policy_anywhere_is_reported_as_no_policy(tmp_path, monkeypatch):
    """The one case that stays a no-op must still be distinguishable from a failure."""
    c, root = _client(tmp_path, monkeypatch)
    (root / "rya.guard.yaml").unlink()

    g = _gov(c)
    assert g["enforcement"]["egressGuard"] is False
    assert g["policy"]["egressSource"] == "none"
    assert g["policy"]["egressDefault"] is None
    assert g["policy"]["egressError"] is None


def test_an_unreadable_policy_store_is_reported_not_swallowed(tmp_path, monkeypatch):
    """The §4.5 failure one layer up: an empty table and an unreachable store look
    identical and mean opposite things."""
    from rya.manifest import load_manifest
    from rya.snapshot import build_console
    from rya.store import Store

    scaffold.write_project(tmp_path, "gov", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path)

    def boom(key):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(store, "policy_get", boom)
    g = build_console(manifest, store, None, tmp_path)["governance"]

    assert g["switches"]["active"] == []
    assert "connection refused" in (g["switches"]["error"] or "")
    # The guard resolver fails CLOSED on the same error, and the view says so rather
    # than describing the file as though it were in force.
    assert "connection refused" in (g["policy"]["egressError"] or "")
    assert g["policy"]["egressRules"] == 0
