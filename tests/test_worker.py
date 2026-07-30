"""PLATFORM_DESIGN §11 items 8-10: the worker, its lifecycle, and the pipeline
that feeds it.

The shape being tested is the one §2 draws: `api` enqueues, a worker per
(workspace, agent, version) claims, and the bundle it runs is an immutable
content-hashed artifact rather than whatever happens to be on disk.
"""

import pytest
import yaml

from rya import bundles, deployments
from rya.cli import scaffold
from rya.errors import RyaError
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store
from rya.worker import (
    COLD_START_TARGET_MS,
    Worker,
    WorkerKey,
    check_handler_set,
    resolve_bundle_root,
    start_worker,
)

ENTRYPOINT = '''
from rya import define_agent
agent = define_agent()

@agent.tool("echo")
async def echo(input):
    return {"echoed": input.get("v")}

@agent.on_event
async def main(ctx, event):
    return {"seen": event.payload.get("v")}

@agent.job("later")
async def later(ctx, job):
    return {"ran": job.payload.get("v")}
'''


def _project(tmp_path, name="wrk", tools=None, entrypoint=ENTRYPOINT):
    root = tmp_path / name
    scaffold.write_project(root, name, template="demo")
    (root / "src" / "agent.py").write_text(entrypoint)
    p = root / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = tools if tools is not None else [{"id": "echo", "permission": "allowed"}]
    p.write_text(yaml.safe_dump(doc))
    return root


def _store(tmp_path):
    s = Store(tmp_path / "state")
    s.ensure()
    return s


def _engine(root, store):
    manifest = load_manifest(root / "rya.agent.yaml")
    return Engine(manifest, load_agent(manifest, root), store, root)


# ---- 8. bundle loading ------------------------------------------------------

def test_worker_loads_a_pinned_bundle_and_reports_its_hash(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b, environment="prod")

    w = start_worker(project_root=root, store=store, version_id=v["id"], agent_name="wrk")
    assert w.key.bundle_hash == b.hash
    assert w.key.version_id == v["id"]
    # ...and it is running the UNPACKED archive, not the working tree.
    assert w.engine.project_root != root
    assert w.engine.manifest.name == "wrk"


def test_worker_resolves_the_environments_current_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b, environment="staging")

    w = start_worker(project_root=root, store=store, environment="staging", agent_name="wrk")
    assert w.key.version_id == v["id"]


def test_worker_refuses_a_version_that_does_not_exist(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, version_id="ver_nope", agent_name="wrk")
    assert e.value.code == "E_VERSION_NOT_FOUND"


def test_worker_refuses_a_retired_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b)
    deployments.retire(store, v["id"])
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, version_id=v["id"], agent_name="wrk")
    assert e.value.code == "E_VERSION_RETIRED"


def test_a_tampered_bundle_fails_before_any_code_is_imported(tmp_path):
    """D12: replay is only sound against the code that wrote the journal, so a
    worker that cannot prove it has that code must not start."""
    store = _store(tmp_path)
    root = _project(tmp_path)
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b)

    key = WorkerKey(workspace="default", agent="wrk", version_id=v["id"])
    unpacked, _ = resolve_bundle_root(store, key, project_root=root)
    (unpacked / "src" / "agent.py").write_text("raise SystemExit('pwned')\n")

    with pytest.raises(RyaError) as e:
        resolve_bundle_root(store, key, project_root=root)
    assert e.value.code == "E_BUNDLE_MISMATCH"


# ---- 8. handler-set advertisement -------------------------------------------

