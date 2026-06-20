"""An approved action executes through the REAL provider seam, not the mock."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


@contextmanager
def _serving(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


def test_approved_channel_action_really_delivers(tmp_path, monkeypatch):
    received = {"hit": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["hit"] = True
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    (tmp_path / "rya.agent.yaml").write_text(
        "name: a\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: email.send\n    permission: approval_required\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
        "    await ctx.approvals.request(title='Send', body='hi',\n"
        "        action={'tool':'email.send','input':{'to':'a@b.co','subject':'x','body':'hi'}})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)

    with _serving(H) as port:
        monkeypatch.setenv("RYA_CHANNEL_EMAIL_URL", f"http://127.0.0.1:{port}/")
        engine = Engine(manifest, agent, Store(tmp_path), tmp_path)
        run = engine.run_event("message.received", {"email": "a@b.co"})
        assert run["status"] == "waiting_approval"
        apr_id = run["pendingApproval"]
        resumed = engine.approve(apr_id)

    assert received["hit"] is True            # the real channel actually delivered
    appr = engine.store.get_approval(apr_id)
    assert appr["actionResult"]["provider"] == "webhook"   # real seam, not the mock email.send
    assert resumed["status"] == "completed"
