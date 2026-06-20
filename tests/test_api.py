"""Live HTTP surface: webhook trigger + token auth + signature verification."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    scaffold.write_project(tmp_path, "api-agent")
    return TestClient(build_app(tmp_path)), tmp_path, monkeypatch


def test_healthz_is_public(client):
    c, _, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_webhook_triggers_real_run(client):
    c, _, _ = client
    r = c.post("/inbound", json={"email": "ada@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting_approval"
    assert body["pendingApproval"]


def test_webhook_to_approval_over_http(client):
    c, _, _ = client
    run = c.post("/inbound", json={"email": "ada@example.com"}).json()
    apr = run["pendingApproval"]
    # No token set -> control routes are open.
    done = c.post(f"/approvals/{apr}/approve")
    assert done.status_code == 200 and done.json()["runStatus"] == "completed"


def test_token_required_when_set(client):
    c, _, monkeypatch = client
    monkeypatch.setenv("RYA_TOKEN", "secret-token")
    # Control route without token -> 401.
    assert c.get("/agents/x").status_code == 401
    # With wrong token -> 401.
    assert c.get("/agents/x", headers={"Authorization": "Bearer nope"}).status_code == 401
    # With correct token -> 200.
    ok = c.get("/agents/x", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
    # Webhook stays public (signature layer is separate).
    assert c.post("/inbound", json={"email": "a@b.com"}).status_code == 200


def test_webhook_signature_enforced(client):
    c, _, monkeypatch = client
    monkeypatch.setenv("RYA_WEBHOOK_SECRET", "whsec")
    raw = json.dumps({"email": "ada@example.com"}).encode()
    good = "sha256=" + hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()

    # No signature -> 401.
    assert c.post("/inbound", content=raw).status_code == 401
    # Bad signature -> 401.
    assert c.post("/inbound", content=raw, headers={"X-Rya-Signature": "sha256=bad"}).status_code == 401
    # Correct signature -> 200.
    ok = c.post("/inbound", content=raw, headers={"X-Rya-Signature": good})
    assert ok.status_code == 200 and ok.json()["status"] == "waiting_approval"
