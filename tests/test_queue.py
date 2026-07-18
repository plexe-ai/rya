"""External-worker job queue: the primitive that lets a polyglot backend (e.g.
Sim's TypeScript workers) use Rya as its durable job queue.

Store-level tests run against BOTH backends: the file store always, Postgres when
RYA_TEST_DATABASE_URL is set (same convention as test_postgres_store.py).
"""

import os

import pytest
from fastapi.testclient import TestClient

from rya import queue as q
from rya.api.app import build_app
from rya.errors import RyaError
from rya.store import FileStore

PG = os.environ.get("RYA_TEST_DATABASE_URL")


@pytest.fixture(params=["file", "postgres"])
def store(request, tmp_path):
    if request.param == "file":
        s = FileStore(tmp_path)
        s.ensure()
        return s
    if not PG:
        pytest.skip("set RYA_TEST_DATABASE_URL to run Postgres queue tests")
    from rya.store_postgres import PostgresStore

    s = PostgresStore(PG)
    s.ensure()
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE rya_queue")
    return s


# ---- core lifecycle -------------------------------------------------------

def test_enqueue_claim_complete(store):
    job = q.enqueue(store, "workflow-execution", {"workflowId": "wf1"}, max_attempts=3)
    assert job["status"] == "pending"

    claimed = q.claim(store, "worker-1", types=["workflow-execution"])
    assert len(claimed) == 1
    got = claimed[0]
    assert got["id"] == job["id"]
    assert got["status"] == "running"
    assert got["workerId"] == "worker-1"
    assert got["attempts"] == 1

    done = q.complete(store, job["id"], "worker-1", output={"ok": True})
    assert done["status"] == "completed"
    assert done["output"] == {"ok": True}
    # completed jobs are not claimable again
    assert q.claim(store, "worker-2") == []


def test_idempotent_enqueue(store):
    a = q.enqueue(store, "webhook-execution", {"n": 1}, job_id="sim-exec-42")
    b = q.enqueue(store, "webhook-execution", {"n": 2}, job_id="sim-exec-42")
    assert a["id"] == b["id"] == "sim-exec-42"
    assert b["payload"] == {"n": 1}  # original wins; the duplicate is a no-op
    assert len(store.queue_list()) == 1


def test_delayed_job_not_due(store):
    q.enqueue(store, "schedule-execution", {}, delay_seconds=3600)
    assert q.claim(store, "w1") == []


def test_type_filter(store):
    q.enqueue(store, "cleanup-logs", {})
    assert q.claim(store, "w1", types=["workflow-execution"]) == []
    assert len(q.claim(store, "w1", types=["cleanup-logs"])) == 1


def test_priority_order(store):
    low = q.enqueue(store, "t", {"which": "low"}, priority=0)
    high = q.enqueue(store, "t", {"which": "high"}, priority=10)
    claimed = q.claim(store, "w1", limit=2)
    assert [j["id"] for j in claimed] == [high["id"], low["id"]]


def test_batch_preserves_order(store):
    jobs = q.enqueue_batch(store, "t", [{"payload": {"i": i}} for i in range(5)])
    assert len(jobs) == 5
    claimed = q.claim(store, "w1", limit=5)
    assert [j["payload"]["i"] for j in claimed] == [0, 1, 2, 3, 4]


# ---- retries + dead letter --------------------------------------------------

def test_fail_retries_then_dead_letters(store):
    job = q.enqueue(store, "t", {}, max_attempts=2, retry_delay_seconds=0)

    first = q.claim(store, "w1")[0]
    assert first["attempts"] == 1
    failed = q.fail(store, job["id"], "w1", "boom 1")
    assert failed["status"] == "pending"  # retryable
    assert failed["lastError"] == "boom 1"

    second = q.claim(store, "w1")[0]
    assert second["attempts"] == 2
    dead = q.fail(store, job["id"], "w1", "boom 2")
    assert dead["status"] == "failed"
    assert dead["deadLetter"] is True
    assert dead["error"] == "boom 2"
    assert q.claim(store, "w1") == []


def test_retry_requeues_dead_letter(store):
    job = q.enqueue(store, "t", {}, max_attempts=1)
    q.claim(store, "w1")
    q.fail(store, job["id"], "w1", "boom")
    revived = q.retry(store, job["id"])
    assert revived["status"] == "pending"
    assert revived["attempts"] == 0
    assert revived["deadLetter"] is False
    assert len(q.claim(store, "w2")) == 1


# ---- leases ----------------------------------------------------------------

def test_expired_lease_is_reclaimed_by_another_worker(store):
    job = q.enqueue(store, "t", {}, max_attempts=2)
    q.claim(store, "w1", lease_seconds=0)  # lease already expired

    reclaimed = q.claim(store, "w2")
    assert len(reclaimed) == 1
    assert reclaimed[0]["workerId"] == "w2"
    assert reclaimed[0]["attempts"] == 2

    # the zombie original worker can no longer report on it
    with pytest.raises(RyaError) as e:
        q.complete(store, job["id"], "w1")
    assert e.value.code == "E_QUEUE_CONFLICT"


def test_expired_lease_dead_letters_when_exhausted(store):
    job = q.enqueue(store, "t", {}, max_attempts=1)
    q.claim(store, "w1", lease_seconds=0)
    assert q.claim(store, "w2") == []  # reap dead-letters it instead of re-issuing
    assert store.queue_get(job["id"])["status"] == "failed"
    assert store.queue_get(job["id"])["deadLetter"] is True


