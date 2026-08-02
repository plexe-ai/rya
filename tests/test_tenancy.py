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

    # Tenant A fires a webhook -> a run in A. Since D21 the multi-tenant api
    # never runs a handler, so the webhook QUEUES the run and a worker executes
    # it — which this test now has to be.
    run_a = c.post("/inbound", json={"email": "a@y.com"},
                   headers={"Authorization": f"Bearer {key_a}"}).json()
    assert run_a["status"] == "queued" and run_a["runId"]
    _drain_workspace(tmp_path, a)
    assert c.get(f"/runs/{run_a['runId']}", headers={"Authorization": f"Bearer {key_a}"}
                 ).json()["status"] == "waiting_approval"

    # Tenant B lists runs -> does NOT see A's run.
    b_runs = c.get("/agents/mt-agent/runs", headers={"Authorization": f"Bearer {key_b}"}).json()["runs"]
    assert all(r["id"] != run_a["runId"] for r in b_runs)
    assert b_runs == []

    # Tenant A sees its own.
    a_runs = c.get("/agents/mt-agent/runs", headers={"Authorization": f"Bearer {key_a}"}).json()["runs"]
    assert any(r["id"] == run_a["runId"] for r in a_runs)


def _drain_workspace(root, workspace_id: str) -> None:
    """Be the execution plane for one workspace.

    The multi-tenant api writes a `queued` run and enqueues it; nothing in that
    process will ever execute it (D13/D21). A test that wants a finished run has
    to stand in for `rya worker`.
    """
    from rya import turns
    from rya.manifest import load_manifest
    from rya.runtime import Engine, load_agent
    from rya.store_postgres import PostgresStore

    manifest = load_manifest(root / "rya.agent.yaml")
    engine = Engine(manifest, load_agent(manifest, root),
                    PostgresStore(PG, workspace_id), root)
    turns.execute_pending(engine, worker_id="test-worker")
    turns.execute_resumes(engine, worker_id="test-worker")


