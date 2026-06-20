"""Scoped connected credentials — the agent-authority intersection rule,
credential injection + vaulting, and store isolation."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from rya.auth import Identity
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


@contextmanager
def _serving(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


SECRET = "ghtok_super_secret_value_123"


def _capture_server():
    seen = {"auth": None, "hits": 0}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["hits"] += 1
            seen["auth"] = self.headers.get("Authorization")
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *a): pass

    return H, seen


def _agent(tmp_path, port, scopes="issues:write"):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: gh\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: gh.issues\n    permission: allowed\n"
        f"    url: http://127.0.0.1:{port}\n    provider: github\n    scopes: [{scopes}]\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
        "    return await ctx.tools.call('gh.issues', {'title':'bug'})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    return manifest, store


def test_store_connection_never_exposes_secret(tmp_path):
    store = Store(tmp_path); store.ensure()
    pub = store.create_connection("github", ["repo:read"], secret=SECRET, label="gh")
    assert "secret" not in pub and pub["secretSet"] is True
    assert all("secret" not in c for c in store.list_connections())
    # the resolver returns the secret (for runtime injection only)
    assert store.get_connection("github")["secret"] == SECRET


def test_credential_injected_and_vaulted(tmp_path):
    H, seen = _capture_server()
    with _serving(H) as port:
        manifest, store = _agent(tmp_path, port)
        store.create_connection("github", ["issues:write", "repo:read"], secret=SECRET)
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        run = engine.run_event("x", {})
    assert run["status"] == "completed"
    # the credential reached the upstream tool as a bearer token...
    assert seen["auth"] == f"Bearer {SECRET}"
    # ...but never appears anywhere in the run trace (vaulted + redacted)
    assert SECRET not in json.dumps(run["trace"])
    # the call recorded which provider/scopes were used
    tool_ev = next(e for e in run["trace"] if e["kind"] == "tool.call")
    assert tool_ev["data"]["provider"] == "github"


def test_intersection_rule_denies_when_user_lacks_scope(tmp_path):
    H, seen = _capture_server()
    with _serving(H) as port:
        manifest, store = _agent(tmp_path, port)
        # connection HAS issues:write, but the *user* only has repo:read →
        # effective = connection ∩ user = {repo:read} → missing issues:write.
        store.create_connection("github", ["issues:write", "repo:read"], secret=SECRET)
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        ident = Identity("user-1", {"scopes": ["repo:read"]})
        run = engine.run_event("x", {}, identity=ident)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_SCOPE_DENIED"
    assert seen["hits"] == 0  # the call never went out


def test_intersection_rule_allows_when_user_has_scope(tmp_path):
    H, seen = _capture_server()
    with _serving(H) as port:
        manifest, store = _agent(tmp_path, port)
        store.create_connection("github", ["issues:write", "repo:read"], secret=SECRET)
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        ident = Identity("user-1", {"scopes": ["issues:write", "billing:admin"]})
        run = engine.run_event("x", {}, identity=ident)
    assert run["status"] == "completed" and seen["hits"] == 1


def test_missing_connection_is_blocked(tmp_path):
    H, seen = _capture_server()
    with _serving(H) as port:
        manifest, store = _agent(tmp_path, port)
        # no connection created at all
        engine = Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)
        run = engine.run_event("x", {})
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_NO_CONNECTION"
    assert seen["hits"] == 0