def test_heartbeat_extends_and_requires_holder(store):
    job = q.enqueue(store, "t", {})
    q.claim(store, "w1", lease_seconds=60)
    hb = q.heartbeat(store, job["id"], "w1", extend_seconds=120)
    assert hb["ok"] is True and hb["cancelRequested"] is False
    with pytest.raises(RyaError) as e:
        q.heartbeat(store, job["id"], "w2")
    assert e.value.code == "E_QUEUE_CONFLICT"


# ---- cancellation ------------------------------------------------------------

def test_cancel_pending(store):
    job = q.enqueue(store, "t", {})
    res = q.cancel(store, job["id"])
    assert res["status"] == "cancelled"
    assert q.claim(store, "w1") == []


def test_cancel_running_graceful(store):
    job = q.enqueue(store, "t", {})
    q.claim(store, "w1")
    res = q.cancel(store, job["id"])
    assert res["status"] == "running" and res["cancelRequested"] is True
    # worker learns via heartbeat, stops, and its final report lands as cancelled
    assert q.heartbeat(store, job["id"], "w1")["cancelRequested"] is True
    final = q.complete(store, job["id"], "w1")
    assert final["status"] == "cancelled"


def test_cancel_running_force(store):
    job = q.enqueue(store, "t", {})
    q.claim(store, "w1")
    res = q.cancel(store, job["id"], force=True)
    assert res["status"] == "cancelled"


def test_cancel_unknown_id_resolves_quietly(store):
    assert q.cancel(store, "qj_nope")["found"] is False


# ---- concurrency caps ---------------------------------------------------------

def test_concurrency_key_caps_parallelism(store):
    a = q.enqueue(store, "t", {"i": 0}, concurrency_key="user-7", concurrency_limit=1)
    q.enqueue(store, "t", {"i": 1}, concurrency_key="user-7", concurrency_limit=1)

    claimed = q.claim(store, "w1", limit=5)
    assert len(claimed) == 1  # cap holds even within one claim call

    q.complete(store, a["id"], "w1")
    assert len(q.claim(store, "w1", limit=5)) == 1  # slot freed


def test_concurrency_keys_are_independent(store):
    q.enqueue(store, "t", {}, concurrency_key="k1", concurrency_limit=1)
    q.enqueue(store, "t", {}, concurrency_key="k2", concurrency_limit=1)
    assert len(q.claim(store, "w1", limit=5)) == 2


# ---- stats ---------------------------------------------------------------------

def test_stats_counts(store):
    q.enqueue(store, "t", {})
    job = q.enqueue(store, "t", {})
    q.claim(store, "w1", limit=1)
    counts = q.stats(store)["counts"]
    assert counts.get("running") == 1
    assert counts.get("pending") == 1
    assert job  # silence lint


# ---- HTTP API round-trip (the exact surface a Sim adapter would use) ----------

def test_queue_http_roundtrip(project):
    client = TestClient(build_app(project))

    r = client.post("/queue/jobs", json={
        "type": "workflow-execution",
        "payload": {"workflowId": "wf-9"},
        "jobId": "sim-job-1", "maxAttempts": 2, "tags": ["workflow:wf-9"],
    })
    assert r.status_code == 200
    assert r.json()["job"]["id"] == "sim-job-1"

    # idempotent re-enqueue over HTTP
    r2 = client.post("/queue/jobs", json={"type": "workflow-execution",
                                          "payload": {"other": True}, "jobId": "sim-job-1"})
    assert r2.json()["job"]["payload"] == {"workflowId": "wf-9"}

    r = client.post("/queue/claim", json={"workerId": "sim-worker-1",
                                          "types": ["workflow-execution"], "limit": 5})
    jobs = r.json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["status"] == "running"

    r = client.post("/queue/jobs/sim-job-1/heartbeat",
                    json={"workerId": "sim-worker-1", "extendSeconds": 90})
    assert r.json()["cancelRequested"] is False

    r = client.post("/queue/jobs/sim-job-1/complete",
                    json={"workerId": "sim-worker-1", "output": {"executionId": "exec-1"}})
    assert r.json()["job"]["status"] == "completed"

    r = client.get("/queue/jobs/sim-job-1")
    assert r.json()["job"]["output"] == {"executionId": "exec-1"}

    assert client.get("/queue/stats").json()["counts"]["completed"] == 1

    # conflict + not-found map to HTTP codes an adapter can branch on
    assert client.post("/queue/jobs/sim-job-1/heartbeat",
                       json={"workerId": "sim-worker-1"}).status_code == 409
    assert client.get("/queue/jobs/qj_missing").status_code == 404


def test_queue_http_batch_and_cancel(project):
    client = TestClient(build_app(project))
    r = client.post("/queue/jobs/batch", json={
        "type": "cleanup-logs",
        "items": [{"payload": {"i": i}} for i in range(3)],
    })
    ids = r.json()["ids"]
    assert len(ids) == 3

    assert client.post(f"/queue/jobs/{ids[0]}/cancel", json={}).json()["status"] == "cancelled"
    r = client.post("/queue/claim", json={"workerId": "w", "limit": 10})
    assert [j["payload"]["i"] for j in r.json()["jobs"]] == [1, 2]