def test_worker_advertises_its_handler_set(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    w = Worker(_engine(root, store), WorkerKey("default", "wrk"))
    handlers = w.preflight()
    assert handlers["event"] is True
    assert handlers["jobs"] == ["later"]
    assert "echo" in handlers["tools"]


def test_worker_refuses_to_start_with_a_hole_in_its_handler_set(tmp_path):
    """§5.2: "the image is missing a handler" must surface at startup, not
    mid-run on whichever unlucky request reached it first."""
    store = _store(tmp_path)
    root = _project(tmp_path, tools=[{"id": "echo", "permission": "allowed"},
                                     {"id": "not.implemented", "permission": "allowed"}])
    w = Worker(_engine(root, store), WorkerKey("default", "wrk"))
    with pytest.raises(RyaError) as e:
        w.preflight()
    assert e.value.code == "E_HANDLER_SET_INCOMPLETE"
    assert "not.implemented" in e.value.message


def test_a_url_tool_needs_no_local_handler(tmp_path):
    """The platform performs a declared `url:` call itself (§7.1 layer 1), so the
    bundle owes no implementation for it."""
    store = _store(tmp_path)
    root = _project(tmp_path, tools=[{"id": "echo", "permission": "allowed"},
                                     {"id": "remote", "permission": "allowed",
                                      "url": "https://api.example.com/x"}])
    assert check_handler_set(load_manifest(root / "rya.agent.yaml"),
                             load_agent(load_manifest(root / "rya.agent.yaml"), root)) == []


def test_worker_refuses_a_version_for_a_different_agent(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    w = Worker(engine, WorkerKey("default", "wrk", version_id="ver_x"),
               version={"agent": "someone-else", "id": "ver_x"})
    with pytest.raises(RyaError) as e:
        w.preflight()
    assert e.value.code == "E_BUNDLE_MISMATCH"


# ---- 9. registration + lifecycle --------------------------------------------

def test_worker_registers_what_it_actually_is(tmp_path):
    """`queue.claim` took a bare worker_id string that was never registered or
    validated; an operator could not see which version was live for which key."""
    store = _store(tmp_path)
    root = _project(tmp_path)
    w = Worker(_engine(root, store), WorkerKey("default", "wrk", version_id="ver_1",
                                               bundle_hash="deadbeef"))
    w.preflight()
    rec = w.register()

    live = store.worker_list(agent="wrk")
    assert [r["id"] for r in live] == [rec["id"]]
    assert live[0]["bundleHash"] == "deadbeef"
    assert live[0]["handlers"]["jobs"] == ["later"]
    assert live[0]["concurrencyKey"] == "default:wrk:ver_1"

    w.deregister("test")
    assert store.worker_list(agent="wrk", status="alive") == []


def test_worker_drains_jobs_and_turns(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    engine.store.create_job(None, "later", {"v": 7}, "1970-01-01T00:00:00Z")

    from rya import turns
    turns.create_turn(engine, "message.received", {"v": 1})

    w = Worker(engine, WorkerKey("default", "wrk"))
    result = w.run(max_iterations=1)
    assert result["stats"]["claimed"] >= 2
    assert result["reason"] == "max-iterations"


def test_worker_scales_to_zero_when_idle(tmp_path):
    """§6: a process with no claimed work and an empty queue for its key exits
    after an idle window."""
    store = _store(tmp_path)
    root = _project(tmp_path)
    w = Worker(_engine(root, store), WorkerKey("default", "wrk"),
               idle_exit_seconds=0.01, poll_seconds=0.01)
    result = w.run(max_iterations=50)
    assert result["reason"] == "idle"
    assert result["iterations"] < 50


def test_a_foreign_queue_job_does_not_keep_a_worker_alive(tmp_path):
    """The queue table serves TWO product surfaces (D14): chat turns dispatched to
    `rya worker`, and SDK-free `/queue/*` jobs that foreign consumers claim for
    themselves. A worker only ever claims `chat-turn` plus the `jobs` primitive.

    Counting foreign jobs as "my depth" means one pending `/queue/*` job — work
    this process will never touch — reads as busy forever, the idle window never
    elapses, and scale-to-zero never fires. §6 opens by naming idle cost as the
    constraint for N workspaces x M agents x V versions, so a worker that cannot
    go idle is the expensive failure.
    """
    from rya import queue as q

    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    q.enqueue(store, "rwap.step", {"dag": "n1"})  # a foreign D14 job
    assert store.queue_counts().get("pending") == 1

    w = Worker(engine, WorkerKey("default", "wrk"),
               idle_exit_seconds=0.01, poll_seconds=0.01)
    assert w.queue_depth() == 0, "a foreign queue job is not this worker's work"

    result = w.run(max_iterations=50)
    assert result["reason"] == "idle"
    # And it left the foreign job alone for its real consumer.
    assert store.queue_counts().get("pending") == 1


def test_a_pending_chat_turn_does_keep_a_worker_alive(tmp_path):
    """The other half of the rule: a turn IS this worker's work, so depth counts
    it and the worker does not idle out from under it."""
    from rya import turns

    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    turns.create_turn(engine, "message.received", {"v": 1})

    w = Worker(engine, WorkerKey("default", "wrk"))
    assert w.queue_depth() == 1


def test_queue_depth_ignores_turns_pinned_to_another_version(tmp_path):
    """D12: a turn pinned to a different version is not claimable here, so it must
    not read as depth either — otherwise every worker stays awake for every other
    version's backlog."""
    from rya import queue as q

    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    q.enqueue(store, "chat-turn", {"type": "message.received", "payload": {}},
              metadata={"versionId": "ver_other"})

    mine = Worker(engine, WorkerKey("default", "wrk", version_id="ver_mine"))
    assert mine.queue_depth() == 0
    unpinned = Worker(engine, WorkerKey("default", "wrk"))
    assert unpinned.queue_depth() == 1


def test_worker_stays_up_while_work_keeps_arriving(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    engine = _engine(root, store)
    w = Worker(engine, WorkerKey("default", "wrk"), idle_exit_seconds=0.01, poll_seconds=0.001)

    def keep_feeding(_tick):
        if w.stats.claimed < 3:
            engine.store.create_job(None, "later", {"v": w.stats.claimed}, "1970-01-01T00:00:00Z")

    result = w.run(max_iterations=20, on_tick=keep_feeding)
    assert result["stats"]["claimed"] >= 3


def test_cold_start_is_measured(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    w = start_worker(project_root=root, store=store, agent_name="wrk")
    assert w.stats.coldStartMs >= 0
    assert isinstance(COLD_START_TARGET_MS, int)


def test_stop_is_cooperative(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path)
    w = Worker(_engine(root, store), WorkerKey("default", "wrk"), poll_seconds=0.001)
    w.run(max_iterations=1, on_tick=lambda _t: w.stop())
    assert w._stop is True


# ---- 10. version pinning end to end -----------------------------------------

def test_runs_are_pinned_to_the_version_that_executed_them(tmp_path):
    """D12. `agentVersion` was only ever the author-typed manifest string."""
    store = _store(tmp_path)
    root = _project(tmp_path)
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b, environment="prod")

    w = start_worker(project_root=root, store=store, version_id=v["id"],
                     environment="prod", agent_name="wrk")
    run = w.engine.run_event("message.received", {"v": 3})
    assert run["versionId"] == v["id"]
    assert run["bundleHash"] == b.hash
    assert run["environment"] == "prod"
    assert deployments.pinned_runs(store, v["id"]) == [] or run["status"] == "completed"


def test_a_pinned_worker_will_not_claim_another_versions_work(tmp_path):
    """A worker serving version A must not execute a run pinned to version B —
    replay is only sound against the code that wrote the journal."""
    from rya import queue as q
    store = _store(tmp_path)
    root = _project(tmp_path)

    mine = q.enqueue(store, "chat-turn", {"type": "m", "payload": {}},
                     metadata={"versionId": "ver_A"})
    theirs = q.enqueue(store, "chat-turn", {"type": "m", "payload": {}},
                       metadata={"versionId": "ver_B"})
    loose = q.enqueue(store, "chat-turn", {"type": "m", "payload": {}})

    claimed = q.claim(store, "worker-A", types=["chat-turn"], limit=10, version_id="ver_A")
    ids = {j["id"] for j in claimed}
    assert mine["id"] in ids
    assert loose["id"] in ids          # unpinned work is anyone's
    assert theirs["id"] not in ids

    # ...and refusing it neither consumed its retry budget nor left it running.
    back = store.queue_get(theirs["id"])
    assert back["status"] == "pending" and back["attempts"] == 0
    assert q.claim(store, "worker-B", types=["chat-turn"], limit=10,
                   version_id="ver_B")[0]["id"] == theirs["id"]


def test_an_unpinned_worker_claims_anything(tmp_path):
    """`rya dev`, single-tenant serve, and any foreign /queue/* consumer (D14)
    are unaffected by version pinning."""
    from rya import queue as q
    store = _store(tmp_path)
    q.enqueue(store, "chat-turn", {"type": "m"}, metadata={"versionId": "ver_A"})
    q.enqueue(store, "chat-turn", {"type": "m"})
    assert len(q.claim(store, "any", types=["chat-turn"], limit=10)) == 2


def test_retiring_a_version_with_a_live_run_fails_closed(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, entrypoint=ENTRYPOINT.replace(
        'return {"seen": event.payload.get("v")}',
        'await ctx.approvals.request(title="gate", body="b", '
        'action={"tool": "echo", "input": {"v": 1}})'))
    b = bundles.build_bundle(root)
    bundles.store_bundle(b, bundles.default_archive_root(root))
    v = deployments.create_version(store, agent="wrk", bundle=b)

    w = start_worker(project_root=root, store=store, version_id=v["id"], agent_name="wrk")
    run = w.engine.run_event("message.received", {"v": 1})
    assert run["status"] == "waiting_approval"

    with pytest.raises(RyaError) as e:
        deployments.retire(store, v["id"])
    assert e.value.code == "E_VERSION_IN_USE"
    assert run["id"] in [r["id"] for r in deployments.pinned_runs(store, v["id"])]
