"""The wide claimer scope (D27/#19-8b) — MULTITENANT_PLAN §7's five exit criteria.

Phase 3 built fork-per-run and shipped it scoped to one ``(workspace, agent,
version)``, on the strength of a claim: that widening to per-tenant would then be
*configuration* rather than a rewrite. This file is where that claim is either true
or not, so the tests are named after the criteria rather than after the functions.

They fork real processes and materialise real bundles, for the reason
``test_fork_execution.py`` gives: the properties under test are about *where* work
happens and *how many* interpreters exist, and a mocked pool would assert the shape
of the code instead of the thing the shape is for.

Two agents throughout, because every interesting property of this scope is a
statement about a sibling — one agent's rollout not costing another a sandbox, one
agent's backlog not starving another's single turn, one agent's broken bundle not
stopping another's work.
"""

import os
import sys
from types import SimpleNamespace

import pytest
import yaml

from rya import bundles, deployments, turns
from rya.cli import scaffold
from rya.errors import RyaError
from rya.execution import scope as S
from rya.execution.pool import FORK_AVAILABLE, default_pool_size
from rya.store import Store
from rya.worker import start_worker

pytestmark = pytest.mark.skipif(not FORK_AVAILABLE,
                                reason="the tenant scope requires os.fork")

ENTRYPOINT = '''
import os
from rya import define_agent

agent = define_agent()
IMPORTED_IN_PID = os.getpid()

@agent.on_event
async def main(ctx, event):
    return {"agent": %r, "seen": event.payload.get("v"), "pid": os.getpid(),
            "importedIn": IMPORTED_IN_PID}
'''

# A bundle that declares a tool it does not implement — the hole `preflight` exists
# for, which at this scope has to be caught per agent instead of per process.
HOLE = '''
from rya import define_agent

agent = define_agent()

@agent.on_event
async def main(ctx, event):
    return {"ok": True}
'''


def _project(tmp_path, name, entrypoint=None, tools=None):
    root = tmp_path / name
    scaffold.write_project(root, name, template="demo")
    (root / "src" / "agent.py").write_text(entrypoint or (ENTRYPOINT % name))
    p = root / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = tools if tools is not None else []
    p.write_text(yaml.safe_dump(doc))
    return root


def _tenant(tmp_path):
    """A deployment root with its own state store — the shape a claimer runs in.

    The state store lives beside the deployment and the bundles live in its archive
    root, which is exactly the split ``pool._open_store`` documents: a claimer that
    confused the two would build a private empty database out of a bundle directory.
    """
    root = tmp_path / "deployment"
    root.mkdir()
    store = Store(root)
    store.ensure()
    return root, store


def _publish(root, store, project, agent, *, promote=None):
    """Publish ``project``'s current tree as a version of ``agent`` under ``root``."""
    bundle = bundles.build_bundle(project)
    bundles.store_bundle(bundle, bundles.default_archive_root(root))
    version = deployments.create_version(store, agent=agent, bundle=bundle)
    if promote:
        deployments.promote(store, environment=promote, agent=agent,
                            version_id=version["id"], actor="test", gate=False)
    return version


def _run_of(store, turn_id):
    """The run behind a drained turn id.

    ``execute_pending`` returns *turn* ids (queue rows), not run ids — the api created
    the run row before enqueuing, so the queue job carries a ``payload.runId``. Worth a
    helper rather than an inline lookup because getting it wrong reads as "the run
    vanished" instead of "you looked up the wrong id".
    """
    job = store.queue_get(turn_id)
    return store.get_run((job.get("payload") or {})["runId"])


def _enqueue(store, version, agent, value):
    source = turns.TurnSource(store=store,
                              manifest=SimpleNamespace(name=agent, version="0.1.0"),
                              version=version)
    return turns.enqueue_event(source, "message.received", {"v": value})


def _two_agents(tmp_path):
    """`support` and `billing`, both published into one tenant's deployment."""
    root, store = _tenant(tmp_path)
    support = _publish(root, store, _project(tmp_path, "support"), "support")
    billing = _publish(root, store, _project(tmp_path, "billing"), "billing")
    return root, store, support, billing


