"""The scheduling policy (D25, issue #16).

Everything here drives ``Supervisor.plan``, which is a pure function of (worker
registry, claimable depth, quota). That is deliberate: a scheduler is the subtlest
component in the multi-tenant design (§9 risk 7), and the interesting questions —
does a crashed key get replaced, does `maxWorkers` refuse the third worker, does a
retired version get scheduled — are all answerable without launching a process.

The parity tests at the bottom are the Phase 3 exit criterion in test form: the
same policy, over two drivers declaring different isolation, must produce the same
plan. Nothing in the policy layer may branch on substrate.
"""

from datetime import datetime, timedelta, timezone

import pytest

from rya import queue as q
from rya.errors import RyaError
from rya.execution import drivers as D
from rya.execution.supervisor import REAP, START, Supervisor, SupervisorPolicy, claimable_by_key
from rya.store import WORKER_LOST_SECONDS, Store

from test_execution import FakeDocker

WS = "default"


def _store(tmp_path):
    s = Store(tmp_path / "state")
    s.ensure()
    return s


def _sup(store, driver=None, *, policy=None, project_root=None, environment=None):
    return Supervisor(store, driver or FakeDocker(), workspace=WS,
                      environment=environment, project_root=project_root,
                      policy=policy or SupervisorPolicy(max_replicas_per_key=4))


def _turn(store, *, agent="support", version="ver_7", n=1):
    for _ in range(n):
        q.enqueue(store, "chat-turn", {"type": "message.received"},
                  metadata={"agent": agent, "versionId": version})


def _worker(store, *, agent="support", version="ver_7", worker_id="wrk_1", lost=False):
    from rya.worker import WorkerKey

    key = WorkerKey(WS, agent, version)
    rec = store.worker_register({"id": worker_id, "workspaceId": WS, "agent": agent,
                                 "versionId": version,
                                 "concurrencyKey": key.concurrency_key()})
    if lost:
        then = datetime.now(timezone.utc) - timedelta(seconds=WORKER_LOST_SECONDS + 30)
        rec["lastHeartbeatAt"] = then.strftime("%Y-%m-%dT%H:%M:%SZ")
        store._write(store.workers_dir / f"{rec['id']}.json", rec)
    return rec


def _version(store, *, agent="support", version_id="ver_7", state="active"):
    """A version record, written directly — the supervisor only ever reads it, and
    building a real bundle here would test `bundles`, not scheduling."""
    store._write(store.versions_dir / f"{version_id}.json",
                 {"id": version_id, "agent": agent, "bundleHash": "h" * 8,
                  "state": state, "createdAt": "2026-01-01T00:00:00Z"})
    return version_id


# ---- the gap this closes ----------------------------------------------------

def test_a_key_with_work_and_no_worker_gets_one_started(tmp_path):
    """The Phase 3 exit criterion. Before this, scale-to-zero was one-way: a worker
    idled out and the key stayed unserved until a human noticed."""
    store = _store(tmp_path)
    _version(store)
    _turn(store)
    sup = _sup(store)

    actions = sup.plan()
    assert [(a.kind, a.key) for a in actions] == [(START, f"{WS}:support:ver_7")]
    assert actions[0].spec.version_id == "ver_7"
    assert "depth 1" in actions[0].reason

    applied = sup.apply(actions)
    assert applied[0]["ok"] is True
    assert [h.key for h in sup.driver.list()] == [f"{WS}:support:ver_7"]


def test_a_key_that_is_already_served_is_left_alone(tmp_path):
    store = _store(tmp_path)
    _version(store)
    _turn(store)
    _worker(store)
    assert _sup(store).plan() == []


def test_no_work_means_no_workers(tmp_path):
    """An empty plan on an idle deployment is the designed steady state (§6), not a
    supervisor that failed to notice anything."""
    store = _store(tmp_path)
    _version(store)
    assert _sup(store).plan() == []


