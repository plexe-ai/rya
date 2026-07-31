"""A3 (Phase 5) core gates: connection upsert, fail-closed identity, the
credential-only-for-egress refactor, the E_CONNECTION_EXPIRED → needs_reconnect
outcome, and the plaintext-at-rest deploy block.

These are the runtime primitives that per-user, url-backed connection tools rely on. The
reconnect signal is exercised here (not as a live eval) because no offline
provider can synthesise an upstream 401 — same rationale as the other
deterministic gates.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from rya.auth import Identity
from rya.manifest import load_manifest
from rya.readiness import check_readiness
from rya.runtime import Engine, load_agent
from rya.store import Store

SECRET = "ghtok_super_secret_value_123"


@contextmanager
def _serving(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


def _status_server(code, body=b'{"error":"expired"}'):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(code)
            self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return H


def _url_agent(tmp_path, port, require_user=False):
    """A url-backed tool with provider=github (no handler → the credential path
    actually runs)."""
    ru = "    require_user: true\n" if require_user else ""
    (tmp_path / "rya.agent.yaml").write_text(
        "name: gh\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: gh.issues\n    permission: allowed\n"
        f"    url: http://127.0.0.1:{port}\n    provider: github\n    scopes: [issues:write]\n{ru}")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
        "    return await ctx.tools.call('gh.issues', {'title':'bug'})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    return manifest, store


# ---- upsert overwrite-in-place ---------------------------------------------
def test_upsert_overwrites_in_place_no_stale_duplicate(tmp_path):
    store = Store(tmp_path); store.ensure()
    a = store.upsert_connection("crm", ["crm:read"], secret="tok-1", owner="c1")
    b = store.upsert_connection("crm", ["crm:read", "crm:write"], secret="tok-2", owner="c1")
    assert a["id"] == b["id"]                                    # same doc, in place
    assert b.get("updatedAt") and b["createdAt"] == a["createdAt"]
    assert store.get_connection("crm", "c1")["secret"] == "tok-2"   # newest token wins
    active = [c for c in store.list_connections()
              if c["provider"] == "crm" and c.get("owner") == "c1"]
    assert len(active) == 1                                       # no stale duplicate
    # a different owner is a distinct connection, untouched by c1's re-login
    store.upsert_connection("crm", ["crm:read"], secret="tok-x", owner="c2")
    assert store.get_connection("crm", "c2")["secret"] == "tok-x"
    assert store.get_connection("crm", "c1")["secret"] == "tok-2"


def test_upsert_migrates_a_create_made_duplicate(tmp_path):
    # create_connection always mints a new doc; a subsequent upsert must adopt the
    # existing (provider, owner) doc rather than add yet another.
    store = Store(tmp_path); store.ensure()
    store.create_connection("crm", ["crm:read"], secret="old", owner="c1")
    store.upsert_connection("crm", ["crm:read"], secret="new", owner="c1")
    active = [c for c in store.list_connections()
              if c["provider"] == "crm" and c.get("owner") == "c1"]
    assert len(active) == 1 and store.get_connection("crm", "c1")["secret"] == "new"


# ---- E_CONNECTION_EXPIRED → needs_reconnect --------------------------------
def test_expired_token_surfaces_reconnect(tmp_path):
    with _serving(_status_server(401)) as port:
        manifest, store = _url_agent(tmp_path, port)
        store.create_connection("github", ["issues:write"], secret=SECRET)
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        run = engine.run_event("x", {})
    assert run["status"] == "needs_reconnect"                    # not a generic failure
    assert run["error"]["code"] == "E_CONNECTION_EXPIRED"
    # a run.needs_reconnect trace step, and NO run.failed
    kinds = [e.get("label") for e in run["trace"] if e["kind"] == "trace"]
    assert "run.failed" not in kinds
    assert SECRET not in json.dumps(run["trace"])                # credential still vaulted


def test_upstream_401_without_credential_is_plain_error(tmp_path):
    # A 401 on a request we did NOT authenticate is a normal upstream error, not a
    # reconnect signal (there is no credential to renew).
    with _serving(_status_server(401)) as port:
        (tmp_path / "rya.agent.yaml").write_text(
            "name: gh\nruntime: python\nentrypoint: agent.py\n"
            f"tools:\n  - id: t\n    permission: allowed\n    url: http://127.0.0.1:{port}\n")
        (tmp_path / "agent.py").write_text(
            "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
            "    return await ctx.tools.call('t', {})\n")
        manifest = load_manifest(tmp_path / "rya.agent.yaml")
        store = Store(tmp_path); store.ensure()
        run = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path).run_event("x", {})
    assert run["status"] == "failed" and run["error"]["code"] == "E_TOOL_UPSTREAM"


# ---- fail closed on missing identity ---------------------------------------
def test_require_user_fails_closed_without_identity(tmp_path):
    with _serving(_status_server(200, b'{"ok":true}')) as port:
        manifest, store = _url_agent(tmp_path, port, require_user=True)
        store.create_connection("github", ["issues:write"], secret=SECRET)
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        run = engine.run_event("x", {})                          # no identity presented
    assert run["status"] == "failed" and run["error"]["code"] == "E_NO_IDENTITY"


def test_require_user_allows_with_identity(tmp_path):
    with _serving(_status_server(200, b'{"ok":true}')) as port:
        manifest, store = _url_agent(tmp_path, port, require_user=True)
        # a per-user connection for this identity
        store.create_connection("github", ["issues:write"], secret=SECRET, owner="user-1")
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        run = engine.run_event("x", {}, identity=Identity("user-1", {"scopes": ["issues:write"]}))
    assert run["status"] == "completed"


# ---- credential enforcement only for egress backends -----------------------
def test_handler_backed_provider_tool_runs_without_connection(tmp_path):
    # A provider+require_user tool that has a LOCAL @agent.tool handler never
    # egresses the secret, so it must run offline with NO connection and NO
    # identity — a local-handler tool stays deterministic offline while
    # url-backed tools carry a live `url:`.
    (tmp_path / "rya.agent.yaml").write_text(
        "name: h\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: local.read\n    permission: allowed\n    provider: crm\n"
        "    scopes: [crm:read]\n    require_user: true\n    url: https://crm-api.example/x\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n"
        "@agent.tool('local.read')\nasync def r(inp):\n    return {'ok':True}\n"
        "@agent.on_event\nasync def h(ctx,e):\n    return await ctx.tools.call('local.read', {})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    run = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path).run_event("x", {})
    assert run["status"] == "completed"


# ---- plaintext-at-rest deploy block ----------------------------------------
def _readiness_agent(tmp_path):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: h\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: c.read\n    permission: read_only\n    provider: crm\n"
        "    scopes: [crm:read]\n    require_user: true\n    url: https://crm-api.example/x\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n    return {}\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    return manifest, store


def test_plaintext_connection_blocks_deploy(tmp_path):
    manifest, store = _readiness_agent(tmp_path)
    pub = store.create_connection("crm", ["crm:read"], secret="PLAINTOKEN", owner="c1")
    # simulate seal() degrading to plaintext (no key material) by writing the raw
    # secret to disk without the enc:v1: envelope.
    p = tmp_path / ".rya" / "connections" / f"{pub['id']}.json"
    doc = json.loads(p.read_text()); doc["secret"] = "PLAINTOKEN"; p.write_text(json.dumps(doc))
    rep = check_readiness(manifest, store, load_agent(manifest, tmp_path), tmp_path)
    assert rep["ready"] is False
    assert "E_PLAINTEXT_SECRET_AT_REST" in [b["code"] for b in rep["blocks"]]


def test_sealed_connection_is_ready(tmp_path):
    manifest, store = _readiness_agent(tmp_path)
    store.create_connection("crm", ["crm:read"], secret="tok", owner="c1")  # sealed (keyfile)
    rep = check_readiness(manifest, store, load_agent(manifest, tmp_path), tmp_path)
    assert "E_PLAINTEXT_SECRET_AT_REST" not in [b["code"] for b in rep["blocks"]]
