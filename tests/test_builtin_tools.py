"""Real built-in tools (http.request, web.fetch) — actual IO, Action-Guard governed,
and usable by the agent loop so it does real work."""

import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rya.errors import RyaError
from rya.tools.registry import default_registry


@contextmanager
def _serving(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body><h1>Hello</h1><p>World &amp; more</p>"
                         b"<script>steal()</script></body></html>")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        self.send_response(201); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def _allow_localhost(tmp_path, monkeypatch, port):
    g = tmp_path / "rya.guard.yaml"
    g.write_text(f"ssrf: false\ndefault: deny\nrules:\n"
                 f"  - {{action: allow, kind: prefix, pattern: 'http://127.0.0.1:{port}'}}\n")
    monkeypatch.setenv("RYA_GUARD_PATH", str(g))


def test_web_fetch_returns_real_stripped_text(tmp_path, monkeypatch):
    reg = default_registry()
    with _serving(H) as port:
        _allow_localhost(tmp_path, monkeypatch, port)
        out = reg.get("web.fetch").fn({"url": f"http://127.0.0.1:{port}/"})
    assert out["status"] == 200
    assert "Hello" in out["text"] and "World & more" in out["text"]
    assert "<h1>" not in out["text"] and "steal()" not in out["text"]  # tags + script stripped


def test_http_request_real_post(tmp_path, monkeypatch):
    reg = default_registry()
    with _serving(H) as port:
        _allow_localhost(tmp_path, monkeypatch, port)
        out = reg.get("http.request").fn({"url": f"http://127.0.0.1:{port}/x",
                                          "method": "POST", "body": {"a": 1}})
    assert out["status"] == 201 and out["json"] == {"ok": True}


def test_builtin_tools_respect_action_guard(tmp_path, monkeypatch):
    reg = default_registry()
    with _serving(H) as port:
        g = tmp_path / "rya.guard.yaml"
        g.write_text("ssrf: true\ndefault: deny\nrules: []\n")  # loopback SSRF-blocked
        monkeypatch.setenv("RYA_GUARD_PATH", str(g))
        with pytest.raises(RyaError) as e:
            reg.get("web.fetch").fn({"url": f"http://127.0.0.1:{port}/"})
    assert e.value.code == "E_EGRESS_BLOCKED"


def test_agent_loop_does_real_work(tmp_path, monkeypatch):
    from rya.manifest import load_manifest
    from rya.runtime import load_agent
    from rya.sdk.context import RuntimeContext
    from rya.store import Store
    from rya.models.registry import default_registry as models_registry

    (tmp_path / "rya.agent.yaml").write_text(
        "name: ai\nruntime: python\nentrypoint: agent.py\nmodel:\n  default: mock-llm\n"
        "tools:\n  - id: web.fetch\n    permission: allowed\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n    return {}\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()

    with _serving(H) as port:
        _allow_localhost(tmp_path, monkeypatch, port)
        run = {"id": "r", "journal": {}, "trace": []}
        ctx = RuntimeContext(store=store, manifest=manifest, run=run, tools=default_registry(),
                             models=models_registry(), project_root=tmp_path, agent=agent)
        out = asyncio.run(ctx.llm.run(input={"url": f"http://127.0.0.1:{port}/"}, tools=["web.fetch"]))

    # the model chose web.fetch, the runtime really fetched, and real content came back
    assert out["toolCalls"][0]["tool"] == "web.fetch"
    assert "Hello" in out["toolCalls"][0]["result"]["text"]