def test_a_key_scaled_to_zero_is_restarted_when_work_arrives(tmp_path):
    """Two-way scale-to-zero, end to end: a worker exits idle, its registration
    goes `stopped`, and the next item brings the key back."""
    store = _store(tmp_path)
    _version(store)
    rec = _worker(store)
    store.worker_deregister(rec["id"], "idle")
    sup = _sup(store)
    assert sup.plan() == []

    _turn(store)
    assert [a.kind for a in sup.plan()] == [START]


# ---- replicas ---------------------------------------------------------------

def test_replicas_scale_with_backlog(tmp_path):
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=12)
    policy = SupervisorPolicy(backlog_per_worker=5, max_replicas_per_key=4)
    actions = _sup(store, policy=policy).plan()
    assert len(actions) == 3          # ceil(12/5)
    assert {a.kind for a in actions} == {START}


def test_replicas_are_capped_per_key(tmp_path):
    """A backlog is not a licence to occupy the whole fleet — the same instinct
    `concurrency_key` encodes for the queue (§6: one workspace must not starve
    another), applied to scheduling."""
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=500)
    policy = SupervisorPolicy(backlog_per_worker=1, max_replicas_per_key=2)
    assert len(_sup(store, policy=policy).plan()) == 2


def test_an_existing_worker_counts_against_the_replica_target(tmp_path):
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=12)
    _worker(store)
    policy = SupervisorPolicy(backlog_per_worker=5, max_replicas_per_key=4)
    assert len(_sup(store, policy=policy).plan()) == 2   # 3 wanted, 1 live


# ---- maxWorkers at schedule time -------------------------------------------

def test_maxworkers_is_enforced_when_scheduling_not_only_when_registering(tmp_path):
    """The exit criterion. `Worker.register` checked this already, but refusing
    there means the launch already happened — a container pulled and started so it
    could exit on a quota error. Deciding here is the difference between a cap and
    a receipt."""
    from rya.quotas import set_quota

    store = _store(tmp_path)
    set_quota(store, {"maxWorkers": 2})
    _version(store)
    _turn(store, n=100)
    policy = SupervisorPolicy(backlog_per_worker=1, max_replicas_per_key=10)
    assert len(_sup(store, policy=policy).plan()) == 2


def test_maxworkers_counts_workers_already_running(tmp_path):
    from rya.quotas import set_quota

    store = _store(tmp_path)
    set_quota(store, {"maxWorkers": 2})
    _version(store)
    _turn(store, n=100)
    _worker(store)
    policy = SupervisorPolicy(backlog_per_worker=1, max_replicas_per_key=10)
    assert len(_sup(store, policy=policy).plan()) == 1


def test_a_crashed_worker_does_not_hold_a_maxworkers_slot_against_its_replacement(tmp_path):
    """Before the liveness fix this arithmetic could not have been written. A
    SIGKILLed worker stayed `alive` forever, so a crash-looping key would have
    exhausted `maxWorkers` and the supervisor would have concluded — correctly from
    the data it had — that the tenant was out of capacity."""
    from rya.quotas import set_quota

    store = _store(tmp_path)
    set_quota(store, {"maxWorkers": 1})
    _version(store)
    _turn(store)
    _worker(store, lost=True)

    kinds = [a.kind for a in _sup(store).plan()]
    assert kinds == [REAP, START]


# ---- reaping ---------------------------------------------------------------

def test_a_lost_worker_is_reaped_and_the_reap_is_planned_first(tmp_path):
    """Applied in order, so a replacement never registers alongside a dead row —
    the second tick would otherwise see a fleet it did not schedule."""
    store = _store(tmp_path)
    _version(store)
    _turn(store)
    rec = _worker(store, lost=True)
    sup = _sup(store)

    actions = sup.plan()
    assert actions[0].kind == REAP and actions[0].worker_id == rec["id"]
    assert "no heartbeat" in actions[0].reason

    sup.apply(actions)
    after = next(w for w in store.worker_list() if w["id"] == rec["id"])
    assert (after["status"], after["stopReason"]) == ("stopped", "lost")