def _claimer(root, store, **kw):
    return start_worker(project_root=root, store=store, fork=True,
                        scope=S.SCOPE_TENANT, **kw)


# ---- the vocabulary --------------------------------------------------------

def test_an_unknown_scope_is_refused_rather_than_defaulted():
    """A deployment that set `RYA_CLAIMER_SCOPE=per-tenant` and silently got the
    narrow scope would be paying the N x M x V sandbox bill while believing it had
    stopped. Same stance `quotas.py` takes for a mistyped limit."""
    with pytest.raises(RyaError) as e:
        S.resolve_scope("per-tenant")
    assert e.value.code == "E_SCOPE_UNKNOWN"
    assert S.SCOPE_TENANT in (e.value.hint or "")
    assert S.resolve_scope(None, env={}) == S.SCOPE_VERSION
    assert S.resolve_scope(None, env={S.SCOPE_ENV: "tenant"}) == S.SCOPE_TENANT


def test_a_version_scoped_key_is_spelled_exactly_as_it_always_was():
    """The supervisor compares what it launched against what registered itself. If
    those two spellings drift the fleet reads as permanently understaffed and it
    starts a worker every tick, forever — which is why both now go through one
    function, and why this pins the old format character for character."""
    assert S.scope_key("acme", "support", "ver_7") == "acme:support:ver_7"
    assert S.scope_key("acme", "support", None) == "acme:support:local"
    assert S.scope_key("acme", scope=S.SCOPE_TENANT) == "acme:*:*"

    from rya.execution.drivers import WorkerSpec
    from rya.worker import WorkerKey

    spec = WorkerSpec(workspace="acme", agent="support", version_id="ver_7")
    assert spec.key == WorkerKey("acme", "support", "ver_7").concurrency_key()
    wide = WorkerSpec(workspace="acme", agent="", scope=S.SCOPE_TENANT)
    assert wide.key == WorkerKey("acme", "", scope=S.SCOPE_TENANT).concurrency_key()


def test_parse_key_distinguishes_every_agent_from_the_empty_agent():
    """"This claimer serves every agent" and "this claimer serves the agent named by
    the empty string" are different statements and the second one is a bug."""
    assert S.parse_key("acme:*:*") == {"scope": "tenant", "workspace": "acme",
                                       "agent": None, "versionId": None}
    assert S.parse_key("acme:support:local")["versionId"] is None
    assert S.parse_key("acme:support:v7")["agent"] == "support"
    with pytest.raises(RyaError):
        S.parse_key("not-a-key")


# ---- criterion 1: one sandbox, not ten -------------------------------------

def test_five_agents_with_two_versions_each_occupy_one_claimer(tmp_path):
    """§7 criterion 1, and the whole economic claim of the phase.

    The narrow scope wants a worker per (agent, version) — ten here. The wide scope
    wants one per tenant, and the ten live versions become ten *warm interpreters
    inside it*, which is what §7.1 means by "the expensive thing was never the
    process, it is the sandbox".
    """
    from rya.execution.drivers import LocalDriver
    from rya.execution.supervisor import Supervisor, SupervisorPolicy

    root, store = _tenant(tmp_path)
    for n in range(5):
        agent = f"agent{n}"
        project = _project(tmp_path, agent)
        v1 = _publish(root, store, project, agent)
        (project / "src" / "agent.py").write_text((ENTRYPOINT % agent) + "\n# v2\n")
        v2 = _publish(root, store, project, agent)
        _enqueue(store, v1, agent, 1)   # a run still pinned to the old version
        _enqueue(store, v2, agent, 2)   # and one on the new one

    narrow = Supervisor(store, LocalDriver(), project_root=root,
                        policy=SupervisorPolicy(require_lease=False))
    wide = Supervisor(store, LocalDriver(), project_root=root, scope=S.SCOPE_TENANT,
                      policy=SupervisorPolicy(require_lease=False))
    narrow_keys = {a.key for a in narrow.plan()}
    wide_keys = {a.key for a in wide.plan()}

    assert len(narrow_keys) == 10, narrow_keys
    assert wide_keys == {"default:*:*"}
    # And the pool inside it is sized for the working set rather than for one entry.
    assert default_pool_size(S.SCOPE_TENANT, env={}) >= 10


