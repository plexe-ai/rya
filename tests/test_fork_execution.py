"""Fork per run, from a hash-keyed warm interpreter (D27, issue #19).

These spawn real processes and fork real children, because the property being
tested is exactly that: the claimer does not hold the tenant's import, and the run
happens somewhere else. A mocked pool would assert the shape of the code rather
than the thing the shape exists to achieve.

The load-bearing assertion is :func:`test_the_claimer_never_imports_the_bundle`.
Everything else in this file could pass with the import still in the claimer.
"""

import os
import sys
from types import SimpleNamespace

import pytest
import yaml

from rya import bundles, deployments
from rya.cli import scaffold
from rya.errors import RyaError
from rya.execution.pool import FORK_AVAILABLE, WarmPool, WarmTemplate
from rya.store import Store
from rya.worker import ForkExecutor, start_worker

pytestmark = pytest.mark.skipif(not FORK_AVAILABLE,
                                reason="fork-per-run needs os.fork (not on Windows)")

ENTRYPOINT = '''
import os
from rya import define_agent

agent = define_agent()

# Module scope, so it runs in the TEMPLATE (once) rather than per fork. Phase 0
# measured tenant import time as the dominant cold-start term; this is the line
# whose cost the warm pool exists to pay only once.
IMPORTED_IN_PID = os.getpid()

@agent.tool("echo")
async def echo(input):
    return {"echoed": input.get("v")}

@agent.on_event
async def main(ctx, event):
    return {"seen": event.payload.get("v"), "pid": os.getpid(),
            "importedIn": IMPORTED_IN_PID}
'''


def _project(tmp_path, name="forked", entrypoint=ENTRYPOINT):
    root = tmp_path / name
    scaffold.write_project(root, name, template="demo")
    (root / "src" / "agent.py").write_text(entrypoint)
    p = root / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = [{"id": "echo", "permission": "allowed"}]
    p.write_text(yaml.safe_dump(doc))
    return root


def _source(store, version, agent="forked"):
    """What the control plane hands `turns.enqueue_event` under D21 — a store, a
    name and a pin, and deliberately no engine (`turns.TurnSource`)."""
    from rya import turns

    return turns.TurnSource(store=store, manifest=SimpleNamespace(name=agent, version="0.1.0"),
                            version=version)


def _published(tmp_path, **kw):
    """A project, a state store, and one published version of it."""
    root = _project(tmp_path, **kw)
    store = Store(root / ".rya")
    store.ensure()
    bundle = bundles.build_bundle(root)
    bundles.store_bundle(bundle, bundles.default_archive_root(root))
    version = deployments.create_version(store, agent=bundle.agent, bundle=bundle)
    return root, store, version


# ---- the property ----------------------------------------------------------

def test_the_claimer_never_imports_the_bundle(tmp_path):
    """D27's whole point, and the one thing a mock could not check.

    ``load_agent`` gives every imported entrypoint a ``rya_user_agent_*`` module
    name, so the absence of one in ``sys.modules`` is direct evidence that this
    process did not import tenant code — while the worker is nonetheless fully
    preflighted and advertising the handler set the bundle really has.
    """
    root, store, version = _published(tmp_path)
    before = {m for m in sys.modules if m.startswith("rya_user_agent_")}

    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        handlers = w.preflight()
        assert handlers["tools"] == ["echo"] and handlers["event"] is True
        assert w.executor.mode == "fork"
        assert w.engine is None
        after = {m for m in sys.modules if m.startswith("rya_user_agent_")}
        assert after == before
    finally:
        w.executor.close()


def test_a_turn_runs_in_a_forked_child_not_in_the_claimer(tmp_path):
    """The run's own pid proves where it happened. It is neither the claimer (this
    process) nor the template — a fork per run, from a warm parent."""
    from rya import turns

    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        template_pid = w.executor.template()._proc.pid
        turns.enqueue_event(_source(store, version), "message.received", {"v": 3})
        tick = w.drain_once()
        assert tick["count"] == 1

        run = next(r for r in store.list_runs() if r.get("status") == "completed")
        assert run["output"]["pid"] not in (os.getpid(), template_pid)
        # The import happened in the template and was inherited by the fork, which
        # is what keeps it off the per-run critical path.
        assert run["output"]["importedIn"] == template_pid
    finally:
        w.executor.close()


