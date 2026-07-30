"""Session-to-JWT bridge: /v1/token mints, approvals record WHO acted."""
import pytest

from rya.auth import issue_jwt, verify_jwt
from rya.errors import RyaError


def test_issue_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("RYA_JWT_SECRET", "s3cret")
    tok = issue_jwt("user_1", email="sarah@bbg.bank")
    ident = verify_jwt(tok)
    assert ident.sub == "user_1" and ident.email == "sarah@bbg.bank"


def test_issue_requires_secret(monkeypatch):
    monkeypatch.delenv("RYA_JWT_SECRET", raising=False)
    with pytest.raises(RyaError) as e:
        issue_jwt("u")
    assert "RYA_JWT_SECRET" in str(e.value)


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setenv("RYA_JWT_SECRET", "s3cret")
    tok = issue_jwt("u", ttl_seconds=-5)
    with pytest.raises(RyaError):
        verify_jwt(tok)


# ---- end to end over HTTP (Postgres, like the tenancy tests) ---------------
import os

PG = os.environ.get("RYA_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="set RYA_TEST_DATABASE_URL to run Postgres turn tests")


def _mt_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rya.api.app import build_app
    from rya.cli import scaffold
    from rya.tenancy import Tenancy
    import psycopg

    monkeypatch.setenv("RYA_DATABASE_URL", PG)
    monkeypatch.setenv("RYA_MULTITENANT", "1")
    monkeypatch.setenv("RYA_JWT_SECRET", "bridge-test-secret")
    t = Tenancy(PG)
    t.setup()
    with psycopg.connect(PG, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE rya_runs, rya_approvals, rya_jobs, rya_memory")
        cur.execute("TRUNCATE rya_api_keys CASCADE")
        cur.execute("TRUNCATE rya_users, rya_workspace_members CASCADE")
        cur.execute("TRUNCATE rya_workspaces CASCADE")
    scaffold.write_project(tmp_path, "mt-agent", template="demo")
    return TestClient(build_app(tmp_path))


@needs_pg
def test_approval_records_who_acted(tmp_path, monkeypatch):
    c = _mt_client(tmp_path, monkeypatch)
    res = c.post("/v1/signup", json={"email": "sarah@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()
    key, session = res["apiKey"], res["token"]

    # the bridge: session -> short-lived user JWT
    ut = c.post("/v1/token", headers={"Authorization": f"Bearer {session}"}).json()["userToken"]

    run = c.post("/inbound", json={"email": "x@y.com"},
                 headers={"Authorization": f"Bearer {key}"}).json()
    assert run["status"] == "waiting_approval"
    apr = c.get("/approvals?status=pending",
                headers={"Authorization": f"Bearer {key}"}).json()["approvals"][0]

    out = c.post(f"/approvals/{apr['id']}/approve",
                 headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": ut}).json()
    assert out["resolvedBy"]["email"] == "sarah@bbg.bank"

    resolved = c.get("/approvals", headers={"Authorization": f"Bearer {key}"}).json()["approvals"]
    mine = next(a for a in resolved if a["id"] == apr["id"])
    assert mine["resolvedBy"] == {"sub": res["user"]["id"], "email": "sarah@bbg.bank"}

    # audit trail: the run's trace event carries the actor
    trace = c.get(f"/runs/{run['runId']}/trace",
                  headers={"Authorization": f"Bearer {key}"}).json()["trace"]
    ev = next(e for e in trace if e["kind"] == "approval.approved")
    assert ev["data"]["actor"]["email"] == "sarah@bbg.bank"


@needs_pg
def test_enforcement_blocks_anonymous_approval(tmp_path, monkeypatch):
    c = _mt_client(tmp_path, monkeypatch)
    monkeypatch.setenv("RYA_REQUIRE_APPROVER_IDENTITY", "1")
    res = c.post("/v1/signup", json={"email": "amit@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()
    key = res["apiKey"]
    run = c.post("/inbound", json={"email": "x@y.com"},
                 headers={"Authorization": f"Bearer {key}"}).json()
    apr = c.get("/approvals?status=pending",
                headers={"Authorization": f"Bearer {key}"}).json()["approvals"][0]

    r = c.post(f"/approvals/{apr['id']}/approve", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "E_APPROVER_IDENTITY_REQUIRED"

    # bad token also refused; approval stays pending
    r = c.post(f"/approvals/{apr['id']}/approve",
               headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": "garbage.token.x"})
    assert r.status_code == 401
    still = c.get("/approvals?status=pending",
                  headers={"Authorization": f"Bearer {key}"}).json()["approvals"]
    assert any(a["id"] == apr["id"] for a in still)