def test_reaping_keeps_the_row_rather_than_deleting_it(tmp_path):
    """"This key kept dying" is the history an operator needs most."""
    store = _store(tmp_path)
    rec = _worker(store, lost=True)
    sup = _sup(store)
    sup.apply(sup.plan())
    assert [w["id"] for w in store.worker_list()] == [rec["id"]]


def test_a_lost_worker_with_no_work_is_reaped_without_a_replacement(tmp_path):
    """Reaping is bookkeeping, not a reason to start something. A key with no
    claimable work does not want a worker whether or not one just died."""
    store = _store(tmp_path)
    _worker(store, lost=True)
    assert [a.kind for a in _sup(store).plan()] == [REAP]


# ---- what must not be scheduled -------------------------------------------

def test_a_retired_version_is_not_started(tmp_path):
    """D12 retains a version while runs are pinned to it, so a retired version with
    queued work is a real state — and the right response is to refuse, not to start
    a worker that raises E_VERSION_RETIRED every few seconds forever."""
    store = _store(tmp_path)
    _version(store, state="retired")
    _turn(store)
    assert _sup(store).plan() == []


def test_a_version_that_does_not_exist_is_not_started(tmp_path):
    store = _store(tmp_path)
    _turn(store, version="ver_ghost")
    assert _sup(store).plan() == []


def test_an_unpinned_key_needs_a_mounted_project(tmp_path):
    """A working-tree worker with no tree has nothing to load."""
    store = _store(tmp_path)
    _turn(store, version=None)
    assert _sup(store).plan() == []
    assert [a.kind for a in _sup(store, project_root=tmp_path).plan()] == [START]


def test_unattributed_work_names_no_key_to_start(tmp_path):
    """A queue item with no agent is claimable by anyone, so it is depth for
    whatever is running but cannot be turned into a scheduling decision. Inventing
    an agent for it would launch a worker for a name nobody published."""
    store = _store(tmp_path)
    q.enqueue(store, "chat-turn", {"type": "m"})
    assert _sup(store).plan() == []
    assert [(c.agent, c.depth) for c in claimable_by_key(store)] == [("", 1)]


# ---- pre-warming ----------------------------------------------------------

def test_nothing_is_prewarmed_by_default(tmp_path):
    """Pre-warming every promoted agent would defeat the scale-to-zero this phase
    just made two-way, and §6 names idle cost as *the* constraint."""
    store = _store(tmp_path)
    _version(store)
    store.env_set_current("prod", "support", "ver_7")
    assert _sup(store).plan() == []


def test_a_named_environment_keeps_one_warm_worker(tmp_path):
    store = _store(tmp_path)
    _version(store)
    store.env_set_current("prod", "support", "ver_7")
    policy = SupervisorPolicy(prewarm_environments=("prod",))
    actions = _sup(store, policy=policy).plan()
    assert [(a.kind, a.reason) for a in actions] == [(START, "pre-warm")]


def test_depth_beats_prewarm_as_the_reason(tmp_path):
    """One key, one decision. A pre-warm must not add a second worker on top of the
    one demand already asked for."""
    store = _store(tmp_path)
    _version(store)
    store.env_set_current("prod", "support", "ver_7")
    _turn(store)
    policy = SupervisorPolicy(prewarm_environments=("prod",))
    actions = _sup(store, policy=policy).plan()
    assert len(actions) == 1 and actions[0].reason == "depth 1"


# ---- the two views ---------------------------------------------------------