def test_every_run_gets_a_fresh_process(tmp_path):
    """Fork per RUN, not per worker. Two items, two pids — which is what bounds the
    blast radius of a handler that corrupts its own interpreter."""
    from rya import turns

    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        src = _source(store, version)
        pids = []
        for i in range(2):
            turns.enqueue_event(src, "message.received", {"v": i})
            assert w.drain_once()["count"] == 1
        for run in store.list_runs():
            if run.get("status") == "completed":
                pids.append(run["output"]["pid"])
        assert len(pids) == 2 and len(set(pids)) == 2
    finally:
        w.executor.close()


def test_several_queued_items_in_one_tick_still_get_one_fork_each(tmp_path):
    """Fork per RUN, not per TICK — which the first implementation got wrong.

    Handing a single child ``limit=N`` drains N items in one address space, so ten
    queued turns would share a process and a handler that corrupted its interpreter
    would take the other nine with it. The claimer loops instead, forking per item.
    """
    from rya import turns

    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        src = _source(store, version)
        for i in range(3):
            turns.enqueue_event(src, "message.received", {"v": i})
        tick = w.drain_once()               # ONE tick, three queued items
        assert tick["count"] == 3

        pids = {r["output"]["pid"] for r in store.list_runs()
                if r.get("status") == "completed"}
        assert len(pids) == 3
    finally:
        w.executor.close()


def test_the_import_is_paid_once_across_many_runs(tmp_path):
    """The warm pool's reason for existing: N runs, one import."""
    from rya import turns

    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        src = _source(store, version)
        for i in range(3):
            turns.enqueue_event(src, "message.received", {"v": i})
            w.drain_once()
        imported_in = {r["output"]["importedIn"]
                       for r in store.list_runs() if r.get("status") == "completed"}
        assert len(imported_in) == 1
    finally:
        w.executor.close()


# ---- the pool is keyed by content ------------------------------------------

def test_the_pool_is_keyed_by_bundle_hash(tmp_path):
    """§9 risk 9: a pool is a cache, and handing a run an interpreter that loaded
    different code is the hazard D9's content-keyed journal exists to catch, one
    layer down. So the key is the content, and two requests for the same content
    share one warm interpreter."""
    root, store, version = _published(tmp_path)
    pool = WarmPool(max_entries=4)
    try:
        a = pool.acquire(bundle_hash=version["bundleHash"], root=root, version=version)
        b = pool.acquire(bundle_hash=version["bundleHash"], root=root, version=version)
        assert a is b
        assert pool.hashes == [version["bundleHash"]]
    finally:
        pool.close()


def test_a_template_that_loaded_different_content_is_refused(tmp_path):
    """The check has to be on what was actually loaded, not on what was requested —
    echoing the request back would make it confirm only that the caller remembered
    its own question."""
    root, store, version = _published(tmp_path)
    tmpl = WarmTemplate(bundle_hash="0" * 64, root=root)
    with pytest.raises(RyaError) as e:
        tmpl.start()
    assert e.value.code == "E_POOL_HASH_MISMATCH"
    assert version["bundleHash"] in e.value.message
    assert not tmpl.alive


def test_two_versions_of_one_agent_get_two_warm_interpreters(tmp_path):
    """#19's rollout case: `support` needs v7 for the runs pinned to it and v8 for
    the new ones. Keyed by hash, those are two entries in one pool rather than a
    collision on one — which is what makes the D27 endgame (both in ONE sandbox)
    possible at all."""
    root, store, v7 = _published(tmp_path)
    (root / "src" / "agent.py").write_text(ENTRYPOINT + "\n# v8\n")
    bundle8 = bundles.build_bundle(root)
    bundles.store_bundle(bundle8, bundles.default_archive_root(root))
    v8 = deployments.create_version(store, agent="forked", bundle=bundle8)
    assert v7["bundleHash"] != v8["bundleHash"]

    pool = WarmPool(max_entries=4)
    try:
        # v7's tree has to be materialised from its archive: the working tree is v8
        # now, which is exactly the situation a hash-keyed pool must survive.
        from rya.worker import WorkerKey, resolve_bundle_root

        root7, _ = resolve_bundle_root(store, WorkerKey("default", "forked", v7["id"]),
                                       project_root=root)
        pool.acquire(bundle_hash=v7["bundleHash"], root=root7, version=v7)
        pool.acquire(bundle_hash=v8["bundleHash"], root=root, version=v8)
        assert sorted(pool.hashes) == sorted([v7["bundleHash"], v8["bundleHash"]])
    finally:
        pool.close()


