"""The api stops executing tenant code — Phase 2's three `GAP` assertions.

`scripts/e2e_platform.py` has carried these as known gaps since the platform
split was written:

    POST /events does not execute in the api process
    POST /events pins the run to the promoted version
    approval resume is claimed by a worker

They are asserted here too, against `RYA_API_INLINE_WORKER=0` — the switch that
already meant "this process runs no handlers" for the background loops but not
for the request path, which is precisely the hole. The e2e proves it end to end
against a real two-process deployment; these prove the mechanism, fast, and say
what each one is actually about.

The fourth thing this file pins is the shape of what replaces execution: a run
row created by the CONTROL plane, so a caller gets an id synchronously, the pin
is decided by whoever can read the environment pointer, and the quota refusal
still reaches the caller as a 429 rather than becoming a silently failed run.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rya import queue as q
from rya import turns
from rya.api.app import build_app
from rya.bundles import build_bundle, pack
from rya.cli import scaffold
from rya.runtime import Engine, load_agent
from rya.manifest import load_manifest
from rya.store import open_store


@pytest.fixture
def offline_api(tmp_path, monkeypatch):
    """A single-tenant api with the inline worker OFF — the production shape.

    `rya dev` sets exactly this so the local shape matches production (§10), and
    it is what the e2e runs under.
    """
    monkeypatch.setenv("RYA_API_INLINE_WORKER", "0")
    monkeypatch.setenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", "1")
    # The api reads the environment pointer to decide what a queued run pins to,
    # so it has to be on the SAME environment as the worker — `prod` here, which
    # is what `_publish` promotes to. An api on `dev` and a worker on `prod`
    # would pin to nothing and every turn would sit unclaimed.
    monkeypatch.setenv("RYA_ENVIRONMENT", "prod")
    for k in ("RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "boundary-agent", template="demo")
    return TestClient(build_app(tmp_path)), tmp_path


def _worker_engine(root: Path, version: dict | None = None) -> Engine:
    """A stand-in for `rya worker`: the process that DOES import the bundle."""
    manifest = load_manifest(root / "rya.agent.yaml")
    return Engine(manifest, load_agent(manifest, root), open_store(root), root,
                  version=version)


def _publish(client: TestClient, root: Path, tmp_path: Path) -> dict:
    bundle = build_bundle(root)
    archive = pack(bundle, tmp_path / "b.tar.gz")
    r = client.post(f"/agents/boundary-agent/versions?hash={bundle.hash}&env=prod",
                    content=archive.read_bytes(),
                    headers={"content-type": "application/gzip"})
    assert r.status_code == 200, r.text
    return r.json()


def test_post_events_does_not_execute_in_the_api_process(offline_api):
    client, root = offline_api
    r = client.post("/agents/boundary-agent/events",
                    json={"type": "message.received", "payload": {"email": "a@x.com"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["runId"], "the caller still gets a run id synchronously"

    # Nothing ran: no trace, no journal, and the run is not 'running'.
    run = client.get(f"/runs/{body['runId']}").json()
    assert run["status"] == "queued"
    assert run["trace"] == [] and run["journal"] == {}


def test_post_events_pins_the_run_to_the_promoted_version(offline_api, tmp_path):
    """The pin is decided by the CONTROL plane, which is the only party that can
    read the environment pointer authoritatively. Before this the api enqueued
    unpinned and whichever worker claimed the turn stamped its own version — so
    'which code ran this' was decided by scheduling."""
    client, root = offline_api
    published = _publish(client, root, tmp_path)

    body = client.post("/agents/boundary-agent/events",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}).json()
    run = client.get(f"/runs/{body['runId']}").json()
    assert run["versionId"] == published["versionId"]
    assert run["bundleHash"] == published["bundleHash"]


def test_the_queued_run_is_the_one_the_worker_finishes(offline_api):
    """The run id handed to the caller must be the id the run ends up with —
    otherwise the synchronous id is a decoy and clients have to correlate a turn
    id back to a run afterwards."""
    client, root = offline_api
    body = client.post("/agents/boundary-agent/events",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}).json()

    ran = turns.execute_pending(_worker_engine(root), worker_id="w1")
    assert ran == [body["turnId"]]

    run = client.get(f"/runs/{body['runId']}").json()
    assert run["status"] == "waiting_approval"  # the demo template's approval gate
    assert run["trace"], "the worker executed against the control plane's run row"


def test_an_over_quota_event_is_refused_by_the_api_not_by_a_worker(offline_api):
    """Admission is a 429 to the CALLER. Deferring it to the worker would turn an
    over-quota call into a 200 followed by a run that fails where nobody looks."""
    client, root = offline_api
    assert client.put("/quotas", json={"maxRunsPerDay": 1}).status_code == 200

    first = client.post("/agents/boundary-agent/events",
                        json={"type": "message.received", "payload": {"email": "a@x.com"}})
    assert first.status_code == 200

    second = client.post("/agents/boundary-agent/events",
                         json={"type": "message.received", "payload": {"email": "b@x.com"}})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "E_QUOTA_EXCEEDED"


# ---- approval resume ------------------------------------------------------
# The scaffolded demo already pauses on a human approval gate, so nothing here
# needs a bespoke agent — the fixture exercises the shipped template's shape.
@pytest.fixture
def paused(offline_api):
    """A run parked in `waiting_approval`, executed by a worker (never the api)."""
    client, root = offline_api
    body = client.post("/agents/boundary-agent/events",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}).json()
    turns.execute_pending(_worker_engine(root), worker_id="w1")
    run = client.get(f"/runs/{body['runId']}").json()
    assert run["status"] == "waiting_approval", run
    approval = client.get("/approvals?status=pending").json()["approvals"][0]
    return client, root, run, approval


def test_approval_resume_is_handed_to_a_worker(paused):
    client, root, run, approval = paused
    r = client.post(f"/approvals/{approval['id']}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True and body["runStatus"] == "resuming"

    # The api recorded the decision and executed nothing.
    assert client.get(f"/runs/{run['id']}").json()["status"] == "waiting_approval"

    # A worker claims it and finishes the run.
    ran = turns.execute_resumes(_worker_engine(root), worker_id="w1")
    assert ran == [body["jobId"]]
    assert client.get(f"/runs/{run['id']}").json()["status"] == "completed"


def test_the_resume_job_is_pinned_to_the_version_that_paused_the_run(offline_api, tmp_path):
    """Why the split ends `E_JOURNAL_DRIFT`: the process that continues a run is
    on the same content hash as the one that paused it, by construction, rather
    than being whatever the api imported at boot."""
    client, root = offline_api
    published = _publish(client, root, tmp_path)

    body = client.post("/agents/boundary-agent/events",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}).json()
    version = {"id": published["versionId"], "bundleHash": published["bundleHash"]}
    turns.execute_pending(_worker_engine(root, version), worker_id="w1")
    approval = client.get("/approvals?status=pending").json()["approvals"][0]

    job_id = client.post(f"/approvals/{approval['id']}/approve").json()["jobId"]
    job = client.get(f"/queue/jobs/{job_id}").json()["job"]
    assert q.version_of(job) == published["versionId"]
    assert q.agent_of(job) == "boundary-agent"

    # A worker on a DIFFERENT version does not claim it (D12's filter).
    other = _worker_engine(root, {"id": "ver_other"})
    assert turns.execute_resumes(other, worker_id="w2") == []
    assert client.get(f"/runs/{body['runId']}").json()["status"] == "waiting_approval"

    # The worker on the right version does.
    assert turns.execute_resumes(_worker_engine(root, version), worker_id="w1") == [job_id]
    assert client.get(f"/runs/{body['runId']}").json()["status"] == "completed"


def test_a_second_approve_is_refused_rather_than_resuming_twice(paused):
    client, root, run, approval = paused
    assert client.post(f"/approvals/{approval['id']}/approve").status_code == 200
    second = client.post(f"/approvals/{approval['id']}/approve")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "E_APPROVAL_NOT_PENDING"


def test_reject_stays_synchronous_because_it_runs_no_tenant_code(paused):
    """The asymmetry is the point: rejecting marks two records and appends a
    trace step. Making it async for symmetry would add latency to the path an
    operator uses to STOP something."""
    client, root, run, approval = paused
    r = client.post(f"/approvals/{approval['id']}/reject")
    assert r.status_code == 200
    assert r.json()["queued"] is False and r.json()["runStatus"] == "rejected"
    assert client.get(f"/runs/{run['id']}").json()["status"] == "rejected"


def test_the_inline_worker_still_executes_where_it_is_allowed(tmp_path, monkeypatch):
    """A bare single-tenant `rya serve` IS the whole deployment, and silently
    ceasing to run anything would be a worse failure than the isolation gap it
    closes. That seam is unchanged — this test is what stops the boundary work
    from quietly turning `rya dev` into a no-op."""
    for k in ("RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_TOKEN", "RYA_API_INLINE_WORKER"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "inline-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    body = client.post("/agents/inline-agent/events",
                       json={"type": "message.received", "payload": {"email": "a@x.com"}}).json()
    # The demo template pauses on its approval gate — the point is that it RAN,
    # synchronously, in this process, rather than sitting queued.
    assert body["status"] == "waiting_approval"
    assert client.get(f"/runs/{body['runId']}").json()["trace"]