def test_a_worker_that_is_still_booting_does_not_get_a_duplicate(tmp_path):
    """A worker appears in the registry only once it reached the database, so a
    process that is starting up looks absent. Trusting the registry alone starts a
    duplicate for every key that is mid-boot."""
    store = _store(tmp_path)
    _version(store)
    _turn(store)
    sup = _sup(store)
    sup.apply(sup.plan())            # launched, not yet registered
    assert store.worker_list(status="alive") == []
    assert sup.plan() == []          # the driver's inventory covers the gap


def test_a_driver_that_cannot_answer_falls_back_to_what_was_launched(tmp_path):
    """A driver's inventory can always be stale or partial. It must not take the
    tick down with it."""
    store = _store(tmp_path)
    _version(store)
    _turn(store)

    class Broken(FakeDocker):
        def list(self, key=None):
            raise OSError("the daemon is not listening")

    sup = _sup(store, Broken())
    sup.apply(sup.plan())
    assert sup.plan() == []          # remembered from `started`


def test_one_failed_action_does_not_abort_the_rest(tmp_path):
    """Failure modes here are per-key (a missing image, a bad bundle) far more often
    than global, so a reap must still happen when a start cannot."""
    store = _store(tmp_path)
    _version(store)
    _turn(store)
    _worker(store, worker_id="wrk_dead", lost=True)

    class Refuses(FakeDocker):
        def start(self, spec):
            raise RyaError("E_WORKER_START_FAILED", "no image")

    sup = _sup(store, Refuses())
    done = sup.apply(sup.plan())
    assert [d["ok"] for d in done] == [True, False]
    assert done[1]["error"] == "no image"
    assert store.worker_list()[0]["stopReason"] == "lost"


# ---- the input signal -----------------------------------------------------

def test_claimable_depth_groups_by_agent_and_version(tmp_path):
    store = _store(tmp_path)
    _turn(store, agent="support", version="ver_7", n=2)
    _turn(store, agent="support", version="ver_8", n=1)
    _turn(store, agent="billing", version="ver_2", n=3)

    got = {(c.agent, c.version_id): c.depth for c in claimable_by_key(store)}
    assert got == {("support", "ver_7"): 2, ("support", "ver_8"): 1,
                   ("billing", "ver_2"): 3}


def test_a_rollout_asks_for_both_versions_at_once(tmp_path):
    """#19's sharp edge, from the supervisor's side: `support` needs v7 alive for
    the runs pinned to it and v8 for the new ones, so a promotion transiently
    doubles a key. At the narrow claimer scope that is two workers, by design."""
    store = _store(tmp_path)
    _version(store, version_id="ver_7")
    _version(store, version_id="ver_8")
    _turn(store, version="ver_7")
    _turn(store, version="ver_8")

    keys = sorted(a.key for a in _sup(store).plan())
    assert keys == [f"{WS}:support:ver_7", f"{WS}:support:ver_8"]


def test_a_foreign_queue_job_is_not_a_reason_to_start_a_worker(tmp_path):
    """The queue table serves two product surfaces (D14). A pending `/queue/*` job
    belongs to a foreign consumer — a TypeScript DAG worker — and counting it here
    would have the supervisor start a `rya worker` that will never touch it."""
    store = _store(tmp_path)
    q.enqueue(store, "etl.transform", {"file": "x.csv"},
              metadata={"agent": "support", "versionId": "ver_7"})
    assert claimable_by_key(store) == []
    assert _sup(store).plan() == []


def test_a_due_job_from_the_jobs_primitive_is_claimable_depth(tmp_path):
    """A `jobs` row records no version, so its depth arrives unpinned — and without
    a promoted version to resolve it against there is no key to start."""
    store = _store(tmp_path)
    _version(store)
    store.create_job(None, "later", {}, "1970-01-01T00:00:00Z", agent="support")
    assert [(c.agent, c.depth) for c in claimable_by_key(store)] == [("support", 1)]
    assert _sup(store).plan() == []


