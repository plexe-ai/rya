"""Channel provider seam: mock fallback + real delivery over a local server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rya.errors import RyaError
from rya.providers.channels import active_channel_provider, send


def test_mock_fallback_when_unconfigured():
    assert active_channel_provider("slack", {}) == "mock"
    out = send("slack", {"text": "hi"}, env={})
    assert out["provider"] == "mock" and out["delivered"] is True


def test_provider_resolution():
    assert active_channel_provider("slack", {"SLACK_WEBHOOK_URL": "x"}) == "slack"
    assert active_channel_provider("email", {"RESEND_API_KEY": "x"}) == "resend"
    assert active_channel_provider("sms", {"RYA_CHANNEL_SMS_URL": "x"}) == "webhook"


def test_real_http_delivery_to_local_server():
    """A real outbound POST actually reaches an HTTP endpoint (no mocks)."""
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()

    out = send("slack", {"text": "hello from rya"},
               env={"SLACK_WEBHOOK_URL": f"http://127.0.0.1:{port}"})
    server.server_close()

    assert out["provider"] == "slack" and out["status"] == 200
    assert received["body"]["text"] == "hello from rya"  # the server actually got it


def test_email_requires_to_field():
    with pytest.raises(RyaError) as exc:
        send("email", {"body": "hi"}, env={"RESEND_API_KEY": "x"})
    assert exc.value.code == "E_VALIDATION"