def test_a_rollout_does_not_transiently_double_the_sandbox_count(tmp_path):
    """§7 criterion 2. The narrow scope's sharp edge: `support` needs v7 for the runs
    pinned to it and v8 for the new ones, so every promotion doubles a key until the
    last v7 run drains. Here they are two groups behind one key."""
    from rya.execution.drivers import LocalDriver
    from rya.execution.supervisor import Supervisor, SupervisorPolicy

    root, store = _tenant(tmp_path)
    project = _project(tmp_path, "support")
    v7 = _publish(root, store, project, "support", promote="prod")
    _enqueue(store, v7, "support", 7)
    policy = SupervisorPolicy(require_lease=False)
    wide = Supervisor(store, LocalDriver(), project_root=root, scope=S.SCOPE_TENANT,
                      environment="prod", policy=policy)
    before = {a.key for a in wide.plan()}

    (project / "src" / "agent.py").write_text((ENTRYPOINT % "support") + "\n# v8\n")
    v8 = _publish(root, store, project, "support", promote="prod")
    _enqueue(store, v8, "support", 8)
    after = {a.key for a in wide.plan()}

    assert before == after == {"default:*:*"}
    # Both versions are genuinely live and genuinely distinct — the test would pass
    # trivially if the promotion had not happened.
    assert v7["bundleHash"] != v8["bundleHash"]
    assert len(S.peek(store)) == 2


def test_an_approval_resuming_on_a_retired_version_needs_no_dedicated_sandbox(tmp_path):
    """§7 criterion 3. D12 retains a retired version while runs are pinned to it, and
    at the narrow scope the supervisor REFUSES to start a worker for one — correctly,
    since that worker would raise E_VERSION_RETIRED on every attempt. So the resume
    waits for a human. At tenant scope the claimer forks it."""
    from rya.execution.drivers import LocalDriver
    from rya.execution.supervisor import Supervisor, SupervisorPolicy

    root, store = _tenant(tmp_path)
    project = _project(tmp_path, "support")
    old = _publish(root, store, project, "support")
    _enqueue(store, old, "support", 1)
    deployments.retire(store, old["id"], force=True)
    policy = SupervisorPolicy(require_lease=False)

    narrow = Supervisor(store, LocalDriver(), project_root=root, policy=policy)
    assert [a.key for a in narrow.plan()] == []          # nothing startable

    wide = Supervisor(store, LocalDriver(), project_root=root, scope=S.SCOPE_TENANT,
                      policy=policy)
    assert [a.key for a in wide.plan()] == ["default:*:*"]


# ---- criterion 4: the hole is still found before the claim -----------------

def test_a_handler_set_hole_is_detected_before_the_job_is_claimed(tmp_path):
    """§7 criterion 4, and the guarantee §7.1 predicted this scope would lose.

    It does not, because of the ORDER: the claimer peeks (a read), warms the version
    (the import, and therefore the preflight), and only then forks a child that
    claims. So the refusal happens with the item still pending and its attempt budget
    untouched — which is what "before claiming" has to mean to be worth anything.
    """
    root, store = _tenant(tmp_path)
    broken = _project(tmp_path, "broken", entrypoint=HOLE,
                      tools=[{"id": "missing", "permission": "allowed"}])
    version = _publish(root, store, broken, "broken")
    turn = _enqueue(store, version, "broken", 1)

    w = _claimer(root, store)
    try:
        tick = w.drain_once()
        assert tick["count"] == 0
        assert "E_HANDLER_SET_INCOMPLETE" not in (tick.get("error") or "")
        assert "missing" in tick["error"]
        # The item was never claimed: still pending, still on attempt zero.
        pending = store.queue_get(turn["turnId"])
        assert pending["status"] == "pending"
        assert int(pending.get("attempts") or 0) == 0
        assert pending.get("workerId") is None
    finally:
        w.executor.close()