def test_the_pool_evicts_the_least_recently_used(tmp_path):
    """Unbounded, a tenant that promotes repeatedly accumulates one warm interpreter
    per historical hash and the memory is never reclaimed."""
    root, store, version = _published(tmp_path)
    pool = WarmPool(max_entries=1)
    try:
        first = pool.acquire(bundle_hash=version["bundleHash"], root=root, version=version)
        # A second entry under a different key: the working-tree entry, which is the
        # one case the pool cannot content-check (that mode's whole point is that the
        # tree changes).
        pool.acquire(bundle_hash=None, root=root)
        assert pool.hashes == ["local"]
        assert not first.alive
    finally:
        pool.close()


# ---- preflight still fails closed ------------------------------------------

def test_a_handler_set_hole_still_fails_before_anything_is_claimed(tmp_path):
    """The guarantee `worker.preflight` exists for, relocated rather than lost. The
    claimer cannot see a handler set — but the template can, and starting the
    template IS the preflight, so the failure is still at startup and not on
    whichever request first reached the missing tool."""
    root = _project(tmp_path)
    p = root / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = [{"id": "echo", "permission": "allowed"},
                    {"id": "nowhere.tool", "permission": "allowed"}]
    p.write_text(yaml.safe_dump(doc))

    store = Store(root / ".rya")
    store.ensure()
    bundle = bundles.build_bundle(root)
    bundles.store_bundle(bundle, bundles.default_archive_root(root))
    version = deployments.create_version(store, agent="forked", bundle=bundle)

    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        with pytest.raises(RyaError) as e:
            w.preflight()
        assert e.value.code == "E_HANDLER_SET_INCOMPLETE"
        assert "nowhere.tool" in e.value.message
    finally:
        w.executor.close()


def test_a_bundle_that_will_not_import_fails_at_worker_startup(tmp_path):
    """Symmetry with the in-process mode, where `load_agent` raises in
    `start_worker`. A tenant entrypoint that throws must not become a mid-run
    surprise just because the import moved to another process."""
    root, store, version = _published(
        tmp_path, entrypoint=ENTRYPOINT + "\nraise RuntimeError('boom at import')\n")
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    assert e.value.code == "E_BUNDLE_IMPORT_FAILED"
    assert "boom at import" in e.value.message


def test_the_claimer_still_reports_claimable_depth_without_an_engine(tmp_path):
    """Scale-to-zero has to keep working in fork mode, so the claimer needs depth
    without an `Engine` — an `Engine` holds an agent, which is the thing this
    process must not have."""
    from rya import turns

    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        assert w.queue_depth() == 0
        turns.enqueue_event(_source(store, version), "message.received", {"v": 1})
        assert w.queue_depth() == 1
        store.create_job(None, "later", {}, "1970-01-01T00:00:00Z", agent="forked")
        assert w.queue_depth() == 2
    finally:
        w.executor.close()


def test_a_dead_template_is_reported_rather_than_silently_retried(tmp_path):
    """"The interpreter holding this tenant's code keeps dying" is a fact a
    supervisor should see, not something a retry loop absorbs."""
    root, store, version = _published(tmp_path)
    executor = ForkExecutor(store=store, root=root, version=version,
                            workspace="default", agent_name="forked")
    executor.template()
    executor._template.stop()
    with pytest.raises(RyaError) as e:
        executor._template.drain(limit=1)
    assert e.value.code == "E_TEMPLATE_NOT_RUNNING"
    executor.close()


def test_a_failed_drain_does_not_raise_out_of_the_worker_loop(tmp_path):
    """A broken executor must produce a tick that says so, not an exception that
    ends the claim loop — the item's lease is what recovers the work."""
    root, store, version = _published(tmp_path)
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="forked", fork=True)
    try:
        w.executor.template().stop()
        w.executor.pool.close()          # so `template()` cannot transparently restart

        def dead(**kw):
            raise RyaError("E_TEMPLATE_LOST", "the interpreter died")

        w.executor.drain = dead
        tick = w.drain_once()
        assert tick["count"] == 0 and "died" in tick["error"]
    finally:
        w.executor.close()
