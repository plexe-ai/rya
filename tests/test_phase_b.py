"""Phase B (items 5 + 6): workers/queue/timeouts, JWT identity."""

import base64
import hashlib
import hmac
import json

import pytest

from rya.auth import Identity, verify_jwt
from rya.errors import RyaError
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


def _agent(tmp_path, body, manifest_extra=""):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: t\nruntime: python\nentrypoint: agent.py\n" + manifest_extra
    )
    (tmp_path / "agent.py").write_text(body)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    return Engine(manifest, agent, Store(tmp_path), tmp_path)


def _mint(claims, secret):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    h, p = b64({"alg": "HS256", "typ": "JWT"}), b64(claims)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{sig}"


# ---- item 5: timeout ------------------------------------------------------
def test_run_times_out(tmp_path):
    engine = _agent(tmp_path,
        "import asyncio\n"
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    await asyncio.sleep(5)\n",
        manifest_extra="timeout_seconds: 1\n")
    run = engine.run_event("x", {})
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_TIMEOUT"


# ---- item 5: claim queue + worker ----------------------------------------
def test_claim_queue_and_worker(tmp_path):
    engine = _agent(tmp_path,
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    await ctx.jobs.schedule('work', {'n': 1})\n"
        "    await ctx.jobs.schedule('work', {'n': 2})\n"
        "@agent.job('work')\n"
        "async def work(ctx, job):\n"
        "    return job.payload\n")
    engine.run_event("x", {})

    # Two distinct claims, then nothing left — no double-claim.
    j1 = engine.store.claim_due_job()
    j2 = engine.store.claim_due_job()
    assert j1 and j2 and j1["id"] != j2["id"]
    assert engine.store.claim_due_job() is None

    # Worker drains due jobs (re-create a run to enqueue fresh ones).
    engine.run_event("x", {})
    ran = engine.work_once()
    assert len(ran) == 2 and all(r["status"] == "completed" for r in ran)


# ---- item 6: JWT verification --------------------------------------------
def test_jwt_verify_hs256():
    secret = "topsecret"
    tok = _mint({"sub": "user_42", "email": "ada@x.com"}, secret)
    ident = verify_jwt(tok, {"RYA_JWT_SECRET": secret})
    assert ident.sub == "user_42" and ident.email == "ada@x.com"


def test_jwt_bad_signature_rejected():
    tok = _mint({"sub": "u"}, "right")
    with pytest.raises(RyaError) as exc:
        verify_jwt(tok, {"RYA_JWT_SECRET": "wrong"})
    assert exc.value.code == "E_UNAUTHORIZED"


def test_jwt_expired_rejected():
    tok = _mint({"sub": "u", "exp": 1}, "s")  # exp in 1970
    with pytest.raises(RyaError) as exc:
        verify_jwt(tok, {"RYA_JWT_SECRET": "s"})
    assert "expired" in exc.value.message.lower()


# ---- item 6: identity into ctx + per-user memory scope -------------------
def test_per_user_memory_isolation(tmp_path):
    engine = _agent(tmp_path,
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    await ctx.memory.set('note', e.payload['note'], scope='user')\n"
        "    assert ctx.identity is not None\n")
    engine.run_event("x", {"note": "ada-note"}, identity=Identity("user_a", {"email": "a"}))
    engine.run_event("x", {"note": "ben-note"}, identity=Identity("user_b", {"email": "b"}))

    assert engine.store.load_memory("user:user_a")["kv"]["note"] == "ada-note"
    assert engine.store.load_memory("user:user_b")["kv"]["note"] == "ben-note"


# ---- item 6: server enforces JWT -----------------------------------------
def test_server_requires_jwt_when_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rya.api.app import build_app
    from rya.cli import scaffold

    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_MULTITENANT", raising=False)
    monkeypatch.setenv("RYA_JWT_SECRET", "serversecret")
    scaffold.write_project(tmp_path, "jwt-agent", template="demo")
    c = TestClient(build_app(tmp_path))

    assert c.post("/agents/_/events", json={"payload": {"email": "a@b.com"}}).status_code == 401
    tok = _mint({"sub": "user_9", "email": "nine@x.com"}, "serversecret")
    r = c.post("/agents/_/events", json={"payload": {"email": "a@b.com"}},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["identity"]["sub"] == "user_9"