def test_one_agents_broken_bundle_does_not_stop_a_sibling(tmp_path):
    """The corollary, and the reason the failure is per group rather than per tick.

    At the narrow scope a fork error ends the drain loop, on purpose: one bundle, one
    agent, so "the interpreter holding this tenant's code keeps dying" was the whole
    worker's story. Here it is one agent of two, and ending the tick would let a bad
    deploy of `broken` become an outage for `support`.
    """
    root, store = _tenant(tmp_path)
    good = _publish(root, store, _project(tmp_path, "support"), "support")
    bad = _publish(root, store,
                   _project(tmp_path, "broken", entrypoint=HOLE,
                            tools=[{"id": "missing", "permission": "allowed"}]),
                   "broken")
    _enqueue(store, bad, "broken", 1)
    _enqueue(store, good, "support", 2)

    w = _claimer(root, store)
    try:
        tick = w.drain_once()
        assert tick["count"] == 1               # support ran
        assert "broken" in (tick.get("error") or "")
        assert _run_of(store, tick["turns"][0])["status"] == "completed"
    finally:
        w.executor.close()


# ---- criterion 5: fairness within a tenant ---------------------------------

def test_one_agent_cannot_starve_a_sibling_inside_one_claimer(tmp_path):
    """§7 criterion 5, and it is new work rather than a bug that was always there:
    at the narrow scope one claimer served one agent, so the question could not
    arise. `concurrency_key` answers it BETWEEN tenants; this answers it within one.

    `support` has a backlog and `billing` has a single turn. Serving strictly by
    depth means `billing` waits for the whole backlog, so the round-robin has to
    reach it inside the first couple of dispatches.
    """
    root, store, support, billing = _two_agents(tmp_path)
    for n in range(8):
        _enqueue(store, support, "support", n)
    _enqueue(store, billing, "billing", 99)

    w = _claimer(root, store, turn_limit=3)
    try:
        tick = w.drain_once()
        assert tick["count"] == 3
        agents = [_run_of(store, tid)["agent"] for tid in tick["turns"]]
        assert "billing" in agents, agents
        assert "support" in agents, agents
    finally:
        w.executor.close()


def test_fair_order_gives_equal_dispatches_not_depth_weighted_ones():
    """The policy, without processes. Equal turns is the strongest thing that can be
    said honestly — a weighted scheme needs a per-item service-time estimate and the
    platform does not have one, which is the same reason `backlog_per_worker` is a
    queue-length heuristic."""
    deep = S.Claimable(workspace="acme", agent="support", version_id="v8", depth=40)
    shallow = S.Claimable(workspace="acme", agent="billing", version_id="v2", depth=1)
    order = S.FairOrder()

    served = []
    for _ in range(6):
        nxt = order.order([deep, shallow])[0]
        order.served(nxt)
        served.append(nxt.agent)
    assert served == ["support", "billing"] * 3


def test_the_deep_group_still_gets_every_dispatch_nobody_else_wants():
    """Fairness redistributes throughput; it does not discard it. A group with
    nothing pending is not a candidate at all."""
    deep = S.Claimable(workspace="acme", agent="support", version_id="v8", depth=40)
    order = S.FairOrder()
    order.served(deep, 10)
    assert order.order([deep]) == [deep]


# ---- the peek --------------------------------------------------------------

def test_unattributed_work_is_reported_and_refused_rather_than_guessed(tmp_path):
    """An item whose metadata names no agent has no handler set to be executed
    against. Running it against whichever bundle happened to be warm is D22's
    cross-agent execution path, chosen deliberately — so it is counted, logged and
    left alone, which is the same call `Supervisor.plan` makes on the same rows."""
    root, store, support, _billing = _two_agents(tmp_path)
    from rya import queue as q

    q.enqueue(store, "chat-turn", {"runId": "r_orphan"})   # no agent in metadata
    groups = S.peek(store)
    assert [g.agent for g in groups] == [""]
    assert groups[0].attributed is False

    w = _claimer(root, store)
    try:
        tick = w.drain_once()
        assert tick["count"] == 0
        assert w.executor.unroutable == 1
        # And it does not read as depth, so the claimer can still go idle.
        assert w.queue_depth() == 0
    finally:
        w.executor.close()


