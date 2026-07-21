"""Keyless Governance Adapter (Workstream E / Acceptance Criteria 1-3).

The Application is keyless: no provider key, only a governance-minted Platform
Token, and it fails closed when governance is unavailable. These tests pin that
contract — a regression here is a contract breach, so they run in CI.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rya.errors import RyaError
from rya.manifest.schema import ModelBlock
from rya.providers import chat as provider_chat
from rya.providers import resolve_provider
from rya.providers import respond as provider_respond

_PROVIDER_ENV = ("RYA_KEYLESS", "RYA_GOVERNANCE_URL", "RYA_PLATFORM_TOKEN",
                 "RYA_ADAPTER_MODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    yield


class _Gov(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path == "/revoked":
            self.send_response(401); self.end_headers(); self.wfile.write(b"{}"); return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(403); self.end_headers(); self.wfile.write(b"{}"); return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for t in ["Best ", "for ", "your ", "trip."]:
                self.wfile.write(f"data: {json.dumps({'type': 'text_delta', 'text': t})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'type': 'message_done', 'usage': {'input': 11, 'output': 4}})}\n\n".encode())
            return
        if body.get("purpose") == "tools":
            out = {"text": "", "tool_calls": [{"id": "c1", "name": "search_rates", "input": {"location": "LAX"}}],
                   "usage": {"input": 20, "output": 6}}
        else:
            out = {"text": "Enterprise RAV4, $341 all-in.", "usage": {"input": 9, "output": 8}}
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(out).encode())


@pytest.fixture(scope="module")
def governance():
    srv = HTTPServer(("127.0.0.1", 0), _Gov)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_manifest_accepts_adapter_provider():
    assert ModelBlock(provider="adapter").provider == "adapter"
    with pytest.raises(Exception):
        ModelBlock(provider="bogus")


def test_keyless_mode_blocks_a_leaked_provider_key(monkeypatch):
    monkeypatch.setenv("RYA_KEYLESS", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked")
    with pytest.raises(RyaError) as ei:
        resolve_provider("auto")
    assert ei.value.code == "E_KEYLESS_VIOLATION"


def test_keyless_mode_forces_the_adapter(monkeypatch):
    monkeypatch.setenv("RYA_KEYLESS", "1")
    assert resolve_provider("auto") == "adapter"
    assert resolve_provider("anthropic") == "adapter"
    assert resolve_provider("mock") == "mock"  # mock holds no key; still allowed for dev/tests


def test_fail_closed_without_url_or_token(monkeypatch, governance):
    with pytest.raises(RyaError) as ei:
        provider_respond(system="hi", input={}, provider="adapter")
    assert ei.value.code == "E_GOVERNANCE_UNAVAILABLE"  # no URL
    monkeypatch.setenv("RYA_GOVERNANCE_URL", governance + "/infer")
    with pytest.raises(RyaError) as ei:
        provider_respond(system="hi", input={}, provider="adapter")
    assert ei.value.code == "E_GOVERNANCE_UNAVAILABLE"  # no token


def test_fail_closed_when_governance_unavailable(monkeypatch, governance):
    monkeypatch.setenv("RYA_GOVERNANCE_URL", governance + "/infer")
    monkeypatch.setenv("RYA_PLATFORM_TOKEN", "pt_test")
    monkeypatch.setenv("RYA_ADAPTER_MODE", "unavailable")
    with pytest.raises(RyaError) as ei:
        provider_respond(system="hi", input={}, provider="adapter")
    assert ei.value.code == "E_GOVERNANCE_UNAVAILABLE"


def test_fail_closed_on_revoked_token(monkeypatch, governance):
    monkeypatch.setenv("RYA_GOVERNANCE_URL", governance + "/revoked")
    monkeypatch.setenv("RYA_PLATFORM_TOKEN", "pt_test")
    with pytest.raises(RyaError) as ei:
        provider_respond(system="hi", input={}, provider="adapter")
    assert ei.value.code == "E_GOVERNANCE_UNAVAILABLE"


def test_happy_path_respond_and_chat(monkeypatch, governance):
    monkeypatch.setenv("RYA_GOVERNANCE_URL", governance + "/infer")
    monkeypatch.setenv("RYA_PLATFORM_TOKEN", "pt_test")

    r = provider_respond(system="recommend", input={"trip": "LAX"}, provider="adapter")
    assert r["text"] == "Enterprise RAV4, $341 all-in."
    assert r["provider"] == "adapter"
    assert r["usage"]["output"] == 8

    chunks = []
    rs = provider_respond(system="recommend", input={}, provider="adapter", on_token=chunks.append)
    assert rs["text"] == "Best for your trip."
    assert len(chunks) == 4

    c = provider_chat(messages=[{"role": "user", "content": {"q": "cars at LAX"}}],
                      tools=[{"name": "search_rates", "input_schema": {"type": "object"}}],
                      system="find a car", provider="adapter")
    assert c["toolCalls"][0]["name"] == "search_rates"
