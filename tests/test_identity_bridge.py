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


def _drain(root, key: str) -> None:
    """Be the execution plane.

    Since D21 the multi-tenant api records decisions and queues work; it never
    runs a handler. Both halves matter here: `/inbound` queues the run, and
    `/approve` records the approval and enqueues the RESUME. A test about who
    acted therefore has to drain both, exactly as `rya worker` does.
    """
    from rya import turns
    from rya.manifest import load_manifest
    from rya.runtime import Engine, load_agent
    from rya.store_postgres import PostgresStore
    from rya.tenancy import Tenancy

    workspace = Tenancy(PG).resolve_key(key)
    manifest = load_manifest(root / "rya.agent.yaml")
    engine = Engine(manifest, load_agent(manifest, root),
                    PostgresStore(PG, workspace), root)
    turns.execute_pending(engine, worker_id="test-worker")
    turns.execute_resumes(engine, worker_id="test-worker")


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
    assert run["status"] == "queued"
    _drain(tmp_path, key)
    apr = c.get("/approvals?status=pending",
                headers={"Authorization": f"Bearer {key}"}).json()["approvals"][0]

    out = c.post(f"/approvals/{apr['id']}/approve",
                 headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": ut}).json()
    # The actor is resolved and recorded by the CONTROL plane — that is the half
    # that needs the authenticated human — and the worker carries out the resume.
    assert out["resolvedBy"]["email"] == "sarah@bbg.bank"
    assert out["queued"] is True
    _drain(tmp_path, key)

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
    c.post("/inbound", json={"email": "x@y.com"},
           headers={"Authorization": f"Bearer {key}"})
    _drain(tmp_path, key)
    apr = c.get("/approvals?status=pending",
                headers={"Authorization": f"Bearer {key}"}).json()["approvals"][0]

    r = c.post(f"/approvals/{apr['id']}/approve", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E_APPROVER_IDENTITY_REQUIRED"

    # bad token also refused; approval stays pending
    r = c.post(f"/approvals/{apr['id']}/approve",
               headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": "garbage.token.x"})
    assert r.status_code == 401
    still = c.get("/approvals?status=pending",
                  headers={"Authorization": f"Bearer {key}"}).json()["approvals"]
    assert any(a["id"] == apr["id"] for a in still)


@needs_pg
def test_the_run_itself_carries_the_identity_in_multi_tenant(tmp_path, monkeypatch):
    """The third consumer of X-Rya-User-Token, which used to be unreachable.

    `_identity_from` opened with `if mt: return None`, so under multi-tenancy every
    `identity=` argument in `api/app.py` was None however the request was headed.
    Two of the three consumers still worked — `get_plane` read the header for
    per-user RLS and `_actor_from` read it for approval attribution — which is why
    the hole stayed hidden. The third is the *run's* Identity, and the SDK's
    `_authorize_connection` needs it: without a verified `sub` a `require_user` tool
    raises `E_NO_IDENTITY`, so those tools were unusable in MT from every client
    while the api was reading the header and dropping it.
    """
    c = _mt_client(tmp_path, monkeypatch)
    res = c.post("/v1/signup", json={"email": "dana@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()
    key, session = res["apiKey"], res["token"]
    ut = c.post("/v1/token", headers={"Authorization": f"Bearer {session}"}).json()["userToken"]

    out = c.post("/agents/mt-agent/events",
                 json={"type": "message.received", "payload": {"text": "hi"}},
                 headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": ut}).json()
    # Echoed back by the route, which reported `null` here for every MT caller before.
    assert out["identity"] is not None
    assert out["identity"]["email"] == "dana@bbg.bank"
    assert out["identity"]["sub"] == res["user"]["id"]

    # And it is on the durable record the worker will replay, not just the response —
    # which is what makes it survive the api/worker split (`turns.py` carries the
    # claims on the job, and the executing process rehydrates them).
    run = c.get(f"/runs/{out['runId']}",
                headers={"Authorization": f"Bearer {key}",
                         "X-Rya-User-Token": ut}).json()
    assert run["identity"]["email"] == "dana@bbg.bank"

    # Per-user RLS engaged on the same request: the row is owned, not shared.
    import psycopg
    with psycopg.connect(PG, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT owner FROM rya_runs WHERE id=%s", (out["runId"],))
        assert cur.fetchone()[0] == res["user"]["id"]


@needs_pg
def test_a_run_without_a_user_token_stays_anonymous_and_shared(tmp_path, monkeypatch):
    """The fix must not invent an identity. No header, no identity — and the row stays
    workspace-shared (`owner IS NULL`) so existing agent/system traffic is unaffected."""
    c = _mt_client(tmp_path, monkeypatch)
    key = c.post("/v1/signup", json={"email": "raj@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()["apiKey"]

    out = c.post("/agents/mt-agent/events",
                 json={"type": "message.received", "payload": {"text": "hi"}},
                 headers={"Authorization": f"Bearer {key}"}).json()
    assert out["identity"] is None

    import psycopg
    with psycopg.connect(PG, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT owner FROM rya_runs WHERE id=%s", (out["runId"],))
        assert cur.fetchone()[0] is None


@needs_pg
def test_an_invalid_user_token_is_refused_not_silently_ignored(tmp_path, monkeypatch):
    """A present-but-bad token must 401, never downgrade to anonymous.

    Treating an expired token as "no identity offered" would silently drop a run's
    attribution at the 12-hour mark instead of asking for a fresh one — and under
    per-user RLS it would also silently change which rows the caller can see.
    """
    c = _mt_client(tmp_path, monkeypatch)
    key = c.post("/v1/signup", json={"email": "mei@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()["apiKey"]

    for bad in ("garbage.token.x", issue_jwt("mei", ttl_seconds=-5)):
        r = c.post("/agents/mt-agent/events",
                   json={"type": "message.received", "payload": {}},
                   headers={"Authorization": f"Bearer {key}", "X-Rya-User-Token": bad})
        assert r.status_code == 401, bad
        assert r.json()["error"]["code"] == "E_UNAUTHORIZED"


@needs_pg
def test_governance_reports_a_kill_switch_on_the_multi_tenant_path(tmp_path, monkeypatch):
    """Audit §4.5 on the deployment that matters.

    The whole shape of that finding was that `rya dev` was honest and a deployed
    workspace was not: reader and writer only diverge once policy is store-backed.
    So the SQLite-ish file store agreeing is not the assertion worth having —
    `rya_policy_log` is a different table with a different projection, and the
    per-tool history is diffed out of it.

    Attribution comes along because this path can actually produce one: an api key
    resolves to `workspace:<id>`, and §12 risk 7 is that "who changed this" must be
    answerable. On the single-tenant path the actor is legitimately null.
    """
    c = _mt_client(tmp_path, monkeypatch)
    res = c.post("/v1/signup", json={"email": "ops@bbg.bank", "password": "renewals-2026x",
                                     "workspaceName": "BBG"}).json()
    key = res["apiKey"]
    auth = {"Authorization": f"Bearer {key}"}

    r = c.put("/tools/email.send/permission",
              json={"permission": "disabled", "reason": "incident 42"}, headers=auth)
    assert r.status_code == 200, r.text

    g = c.get("/console?agent=mt-agent", headers=auth).json()["governance"]
    assert [o["tool"] for o in g["switches"]["active"]] == ["email.send"]
    assert g["switches"]["error"] is None
    assert g["policy"]["toolsDenied"] == 1 and g["policy"]["toolsOverridden"] == 1

    hist = g["switches"]["history"]
    assert len(hist) == 1 and hist[0]["tool"] == "email.send"
    assert hist[0]["previous"] is None and hist[0]["permission"] == "disabled"
    assert hist[0]["reason"] == "incident 42"
    assert hist[0]["actor"], "a governance change on this path is attributable"