def test_a_due_job_with_no_pin_resolves_to_the_promoted_version(tmp_path):
    """The `jobs` primitive records no version, so "new work goes to the promoted
    version" is the answer — read from the environment pointer, which is the same
    question the api asks to pin a run. Shared with the supervisor, because the
    version it schedules for and the version the claimer forks have to agree."""
    root, store = _tenant(tmp_path)
    project = _project(tmp_path, "support")
    v1 = _publish(root, store, project, "support", promote="prod")
    group = S.Claimable(workspace="default", agent="support", version_id=None, depth=1)

    assert S.resolve_version(store, group, environment="prod") == v1["id"]
    assert S.resolve_version(store, group, environment=None) is None
    assert S.resolve_version(store, group, environment="staging") is None


# ---- execution actually happens, in the right interpreter ------------------

def test_two_agents_run_in_two_interpreters_inside_one_claimer(tmp_path):
    """D3 survives the wide scope verbatim: a run still executes in a process that
    imported exactly one bundle. What moved is the *sandbox* boundary, not the
    process one — so the two agents report different import pids, and neither is the
    claimer's."""
    root, store, support, billing = _two_agents(tmp_path)
    _enqueue(store, support, "support", 1)
    _enqueue(store, billing, "billing", 2)
    # A snapshot, not an absolute: `load_agent` gives every imported entrypoint a
    # `rya_user_agent_*` module name and other tests in this process have left theirs
    # behind, so the claim is that THIS claimer added none.
    before = {m for m in sys.modules if m.startswith("rya_user_agent_")}

    w = _claimer(root, store, turn_limit=4)
    try:
        tick = w.drain_once()
        assert tick["count"] == 2
        outputs = {}
        for turn_id in tick["turns"]:
            run = _run_of(store, turn_id)
            assert run["status"] == "completed"
            outputs[run["agent"]] = run["output"]
        assert set(outputs) == {"support", "billing"}
        assert outputs["support"]["importedIn"] != outputs["billing"]["importedIn"]
        assert outputs["support"]["importedIn"] != os.getpid()
        # Two warm interpreters, one claimer, and no tenant import in this process.
        assert len(w.executor.pool) == 2
        assert {m for m in sys.modules if m.startswith("rya_user_agent_")} == before
    finally:
        w.executor.close()


def test_the_registration_says_which_scope_and_which_agents(tmp_path):
    """The same row means different things at the two scopes: at `version` the agent
    and versionId columns describe what the process serves, and at `tenant` they are
    empty and `handlers.agents` is where the answer lives. An operator reading
    `GET /workers` needs to be able to tell which."""
    root, store, support, billing = _two_agents(tmp_path)
    _enqueue(store, support, "support", 1)

    w = _claimer(root, store)
    try:
        w.preflight()
        record = w.register()
        assert record["scope"] == S.SCOPE_TENANT
        assert record["concurrencyKey"] == "default:*:*"
        assert record["agent"] == "" and record["versionId"] is None
        assert record["mode"] == "fork-tenant"
        w.drain_once()
        assert set(w.advertise()["agents"]) == {"support"}
    finally:
        w.executor.close()


def test_inline_mode_cannot_be_tenant_scoped(tmp_path):
    """A contradiction rather than a missing feature: inline mode holds one import
    for the life of the process (D3 — `load_agent` mutates sys.path and never unwinds
    it), so a tenant-scoped inline claimer would have to pick one of its tenant's
    agents at startup and would then be a version-scoped claimer with a misleading
    key."""
    root, store = _tenant(tmp_path)
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, scope=S.SCOPE_TENANT, fork=False)
    assert e.value.code == "E_FORK_UNAVAILABLE"
    assert "--scope version" in (e.value.hint or "")


