"""Phase B (item 7): real tool execution + Slack inbound adapter."""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


def _engine(tmp_path, agent_body, manifest_body):
    (tmp_path / "rya.agent.yaml").write_text(manifest_body)
    (tmp_path / "agent.py").write_text(agent_body)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    return Engine(manifest, agent, Store(tmp_path), tmp_path)


def test_agent_defined_tool_executes(tmp_path):
    engine = _engine(tmp_path,
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.tool('math.double')\n"
        "async def double(input):\n"
        "    return {'result': input['n'] * 2}\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    out = await ctx.tools.call('math.double', {'n': 21})\n"
        "    ctx.logs.info('doubled', value=out['result'])\n"
        "    return out\n",
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: math.double\n    permission: allowed\n")
    run = engine.run_event("x", {})
    tc = next(e for e in run["trace"] if e["kind"] == "tool.call")
    assert tc["data"]["impl"] == "agent"
    assert tc["data"]["result"]["result"] == 42


def test_http_tool_calls_real_endpoint(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            payload = json.loads(self.rfile.read(n))
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"echo": payload, "ok": True}).encode())
        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.handle_request, daemon=True).start()

    engine = _engine(tmp_path,
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    return await ctx.tools.call('remote.echo', {'hi': 'there'})\n",
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        f"tools:\n  - id: remote.echo\n    permission: allowed\n    url: http://127.0.0.1:{port}\n")
    run = engine.run_event("x", {})
    srv.server_close()
    tc = next(e for e in run["trace"] if e["kind"] == "tool.call")
    assert tc["data"]["impl"] == "http"
    assert tc["data"]["result"]["echo"] == {"hi": "there"}


def _slack_sign(secret, ts, raw):
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{raw}".encode(), hashlib.sha256).hexdigest()


def test_slack_inbound_adapter(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rya.api.app import build_app
    from rya.cli import scaffold

    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_MULTITENANT", raising=False)
    monkeypatch.setenv("RYA_SLACK_SIGNING_SECRET", "shhh")
    scaffold.write_project(tmp_path, "slackbot", template="demo")
    c = TestClient(build_app(tmp_path))

    # url_verification handshake (signed).
    body = json.dumps({"type": "url_verification", "challenge": "abc123"})
    ts = "1700000000"
    r = c.post("/slack/events", content=body,
               headers={"x-slack-request-timestamp": ts, "x-slack-signature": _slack_sign("shhh", ts, body)})
    assert r.status_code == 200 and r.json()["challenge"] == "abc123"

    # bad signature rejected.
    r = c.post("/slack/events", content=body,
               headers={"x-slack-request-timestamp": ts, "x-slack-signature": "v0=bad"})
    assert r.status_code == 401

    # event_callback → a real run.
    ev = json.dumps({"type": "event_callback", "event": {"type": "message", "text": "hi", "user": "U1"}})
    r = c.post("/slack/events", content=ev,
               headers={"x-slack-request-timestamp": ts, "x-slack-signature": _slack_sign("shhh", ts, ev)})
    assert r.status_code == 200 and r.json()["runId"].startswith("run_")