def test_unpinned_work_is_scheduled_onto_the_promoted_version(tmp_path):
    """§9: new work goes to the promoted version. A background job has no journal to
    replay, so unlike an approval resume there is no version it *must* run on — and
    holding it back for a version that has been superseded would be the strange
    choice. This is also why the supervisor and the api must agree on which
    environment they are, which is the coupling Phase 2 created."""
    store = _store(tmp_path)
    _version(store, version_id="ver_9")
    store.env_set_current("prod", "support", "ver_9")
    store.create_job(None, "later", {}, "1970-01-01T00:00:00Z", agent="support")

    actions = _sup(store, environment="prod").plan()
    assert [(a.kind, a.key) for a in actions] == [(START, f"{WS}:support:ver_9")]


def test_a_due_job_falls_back_to_the_working_tree_when_nothing_is_promoted(tmp_path):
    """`rya dev`: no pointer, no versions, a mounted tree. The unpinned key is the
    right answer there and the only one available."""
    store = _store(tmp_path)
    store.create_job(None, "later", {}, "1970-01-01T00:00:00Z", agent="support")
    actions = _sup(store, project_root=tmp_path, environment="prod").plan()
    assert [(a.kind, a.key) for a in actions] == [(START, f"{WS}:support:local")]


def test_a_job_scheduled_in_the_future_is_not_depth_yet(tmp_path):
    store = _store(tmp_path)
    store.create_job(None, "later", {}, "2999-01-01T00:00:00Z", agent="support")
    assert claimable_by_key(store) == []


# ---- driver parity (the exit criterion) -----------------------------------

@pytest.mark.parametrize("driver_factory", [
    pytest.param(lambda: D.LocalDriver(), id="local"),
    pytest.param(lambda: FakeDocker(), id="shared-kernel"),
    pytest.param(lambda: FakeDocker(isolation=D.ISOLATION_SANDBOXED), id="sandboxed"),
    pytest.param(lambda: FakeDocker(isolation=D.ISOLATION_MICROVM), id="microvm"),
])
def test_the_same_policy_produces_the_same_plan_on_every_driver(tmp_path, driver_factory):
    """The Phase 3 exit criterion: no branching on substrate in the policy layer.

    Note what is NOT asserted — that every driver launches the same way. They do
    not; that is what a driver is for. What must be identical is the decision, and
    a plan is the decision.
    """
    store = _store(tmp_path)
    _version(store, version_id="ver_7")
    _version(store, version_id="ver_8")
    _turn(store, version="ver_7", n=7)
    _turn(store, version="ver_8", n=1)
    _worker(store, version="ver_8", worker_id="wrk_dead", lost=True)

    sup = _sup(store, driver_factory())
    plan = [(a.kind, a.key, a.reason) for a in sup.plan()]
    assert plan == [
        (REAP, f"{WS}:support:ver_8", "no heartbeat for 150s"),
        (START, f"{WS}:support:ver_7", "depth 7"),
        (START, f"{WS}:support:ver_7", "depth 7"),
        (START, f"{WS}:support:ver_8", "depth 1"),
    ]


def test_cold_start_targets_are_per_driver(tmp_path):
    """`COLD_START_TARGET_MS` was one global number written when a worker meant a
    local process. A Fargate task is tens of seconds; one number two of the four
    drivers cannot hit is worse than four honest ones."""
    assert D.LocalDriver().cold_start_target_ms == 2000
    assert FakeDocker().cold_start_target_ms == 1500


# ---- the singleton guard (Phase 5, open question 7) ------------------------