# ---- pre-warming -----------------------------------------------------------

def test_prewarming_warms_interpreters_inside_one_claimer(tmp_path):
    """§7's pre-warm item, and the wide scope changes what it costs. The narrow scope
    pre-warmed by keeping a whole extra SANDBOX alive per key; this keeps an
    interpreter warm inside a sandbox the tenant already needs — the same latency win
    at a fraction of the idle cost §6 names as *the* constraint."""
    root, store = _tenant(tmp_path)
    support = _publish(root, store, _project(tmp_path, "support"), "support",
                       promote="prod")
    _publish(root, store, _project(tmp_path, "billing"), "billing", promote="staging")

    w = _claimer(root, store, environment="prod", prewarm=("prod",))
    try:
        handlers = w.preflight()
        assert w.prewarmed == [{"agent": "support", "versionId": support["id"]}]
        # Only `prod` was asked for, so `billing` is not warm.
        assert set(handlers["agents"]) == {"support"}
        assert len(w.executor.pool) == 1
    finally:
        w.executor.close()


def test_prewarming_is_empty_by_default(tmp_path):
    """Warming every promoted agent would defeat the scale-to-zero the execution
    plane just made two-way. An operator who wants the first prod request to skip a
    cold start opts in per environment and pays for it knowingly."""
    root, store = _tenant(tmp_path)
    _publish(root, store, _project(tmp_path, "support"), "support", promote="prod")
    w = _claimer(root, store, environment="prod")
    try:
        assert w.preflight()["agents"] == {}
        assert len(w.executor.pool) == 0
    finally:
        w.executor.close()


def test_a_broken_prewarm_target_is_skipped_not_fatal(tmp_path):
    """At the narrow scope a bundle that will not import is correctly a startup
    failure — it is the only thing that process serves. Here refusing to serve four
    healthy agents because a fifth has a broken bundle would be the wrong trade."""
    root, store = _tenant(tmp_path)
    good = _publish(root, store, _project(tmp_path, "support"), "support",
                    promote="prod")
    bad = _publish(root, store,
                   _project(tmp_path, "broken", entrypoint="raise RuntimeError('boom')"),
                   "broken", promote="prod")
    assert bad["id"] != good["id"]

    w = _claimer(root, store, environment="prod", prewarm=("prod",))
    try:
        w.preflight()
        assert [p["agent"] for p in w.prewarmed] == ["support"]
    finally:
        w.executor.close()


# ---- the pool's ceiling ----------------------------------------------------

def test_the_pool_ceiling_follows_the_scope_and_the_environment_wins():
    """A ceiling of 4 at tenant scope would evict and re-import continuously for
    §7.1's own worked example (5 agents, 2 live versions), and every eviction would
    look like a cold start on somebody's next turn."""
    assert default_pool_size(S.SCOPE_VERSION, env={}) == 4
    assert default_pool_size(S.SCOPE_TENANT, env={}) == 12
    assert default_pool_size(S.SCOPE_TENANT, env={"RYA_POOL_MAX_ENTRIES": "3"}) == 3
    # A nonsense value falls back rather than crashing a claimer at boot.
    assert default_pool_size(S.SCOPE_TENANT, env={"RYA_POOL_MAX_ENTRIES": "lots"}) == 12


# ---- one broker, every agent (D18 at the wide scope) -----------------------

MEDIATED = '''
import os
from rya import define_agent

agent = define_agent()

@agent.on_event
async def main(ctx, event):
    await ctx.llm.respond(system="s", input={"v": event.payload.get("v")})
    return {"agent": %r, "pid": os.getpid(),
            "storeKind": type(ctx.store).__name__,
            "dsn": os.environ.get("RYA_DATABASE_URL"),
            "providerKey": os.environ.get("ANTHROPIC_API_KEY"),
            "secret": ctx.secrets.get("AGENT_TOKEN")}
'''