def test_workspace_membership_invite_and_claim(tmp_path, monkeypatch):
    """Team access: owner invites an email; the teammate signs up later, lands
    in the shared workspace, and can mint their own key for it."""
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
        cur.execute("TRUNCATE rya_workspace_members, rya_api_keys, rya_users CASCADE")
        cur.execute("TRUNCATE rya_workspaces CASCADE")

    scaffold.write_project(tmp_path, "team-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    # Owner signs up -> workspace + session.
    owner = client.post("/v1/signup", json={"email": "owner@csa.test",
                                            "password": "password123",
                                            "workspaceName": "ChatStudyAbroad"}).json()
    ws_id = owner["workspace"]["id"]
    oh = {"Authorization": "Bearer " + owner["token"]}

    # Owner invites a teammate who has NO account yet.
    r = client.post(f"/v1/workspaces/{ws_id}/members", json={"email": "teammate@csa.test"},
                    headers=oh)
    assert r.status_code == 200 and r.json()["claimed"] is False

    # Teammate signs up -> the invite is claimed; shared workspace is listed
    # as 'member' next to their own.
    mate = client.post("/v1/signup", json={"email": "teammate@csa.test",
                                           "password": "password123",
                                           "workspaceName": "My own"}).json()
    login = client.post("/v1/login", json={"email": "teammate@csa.test",
                                           "password": "password123"}).json()
    roles = {w["id"]: w["role"] for w in login["workspaces"]}
    assert roles[ws_id] == "member"
    assert roles[mate["workspace"]["id"]] == "owner"

    # Teammate mints a key for the SHARED workspace and uses it.
    mh = {"Authorization": "Bearer " + login["token"]}
    k = client.post(f"/v1/workspaces/{ws_id}/keys", json={}, headers=mh).json()
    assert k["apiKey"].startswith("rya_sk_")
    tools = client.get("/tools", headers={"Authorization": "Bearer " + k["apiKey"]})
    assert tools.status_code == 200

    # A member cannot invite; a stranger cannot mint a key.
    r = client.post(f"/v1/workspaces/{ws_id}/members", json={"email": "x@y.test"}, headers=mh)
    assert r.status_code == 403
    stranger = client.post("/v1/signup", json={"email": "stranger@nowhere.test",
                                               "password": "password123"}).json()
    sh = {"Authorization": "Bearer " + stranger["token"]}
    assert client.post(f"/v1/workspaces/{ws_id}/keys", json={}, headers=sh).status_code == 403

    # Owner sees the member list with claim state.
    members = client.get(f"/v1/workspaces/{ws_id}/members", headers=oh).json()["members"]
    assert members[0]["email"] == "teammate@csa.test" and members[0]["claimed"] is True


def test_key_management_removal_and_password(tmp_path, monkeypatch):
    """Owner lists/revokes keys; removing a member revokes their keys; change
    password requires the current one and takes effect immediately."""
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
        cur.execute("TRUNCATE rya_workspace_members, rya_api_keys, rya_users CASCADE")
        cur.execute("TRUNCATE rya_workspaces CASCADE")

    scaffold.write_project(tmp_path, "mgmt-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    owner = client.post("/v1/signup", json={"email": "own@csa.test", "password": "password123",
                                            "workspaceName": "W"}).json()
    ws, oh = owner["workspace"]["id"], {"Authorization": "Bearer " + owner["token"]}

    client.post(f"/v1/workspaces/{ws}/members", json={"email": "m@csa.test"}, headers=oh)
    mate = client.post("/v1/signup", json={"email": "m@csa.test", "password": "password123"}).json()
    mh = {"Authorization": "Bearer " + mate["token"]}
    mate_key = client.post(f"/v1/workspaces/{ws}/keys", json={}, headers=mh).json()["apiKey"]
    assert client.get("/tools", headers={"Authorization": "Bearer " + mate_key}).status_code == 200

    # owner sees key metadata (values never listed) and can revoke one
    keys = client.get(f"/v1/workspaces/{ws}/keys", headers=oh).json()["keys"]
    assert all("key" not in k and "hash" not in str(k).lower() for k in keys)
    assert any(k["label"] == "m@csa.test" for k in keys)

    # removing the member revokes their minted key -> access actually gone
    r = client.delete(f"/v1/workspaces/{ws}/members/m@csa.test", headers=oh).json()
    assert r["removed"] is True and r["keysRevoked"] == 1
    assert client.get("/tools", headers={"Authorization": "Bearer " + mate_key}).status_code == 401
    login = client.post("/v1/login", json={"email": "m@csa.test", "password": "password123"}).json()
    assert all(w["id"] != ws for w in login["workspaces"])

    # password change: wrong current rejected; new one takes effect
    assert client.post("/v1/password", json={"current": "wrong", "new": "newpassword1"},
                       headers=oh).status_code == 401
    assert client.post("/v1/password", json={"current": "password123", "new": "newpassword1"},
                       headers=oh).json()["ok"] is True
    assert client.post("/v1/login", json={"email": "own@csa.test",
                                          "password": "password123"}).status_code == 401
    assert client.post("/v1/login", json={"email": "own@csa.test",
                                          "password": "newpassword1"}).json()["ok"] is True