def test_a_second_supervisor_stands_by_instead_of_doubling_the_fleet(tmp_path):
    """Open question 7, arrived-with-the-`kubernetes`-driver edition.

    The obvious way to run a supervisor on Kubernetes is a Deployment; a Deployment
    is scalable by default; and a second replica does not merely duplicate work. It
    reads the same depth, computes the same target, and then starts its own workers —
    because ``observe`` reconciles the registry against the *driver's* inventory, and
    a second supervisor's driver inventory is empty. So the fleet does not converge
    on 2N by accident; each replica believes it is the only one.
    """
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=1)
    first, second = _sup(store), _sup(store)

    tick = first.tick()
    assert [a["action"] for a in tick["actions"]] == [START]
    assert first.passive is False

    # The standby still observes and still plans — and applies nothing.
    tick = second.tick()
    assert second.passive is True
    assert tick["actions"] == []
    assert [a["action"] for a in tick["withheld"]] == [START]
    assert len(second.driver.started) == 0


def test_the_lease_is_renewed_by_its_holder_and_survives_a_tick(tmp_path):
    """A renewal must not look like a takeover, or `acquiredAt` — the field an
    operator reads to see whether a fleet changed hands — resets every tick."""
    store = _store(tmp_path)
    sup = _sup(store)
    assert sup.hold_lease() is True
    first = dict(sup.lease or {})
    assert first["renewed"] is False
    assert sup.hold_lease() is True
    assert (sup.lease or {})["renewed"] is True
    assert (sup.lease or {})["acquiredAt"] == first["acquiredAt"]


def test_a_released_lease_is_taken_immediately_rather_than_after_the_ttl(tmp_path):
    """A clean exit hands it back so a standby takes over on its next tick. A crash
    skips that, which is what the TTL is for."""
    store = _store(tmp_path)
    first, second = _sup(store), _sup(store)
    assert first.hold_lease() is True
    assert second.hold_lease() is False
    first.release_lease()
    assert second.hold_lease() is True


def test_an_expired_lease_is_taken_over(tmp_path):
    """The crash path. Bounded by the TTL rather than by a human."""
    store = _store(tmp_path)
    policy = SupervisorPolicy(lease_seconds=1.0)
    first = _sup(store, policy=policy)
    assert first.hold_lease() is True
    # Expire it by hand rather than by sleeping: the deadline is a fixed-width UTC
    # ISO string compared with `<`, so this is exactly what the clock would do.
    doc = store.lease_get(first.lease_name)
    doc["expiresAt"] = "2000-01-01T00:00:00Z"
    store._write(store.leases_dir / "supervisor_default.json", doc)

    second = _sup(store, policy=policy)
    assert second.hold_lease() is True
    assert store.lease_get(second.lease_name)["holder"] == second.id


def test_the_lease_is_per_workspace_so_two_supervisors_can_split_a_fleet(tmp_path):
    """Deliberately useful rather than merely safe: two supervisors over a hundred
    tenants share the work instead of one idling."""
    store_a, store_b = _store(tmp_path / "a"), _store(tmp_path / "b")
    a = Supervisor(store_a, FakeDocker(), workspace="acme")
    b = Supervisor(store_b, FakeDocker(), workspace="globex")
    assert a.lease_name != b.lease_name
    assert a.hold_lease() is True
    assert b.hold_lease() is True


def test_a_store_without_leases_still_schedules(tmp_path):
    """A duck-typed third-party store from before this existed. Acting is the
    compatible answer: the alternative is that it silently stops scheduling anything,
    which is a worse failure than the one the lease prevents."""
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=1)
    sup = _sup(store)
    object.__setattr__(sup, "store", _NoLeases(store))
    assert sup.hold_lease() is True
    assert [a.kind for a in sup.plan()] == [START]


class _NoLeases:
    """Everything the supervisor needs except a lease."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name.startswith("lease_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_opting_out_of_the_lease_is_explicit(tmp_path):
    store = _store(tmp_path)
    _version(store)
    _turn(store, n=1)
    policy = SupervisorPolicy(require_lease=False)
    first, second = _sup(store, policy=policy), _sup(store, policy=policy)
    assert first.tick()["actions"]
    # Both act — which is the documented cost of --no-lease, not a surprise.
    assert second.tick()["actions"]
    assert second.passive is False