def _mediated_pair(tmp_path, monkeypatch):
    """Two agents, each declaring its OWN model route and its own secret.

    Different routes on purpose: one broker now serves both, and the thing that has
    to be per-agent is exactly what a manifest declares. A broker that resolved one
    config and used it for everyone would hand `billing`'s handler `support`'s model
    — and, worse, `support`'s secrets.
    """
    root, store = _tenant(tmp_path)
    published = {}
    for name, model, token in (("support", "mock-support", "tok-support"),
                               ("billing", "mock-billing", "tok-billing")):
        project = _project(tmp_path, name, entrypoint=MEDIATED % name)
        p = project / "rya.agent.yaml"
        doc = yaml.safe_load(p.read_text())
        # The `model:` block, not the `models:` registry: `resolve_run_config` builds
        # routes from the former, which is what a handler's `ctx.llm` actually calls.
        doc["model"] = {"provider": "mock", "default": model}
        doc["secrets"] = ["AGENT_TOKEN"]
        p.write_text(yaml.safe_dump(doc))
        (project / ".env").write_text(f"AGENT_TOKEN={token}\n")
        monkeypatch.setenv("AGENT_TOKEN", token)   # one deployment-level value
        published[name] = _publish(root, store, project, name)
    return root, store, published


def test_one_broker_serves_every_agent_with_its_own_config(tmp_path, monkeypatch):
    """D18 at the wide scope. The claimer holds one broker and both agents reach it,
    each getting the routes its own version's manifest declares — resolved from the
    persisted manifest (D21), which is the second reader of that decision.

    The credential assertions come from inside the handlers, so this is the tenant's
    own view of what it holds rather than the platform's claim about it.
    """
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    root, store, published = _mediated_pair(tmp_path, monkeypatch)
    for name, version in published.items():
        _enqueue(store, version, name, 1)

    w = _claimer(root, store, turn_limit=4, mediated=True)
    try:
        broker = w.executor.broker
        assert broker is not None
        # No default agent: the capability names one per dispatch, and a default here
        # would be a route around that.
        assert broker.agent == ""
        tick = w.drain_once()
        assert tick["count"] == 2

        outputs = {}
        for turn_id in tick["turns"]:
            run = _run_of(store, turn_id)
            assert run["status"] == "completed", run.get("error")
            outputs[run["agent"]] = run["output"]
        assert set(outputs) == {"support", "billing"}
        for name, out in outputs.items():
            assert out["storeKind"] == "BrokerStore"
            assert out["dsn"] is None and out["providerKey"] is None
        # Both handlers reached `ctx.secrets` and got the DEPLOYMENT's value, which is
        # the correct answer and worth pinning because it is easy to expect the other
        # one: a secret is per-deployment config (D8), and what a manifest declares is
        # which names an agent expects to find — not a private copy of them. The
        # per-agent thing is the model route, asserted below and in the meter rows.
        assert outputs["support"]["secret"] == outputs["billing"]["secret"]
        assert outputs["support"]["secret"] in ("tok-support", "tok-billing")
    finally:
        w.executor.close()


