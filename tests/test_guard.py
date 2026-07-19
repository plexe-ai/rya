"""Action Guard — egress policy engine, SSRF, real enforcement, and API."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@contextmanager
def _serving(handler_cls):
    """Run a local HTTP server that handles requests until torn down.

    Earlier this used a single-shot ``handle_request()`` on a daemon thread,
    which raced the client and intermittently surfaced 'Connection reset by
    peer'. ``serve_forever`` on a background thread (cleanly shut down on exit)
    removes that race.
    """
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.errors import RyaError
from rya.guard import check_egress, evaluate, is_ssrf, run_tests
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store

POLICY = {
    "ssrf": True, "default": "deny", "fail": "closed",
    "rules": [
        {"action": "allow", "kind": "prefix", "pattern": "https://api.acme.com/", "methods": ["GET", "POST"]},
        {"action": "allow", "kind": "glob", "pattern": "https://*.openai.com/*"},
        {"action": "deny", "kind": "glob", "pattern": "https://webhook.site/*", "note": "exfil host"},
    ],
}


# ---- engine --------------------------------------------------------------
def test_allow_deny_default():
    assert evaluate("https://api.acme.com/orders", "POST", POLICY)["decision"] == "allow"
    assert evaluate("https://api.openai.com/v1/x", "POST", POLICY)["decision"] == "allow"
    assert evaluate("https://elsewhere.com/x", "POST", POLICY)["decision"] == "block"  # default deny
    d = evaluate("https://webhook.site/abc", "POST", POLICY)
    assert d["decision"] == "block" and d["reason"] == "exfil host"


def test_deny_beats_allow():
    pol = {"default": "allow", "ssrf": False, "rules": [
        {"action": "allow", "kind": "glob", "pattern": "https://x.com/*"},
        {"action": "deny", "kind": "glob", "pattern": "https://x.com/secret*"},
    ]}
    assert evaluate("https://x.com/secret/1", "GET", pol)["decision"] == "block"
    assert evaluate("https://x.com/public", "GET", pol)["decision"] == "allow"


def test_method_scoping():
    pol = {"default": "deny", "ssrf": False, "rules": [
        {"action": "allow", "kind": "prefix", "pattern": "https://api.acme.com/", "methods": ["GET"]}]}
    assert evaluate("https://api.acme.com/x", "GET", pol)["decision"] == "allow"
    assert evaluate("https://api.acme.com/x", "POST", pol)["decision"] == "block"


def test_ssrf():
    for h in ("localhost", "127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "metadata.google.internal"):
        assert is_ssrf(h) is True
    assert is_ssrf("api.anthropic.com") is False
    assert evaluate("http://169.254.169.254/latest/meta-data/", "GET", POLICY)["decision"] == "block"


def test_run_tests_metrics():
    rep = run_tests(POLICY)
    assert rep["attacksBlocked"] == rep["attacksTotal"]   # all attacks blocked
    assert rep["benignFalseBlocks"] == 0
    assert rep["accuracy"] == 100


# ---- real enforcement at the egress chokepoint ---------------------------
def _http_tool_agent(tmp_path, port):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        f"tools:\n  - id: remote.call\n    permission: allowed\n    url: http://127.0.0.1:{port}\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
        "    return await ctx.tools.call('remote.call', {'x':1})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    return Engine(manifest, load_agent(manifest, tmp_path), Store(tmp_path), tmp_path)


def test_egress_blocked_never_leaves(tmp_path, monkeypatch):
    received = {"hit": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["hit"] = True
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))  # drain body before replying
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *a): pass

    with _serving(H) as port:
        # Guard that denies everything by default (and would SSRF-block 127.0.0.1 anyway).
        guard = tmp_path / "rya.guard.yaml"
        guard.write_text("ssrf: true\ndefault: deny\nrules: []\n")
        monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

        engine = _http_tool_agent(tmp_path, port)
        run = engine.run_event("x", {})
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_EGRESS_BLOCKED"
    assert received["hit"] is False  # the request never left the process


def test_egress_allowed_passes(tmp_path, monkeypatch):
    received = {"hit": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["hit"] = True
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))  # drain body before replying
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *a): pass

    with _serving(H) as port:
        guard = tmp_path / "rya.guard.yaml"
        guard.write_text(f"ssrf: false\ndefault: deny\nrules:\n  - {{action: allow, kind: prefix, pattern: 'http://127.0.0.1:{port}'}}\n")
        monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

        engine = _http_tool_agent(tmp_path, port)
        run = engine.run_event("x", {})
    assert run["status"] == "completed"
    assert received["hit"] is True


def test_no_policy_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("RYA_GUARD_PATH", str(tmp_path / "missing.yaml"))
    check_egress("https://anything.com/x", "POST")  # must not raise


# ---- API -----------------------------------------------------------------
def test_guard_api(tmp_path, monkeypatch):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_GUARD_PATH"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "guard-agent", template="demo")  # writes a default rya.guard.yaml
    c = TestClient(build_app(tmp_path))

    g = c.get("/guard").json()
    assert g["exists"] is True
    assert any(r["pattern"].startswith("https://api.anthropic") for r in g["policy"]["rules"])
    assert g["tests"]["accuracy"] == 100

    new = {"ssrf": True, "default": "deny", "fail": "closed", "rules": [
        {"action": "deny", "kind": "glob", "pattern": "https://evil.com/*", "note": "blocked"}]}
    r = c.put("/guard", json={"policy": new})
    assert r.status_code == 200
    assert (tmp_path / "rya.guard.yaml").exists()
    assert c.get("/guard").json()["policy"]["rules"][0]["pattern"] == "https://evil.com/*"
