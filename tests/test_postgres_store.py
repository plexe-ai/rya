"""Postgres store + cross-process durability.

Skipped unless RYA_TEST_DATABASE_URL points at a reachable Postgres, e.g.:
    docker run -d -e POSTGRES_PASSWORD=rya -e POSTGRES_DB=rya -p 55432:5432 postgres:16-alpine
    RYA_TEST_DATABASE_URL=postgresql://postgres:rya@localhost:55432/rya pytest -q
"""

import os

import pytest

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent

PG = os.environ.get("RYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG, reason="set RYA_TEST_DATABASE_URL to run Postgres tests")


def _fresh_store():
    """A NEW store instance == a simulated process restart (no in-memory carryover)."""
    from rya.store_postgres import PostgresStore

    store = PostgresStore(PG)
    store.ensure()
    # Clean tables so the test is isolated.
    with store._conn.cursor() as cur:
        cur.execute("TRUNCATE rya_runs, rya_approvals, rya_jobs, rya_memory")
    return store


def _engine(project, store):
    manifest = load_manifest(project / "rya.agent.yaml")
    agent = load_agent(manifest, project)
    return Engine(manifest, agent, store, project)


def test_pause_resume_survives_restart(tmp_path):
    scaffold.write_project(tmp_path, "pg-agent", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)

    # "Process 1": trigger, pause for approval.
    store1 = _fresh_store()
    run = Engine(manifest, agent, store1, tmp_path).run_event("message.received", {"email": "ada@x.com"})
    assert run["status"] == "waiting_approval"
    approval_id = run["pendingApproval"]

    # "Process 2": brand-new store instance (restart). Approve -> resume from Postgres.
    from rya.store_postgres import PostgresStore

    store2 = PostgresStore(PG)
    store2.ensure()
    resumed = Engine(manifest, agent, store2, tmp_path).approve(approval_id)
    assert resumed["status"] == "completed"

    # "Process 3": another fresh store sees the persisted, completed run.
    store3 = PostgresStore(PG)
    store3.ensure()
    assert store3.get_run(run["id"])["status"] == "completed"
    assert store3.describe()["backend"] == "postgres"


def test_memory_persists_in_postgres(tmp_path):
    scaffold.write_project(tmp_path, "pg-mem", template="demo")
    store = _fresh_store()
    store.save_memory("agent", {"kv": {"k": "v"}, "collections": {"c": [{"x": 1}]}})

    from rya.store_postgres import PostgresStore

    fresh = PostgresStore(PG)
    fresh.ensure()
    mem = fresh.load_memory("agent")
    assert mem["kv"]["k"] == "v"
    assert "agent" in fresh.list_memory_scopes()