def test_the_broker_meters_each_agent_separately(tmp_path, monkeypatch):
    """D30's billing record, written by the platform rather than by the billed party —
    and at this scope it has to attribute correctly across agents, because one broker
    served both and `agent` comes from the capability rather than from the payload."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    root, store, published = _mediated_pair(tmp_path, monkeypatch)
    for name, version in published.items():
        _enqueue(store, version, name, 1)

    w = _claimer(root, store, turn_limit=4, mediated=True)
    try:
        assert w.drain_once()["count"] == 2
        rows = [r for r in store.meter_read() if str(r.get("kind") or "").startswith("llm.")]
        assert {r["agent"] for r in rows} == {"support", "billing"}
        assert {r["source"] for r in rows} == {"broker"}
        # And each row names the model that agent's OWN manifest declared, which is
        # the attribution one broker serving two agents has to get right.
        by_agent = {r["agent"]: r["model"] for r in rows}
        assert by_agent == {"support": "mock-support", "billing": "mock-billing"}
    finally:
        w.executor.close()


def test_the_public_routes_projection_keeps_the_key_on_the_platform_side(tmp_path,
                                                                        monkeypatch):
    """The projection lives on the broker, next to the credential, so there is exactly
    one version of "what a RunConfig looks like from outside". It used to be computed by
    the claimer's executor, which was harmless while the executor also held the real
    config and became a second place to get it wrong once the config went per-agent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    root, store, published = _mediated_pair(tmp_path, monkeypatch)

    w = _claimer(root, store, mediated=True)
    try:
        broker = w.executor.broker
        from rya.config import DEFAULT_ROUTE

        routes = broker.public_routes(agent="support",
                                      version_id=published["support"]["id"])
        assert routes[DEFAULT_ROUTE]["model"] == "mock-support"
        assert set(routes[DEFAULT_ROUTE]) == {"provider", "model", "temperature",
                                              "maxTokens"}
        # The real route, on this side, is a full ModelRoute with the credential
        # fields the projection dropped.
        real = broker.config_for("support", published["support"]["id"]).route(None)
        assert real.model == "mock-support"
        assert hasattr(real, "api_key") and hasattr(real, "base_url")
        # And a different agent resolves a different config rather than a cached one.
        other = broker.public_routes(agent="billing",
                                     version_id=published["billing"]["id"])
        assert other[DEFAULT_ROUTE]["model"] == "mock-billing"
    finally:
        w.executor.close()


def test_closing_the_claimer_removes_the_brokers_socket_directory(tmp_path, monkeypatch):
    """A Phase 4 leak, found by counting `/tmp` after Phase 5's tests ran.

    `ForkExecutor.close` stopped the pool and left the `BrokerServer` running, so every
    mediated claimer that exited left behind a 0700 temp directory with a stale socket
    in it. Invisible on a laptop and one directory per restart on a box that recycles
    claimers — which is what a supervisor does for a living.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    root, store, _published = _mediated_pair(tmp_path, monkeypatch)

    w = _claimer(root, store, mediated=True)
    socket_dir = w.executor.broker.socket_path.parent
    assert socket_dir.is_dir()
    w.executor.close()
    assert not socket_dir.exists(), sorted(p.name for p in socket_dir.iterdir())


def test_the_tenant_scope_implies_fork_rather_than_demanding_it(tmp_path, monkeypatch):
    """The CLI help has always said `--scope tenant` "implies --fork", and
    `drivers.worker_argv` has always rendered both. `start_worker` disagreed.

    The visible symptom was a misdirected error rather than a refusal to work: with
    mediation on, `--scope tenant` reported "Broker mediation was requested without
    --fork", which names a flag the operator had no reason to type — they asked for a
    scope that mandates forking. The cause is the ordering, which is the same class as
    every other ordering bug in `worker.py`: a check that ran before the thing it
    checks had been decided.

    Found by the e2e in Phase 6, and worth a unit test because the e2e only exercises
    it with mediation on and the ordering matters in both directions.
    """
    root, store = _tenant(tmp_path)
    monkeypatch.setenv("RYA_BROKER", "1")
    # No `fork=True`, and mediation on. Before the fix this raised.
    w = start_worker(project_root=root, store=store, scope=S.SCOPE_TENANT)
    try:
        assert w.executor.mode.startswith("fork-tenant")
        assert w.key.tenant_scoped is True
    finally:
        w.executor.close()


def test_passing_fork_false_explicitly_at_tenant_scope_is_still_refused(tmp_path):
    """An omission and a statement are different things.

    `start_worker(..., fork=False, scope="tenant")` is reachable from Python and says
    something contradictory — inline mode binds one import to the process for its whole
    life (D3), so it can serve one agent-version and not a tenant. That is refused
    rather than silently upgraded, which is the opposite treatment from an omitted flag
    and the right one.
    """
    root, store = _tenant(tmp_path)
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, scope=S.SCOPE_TENANT, fork=False)
    assert e.value.code == "E_FORK_UNAVAILABLE"
    assert "--scope version" in (e.value.hint or "")