# --------------------------------------------------------------------------- #
# #5 — the rya_worker role, and a load-bearing --workspace
#
# `worker_dsn()` and the rya_worker role existed with ZERO callers, so the
# execution plane connected as a superuser: superusers BYPASS row-level security,
# which made every policy above decorative for exactly the process that imports
# tenant code. These are the assertions the Phase 1 exit criteria ask for.
# --------------------------------------------------------------------------- #
def test_the_worker_role_is_subject_to_rls(tenancy):
    """A worker connection sees one tenant on an unfiltered SELECT — the
    superuser it replaced saw both."""
    import psycopg
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn

    t, app_dsn = tenancy
    a = t.create_workspace("wrk-a")["id"]
    b = t.create_workspace("wrk-b")["id"]
    PostgresStore(app_dsn, a).save_run({"id": "wr_a", "agent": "x", "createdAt": "t",
                                        "trace": [], "journal": {}})
    PostgresStore(app_dsn, b).save_run({"id": "wr_b", "agent": "x", "createdAt": "t",
                                        "trace": [], "journal": {}})

    with psycopg.connect(worker_dsn(PG), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.workspace_id', %s, false)", (a,))
        cur.execute("SELECT id FROM rya_runs")          # no WHERE clause at all
        assert {r[0] for r in cur.fetchall()} == {"wr_a"}

    # The superuser DSN this replaces sees BOTH — which is the whole point.
    with psycopg.connect(PG, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.workspace_id', %s, false)", (a,))
        cur.execute("SELECT id FROM rya_runs WHERE id IN ('wr_a','wr_b')")
        assert {r[0] for r in cur.fetchall()} == {"wr_a", "wr_b"}


def test_a_worker_scoped_store_cannot_read_or_write_another_workspace(tenancy):
    """The exit criterion, through the store API rather than raw SQL."""
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn

    t, app_dsn = tenancy
    a = t.create_workspace("wsr-a")["id"]
    b = t.create_workspace("wsr-b")["id"]
    PostgresStore(app_dsn, b).save_run({"id": "only_b", "agent": "x", "createdAt": "t",
                                        "trace": [], "journal": {}})

    worker_a = PostgresStore(worker_dsn(PG), workspace_id=a)
    assert worker_a.get_run("only_b") is None
    assert worker_a.list_runs() == []

    # A write from A lands in A, and is invisible to a worker scoped to B.
    worker_a.save_run({"id": "from_a", "agent": "x", "createdAt": "t",
                       "trace": [], "journal": {}})
    assert PostgresStore(worker_dsn(PG), workspace_id=b).get_run("from_a") is None
    assert worker_a.get_run("from_a")["id"] == "from_a"


def test_the_worker_role_cannot_write_governance_tables(tenancy):
    """"Read the verdict, never write it." A bug in worker-side policy code must
    not be able to escalate into a policy WRITE, an environment repoint or a
    forged version."""
    import psycopg
    from rya.tenancy import _GOVERNANCE_TABLES, worker_dsn

    t, _ = tenancy
    ws = t.create_workspace("gov")["id"]

    for tbl in _GOVERNANCE_TABLES:
        with psycopg.connect(worker_dsn(PG), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.workspace_id', %s, false)", (ws,))
            # SELECT is granted...
            cur.execute(f"SELECT count(*) FROM {tbl}")
            assert cur.fetchone()[0] >= 0
        # ...INSERT, UPDATE and DELETE are not.
        for stmt in (f"INSERT INTO {tbl} (workspace_id) VALUES ('{ws}')",
                     f"UPDATE {tbl} SET workspace_id='{ws}'",
                     f"DELETE FROM {tbl}"):
            with psycopg.connect(worker_dsn(PG), autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT set_config('app.workspace_id', %s, false)", (ws,))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(stmt)


def test_the_worker_role_can_still_write_run_data(tenancy):
    """The restriction is governance-only: refusing ordinary run writes would
    just be a broken worker."""
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn

    t, _ = tenancy
    ws = t.create_workspace("rw")["id"]
    store = PostgresStore(worker_dsn(PG), workspace_id=ws)
    store.save_run({"id": "ok_1", "agent": "x", "createdAt": "t", "trace": [], "journal": {}})
    assert store.get_run("ok_1")["id"] == "ok_1"


def test_open_worker_store_refuses_a_key_store_workspace_mismatch(tmp_path, monkeypatch):
    """`preflight` fails closed when the key and the store disagree about the
    tenant. Without this the worker stamps rows for one tenant into another's
    scope, silently, because each half is individually consistent."""
    from rya.errors import RyaError
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn
    from rya.worker import Worker, WorkerKey

    class _FakeEngine:
        def __init__(self, store):
            self.store = store
            self.agent = None
            self.manifest = None

    store = PostgresStore(worker_dsn(PG), workspace_id="ws_real")
    # Built through the constructor, not `__new__`: since D27 a Worker holds its own
    # store rather than reaching through an engine (the fork claimer has no engine),
    # and this check is about the store the WORKER writes with.
    w = Worker(_FakeEngine(store), WorkerKey(workspace="ws_claimed", agent="a"))
    w.advertise = lambda: {}            # no bundle needed; preflight is the subject
    with pytest.raises(RyaError) as exc:
        w.preflight()
    assert exc.value.code == "E_WORKSPACE_MISMATCH"


def test_no_row_disagrees_about_its_own_workspace(tenancy):
    """Exit criterion: the JSONB payload and the column must never disagree, or
    an app-layer read and an RLS-layer read return different answers."""
    import psycopg
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn

    t, app_dsn = tenancy
    ws = t.create_workspace("consistency")["id"]
    PostgresStore(app_dsn, ws).save_run({"id": "c_1", "agent": "x", "createdAt": "t",
                                         "trace": [], "journal": {}})
    from rya.worker import WorkerKey, pin_run
    store = PostgresStore(worker_dsn(PG), workspace_id=ws)
    store.save_run(pin_run({"id": "c_2", "agent": "x", "createdAt": "t",
                            "trace": [], "journal": {}}, WorkerKey(workspace=ws, agent="x")))

    with psycopg.connect(PG, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, workspace_id, data->>'workspaceId'
                       FROM rya_runs
                       WHERE data->>'workspaceId' IS NOT NULL
                         AND data->>'workspaceId' <> workspace_id""")
        assert cur.fetchall() == []


# --------------------------------------------------------------------------- #
# #20 / D29 — the organization layer (additive and inert)
# --------------------------------------------------------------------------- #
def test_every_workspace_gets_an_organization(tenancy):
    t, _ = tenancy
    ws = t.create_workspace("acme")
    assert ws["orgId"]
    assert [w["orgId"] for w in t.list_workspaces() if w["id"] == ws["id"]] == [ws["orgId"]]
    assert any(o["id"] == ws["orgId"] and o["workspaces"] == 1 for o in t.list_organizations())


def test_the_org_backfill_is_idempotent(tenancy):
    """`setup()` runs on every boot, so a backfill that minted a fresh org each
    time would multiply orgs per workspace on restart."""
    import psycopg

    t, _ = tenancy
    t.create_workspace("one")
    t.create_workspace("two")
    before = {o["id"] for o in t.list_organizations()}

    # Simulate a pre-D29 row: an existing workspace with no org.
    with psycopg.connect(PG, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rya_workspaces (id, name, created_at) VALUES "
                    "('ws_legacy', 'legacy', 't')")

    t.setup()                      # the boot path that backfills
    after = {o["id"] for o in t.list_organizations()}
    assert len(after) == len(before) + 1        # exactly one new org, for ws_legacy
    assert all(w["orgId"] for w in t.list_workspaces())

    t.setup()                      # and again — nothing further is created
    assert {o["id"] for o in t.list_organizations()} == after


def test_many_workspaces_can_share_one_org(tenancy):
    """D29's point: the BILLING boundary can span workspaces while the ISOLATION
    boundary does not move."""
    from rya.store_postgres import PostgresStore
    from rya.tenancy import worker_dsn

    t, app_dsn = tenancy
    a = t.create_workspace("dept-a")
    b = t.create_workspace("dept-b")
    org = t.create_organization("one-invoice")
    t.assign_workspace_to_org(a["id"], org["id"])
    t.assign_workspace_to_org(b["id"], org["id"])

    assert any(o["id"] == org["id"] and o["workspaces"] == 2 for o in t.list_organizations())

    # Sharing an org changes nothing about isolation.
    PostgresStore(app_dsn, a["id"]).save_run({"id": "org_a", "agent": "x",
                                              "createdAt": "t", "trace": [], "journal": {}})
    assert PostgresStore(worker_dsn(PG), workspace_id=b["id"]).get_run("org_a") is None


def test_assigning_to_an_unknown_org_is_refused(tenancy):
    from rya.errors import RyaError

    t, _ = tenancy
    ws = t.create_workspace("x")
    with pytest.raises(RyaError):
        t.assign_workspace_to_org(ws["id"], "org_nope")
