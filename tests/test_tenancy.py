"""Multi-tenancy: API keys, app-layer isolation, and DB-enforced RLS.

Gated on RYA_TEST_DATABASE_URL (an admin/superuser Postgres DSN — it provisions
the rya_app role + RLS policies).
"""

import os

import pytest

PG = os.environ.get("RYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG, reason="set RYA_TEST_DATABASE_URL to run tenancy tests")


@pytest.fixture
def tenancy():
    import psycopg
    from rya.tenancy import Tenancy

    t = Tenancy(PG)
    app_dsn = t.setup()
    # Clean slate.
    with psycopg.connect(PG, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE rya_runs, rya_approvals, rya_jobs, rya_memory")
        cur.execute("TRUNCATE rya_api_keys CASCADE")
        cur.execute("TRUNCATE rya_workspaces CASCADE")
    return t, app_dsn


def test_api_key_resolution(tenancy):
    t, _ = tenancy
    ws = t.create_workspace("acme")
    rec = t.create_api_key(ws["id"], "ci")
    assert rec["key"].startswith("rya_sk_")
    assert t.resolve_key(rec["key"]) == ws["id"]
    assert t.resolve_key("rya_sk_nonsense") is None


def test_self_serve_signup_login_workspace(tenancy):
    import pytest as _pytest
    from rya.errors import RyaError
    t, _ = tenancy
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE rya_users CASCADE")

    # signup → user + first workspace + an API key, all in one step
    res = t.signup("Ada@Acme.io", "supersecret1", "Acme")
    assert res["user"]["email"] == "ada@acme.io"             # normalized
    assert res["apiKey"].startswith("rya_sk_")
    assert t.resolve_key(res["apiKey"]) == res["workspace"]["id"]

    # password is hashed, never stored plaintext
    with psycopg.connect(PG, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT password_hash FROM rya_users WHERE email='ada@acme.io'")
        assert "supersecret1" not in cur.fetchone()[0]

    # login verifies credentials and lists the user's workspaces
    assert t.authenticate("ada@acme.io", "supersecret1")["id"] == res["user"]["id"]
    assert t.authenticate("ada@acme.io", "wrong") is None
    wss = t.list_user_workspaces(res["user"]["id"])
    assert [w["id"] for w in wss] == [res["workspace"]["id"]]

    # duplicate email is rejected
    with _pytest.raises(RyaError) as e:
        t.signup("ada@acme.io", "anotherpass1")
    assert e.value.code == "E_EMAIL_TAKEN"


def test_app_layer_isolation(tenancy):
    from rya.store_postgres import PostgresStore

    t, app_dsn = tenancy
    a = t.create_workspace("tenant-a")["id"]
    b = t.create_workspace("tenant-b")["id"]

    store_a = PostgresStore(app_dsn, a)
    store_a.ensure()
    run = {"id": "run_a1", "agent": "x", "createdAt": "2026-01-01T00:00:00Z", "trace": [], "journal": {}}
    store_a.save_run(run)

    store_b = PostgresStore(app_dsn, b)
    # Workspace B cannot see workspace A's run.
    assert store_b.get_run("run_a1") is None
    assert store_b.list_runs() == []
    # Workspace A can.
    assert store_a.get_run("run_a1")["id"] == "run_a1"


def test_rls_enforced_by_database(tenancy):
    """The killer test: even a raw `SELECT *` (no app filter) only sees the
    current workspace's rows, because Postgres RLS enforces it for rya_app."""
    import psycopg
    from rya.store_postgres import PostgresStore

    t, app_dsn = tenancy
    a = t.create_workspace("rls-a")["id"]
    b = t.create_workspace("rls-b")["id"]
    PostgresStore(app_dsn, a).save_run({"id": "r_a", "agent": "x", "createdAt": "t", "trace": [], "journal": {}})
    PostgresStore(app_dsn, b).save_run({"id": "r_b", "agent": "x", "createdAt": "t", "trace": [], "journal": {}})

    # Connect as the non-superuser rya_app role, scope to A, run an UNFILTERED query.
    with psycopg.connect(app_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.workspace_id', %s, false)", (a,))
        cur.execute("SELECT id FROM rya_runs")  # no WHERE clause at all
        ids = {r[0] for r in cur.fetchall()}
    assert ids == {"r_a"}  # RLS hid r_b at the database layer


def test_per_user_rls_enforced_by_database(tenancy):
    """RLS-as-the-user: within ONE workspace, a user cannot see another user's
    runs — even on an unfiltered SELECT — because Postgres scopes on app.user_id."""
    import psycopg
    from rya.store_postgres import PostgresStore

    t, app_dsn = tenancy
    ws = t.create_workspace("acme")["id"]

    store_a = PostgresStore(app_dsn, ws, user_id="user_a")
    store_a.ensure()
    store_a.save_run({"id": "r_a", "agent": "x", "createdAt": "t", "trace": [], "journal": {}})
    store_b = PostgresStore(app_dsn, ws, user_id="user_b")
    store_b.save_run({"id": "r_b", "agent": "x", "createdAt": "t", "trace": [], "journal": {}})

    # Same workspace, different users — A sees only its own run.
    assert store_a.get_run("r_b") is None
    assert {r["id"] for r in store_a.list_runs()} == {"r_a"}

    # Unfiltered raw query as the app role, scoped to user_a -> only r_a.
    with psycopg.connect(app_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.workspace_id', %s, false)", (ws,))
        cur.execute("SELECT set_config('app.user_id', 'user_a', false)")
        cur.execute("SELECT id FROM rya_runs")
        assert {r[0] for r in cur.fetchall()} == {"r_a"}


def test_multi_tenant_http_isolation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rya.api.app import build_app
    from rya.cli import scaffold
    from rya.tenancy import Tenancy
    import psycopg

    monkeypatch.setenv("RYA_DATABASE_URL", PG)
    monkeypatch.setenv("RYA_MULTITENANT", "1")

    t = Tenancy(PG)
    t.setup()
    with psycopg.connect(PG, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE rya_runs, rya_approvals, rya_jobs, rya_memory")
        cur.execute("TRUNCATE rya_api_keys CASCADE")
        cur.execute("TRUNCATE rya_workspaces CASCADE")
    a = t.create_workspace("http-a")["id"]
    b = t.create_workspace("http-b")["id"]
    key_a = t.create_api_key(a)["key"]
    key_b = t.create_api_key(b)["key"]

    scaffold.write_project(tmp_path, "mt-agent", template="demo")
    c = TestClient(build_app(tmp_path))

    # No key -> 401.
    assert c.post("/inbound", json={"email": "x@y.com"}).status_code == 401

    # Tenant A fires a webhook -> a run in A.
    run_a = c.post("/inbound", json={"email": "a@y.com"},
                   headers={"Authorization": f"Bearer {key_a}"}).json()
    assert run_a["status"] == "waiting_approval"

    # Tenant B lists runs -> does NOT see A's run.
    b_runs = c.get("/agents/mt-agent/runs", headers={"Authorization": f"Bearer {key_b}"}).json()["runs"]
    assert all(r["id"] != run_a["runId"] for r in b_runs)
    assert b_runs == []

    # Tenant A sees its own.
    a_runs = c.get("/agents/mt-agent/runs", headers={"Authorization": f"Bearer {key_a}"}).json()["runs"]
    assert any(r["id"] == run_a["runId"] for r in a_runs)
