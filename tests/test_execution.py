"""The driver seam and the isolation refusal (D26, issue #18).

The property under test is that scheduling policy never learns what substrate it
is on, and that "is Rya safe for untrusted tenants" is a question the platform
answers by refusing rather than by documentation.
"""

import pytest

from rya.errors import RyaError
from rya.execution import drivers as D


class FakeDocker(D.ExecutionDriver):
    """A stand-in for a driver that has not been written yet.

    Its whole job is to prove the supervisor and the isolation check read a
    driver's *declaration* rather than its name. When the real `docker` driver
    lands with #14 it should be substitutable for this without a policy change.
    """

    name = "fake-docker"
    isolation = D.ISOLATION_SHARED_KERNEL
    cold_start_target_ms = 1500

    def __init__(self, isolation=None):
        if isolation is not None:
            self.isolation = isolation
        self.started, self.stopped = [], []
        self._live = {}
        self._n = 0

    def start(self, spec):
        self._n += 1
        handle = D.WorkerHandle(id=f"fk_{self._n}", driver=self.name, spec=spec,
                                native={"container": f"c{self._n}"})
        self.started.append(handle)
        self._live[handle.id] = handle
        return handle

    def stop(self, handle, *, timeout=10.0):
        self.stopped.append(handle)
        self._live.pop(handle.id, None)

    def list(self, key=None):
        return [h for h in self._live.values() if key is None or h.key == key]


# ---- resolution -------------------------------------------------------------

def test_the_driver_defaults_to_local():
    """Same shape as `open_store` and `resolve_bundle_store`: one env var, and the
    zero-config arm is the one that works on a laptop with nothing installed."""
    d = D.resolve_driver(env={})
    assert d.name == "local" and d.isolation == D.ISOLATION_NONE


def test_an_unwritten_driver_says_when_it_arrives_rather_than_that_it_is_a_typo():
    """`RYA_EXECUTION_DRIVER=ecs` is a reasonable request against a roadmap.
    Answering it with the same message as a misspelling wastes the operator's time on
    the wrong hypothesis.

    Phase 4 shipped `docker` and `kubernetes`, so `ecs` is the remaining planned one
    — this test moved with the roadmap rather than being deleted, because the
    behaviour it protects (a planned driver reads differently from a typo) is not
    about which drivers happen to exist today.
    """
    with pytest.raises(RyaError) as e:
        D.resolve_driver(env={D.DRIVER_ENV: "ecs"})
    assert e.value.code == "E_DRIVER_UNKNOWN"
    assert "Fargate" in e.value.message
    assert "local" in (e.value.hint or "")


def test_a_misspelled_driver_lists_what_exists():
    with pytest.raises(RyaError) as e:
        D.resolve_driver(env={D.DRIVER_ENV: "loclal"})
    assert e.value.code == "E_DRIVER_UNKNOWN"
    assert "loclal" in e.value.message and "gVisor" not in e.value.message


# ---- the fail-closed check --------------------------------------------------

def test_untrusted_tenancy_on_a_shared_kernel_driver_fails_at_startup():
    """§9 risk 8: the wrong answer is not reached by MISREADING the documentation,
    it is reached by not consulting it. So this is a refusal, not a warning."""
    with pytest.raises(RyaError) as e:
        D.require_isolation_for_tenancy(FakeDocker(), untrusted=True)
    assert e.value.code == "E_ISOLATION_INSUFFICIENT"
    assert "shared-kernel" in e.value.message and "sandboxed" in e.value.message


def test_the_default_configuration_of_every_driver_refuses_untrusted_tenancy():
    """Was Phase 3's "no driver can carry untrusted tenancy", narrowed by Phase 4 to
    the claim that still holds: a driver must be *configured* for it.

    `docker` with no runtime and `kubernetes` with the RuntimeClass opted out both
    declare less than `sandboxed`, which is the honest answer — a container on the host
    kernel is a shared kernel whatever driver launched it. The point of the original
    criterion survives: the safe direction is the default one.
    """
    for name in sorted(D.DRIVERS):
        driver = D.resolve_driver(env={
            D.DRIVER_ENV: name, D.RUNTIME_ENV: "",
            D.K8S_RUNTIME_CLASS_ENV: D.RUNTIME_CLASS_NONE})
        assert D.isolation_rank(driver.isolation) < D.isolation_rank(D.UNTRUSTED_MIN_ISOLATION), name
        with pytest.raises(RyaError) as e:
            D.require_isolation_for_tenancy(driver, untrusted=True)
        assert e.value.code == "E_ISOLATION_INSUFFICIENT"


def test_the_kubernetes_driver_defaults_to_asking_for_gvisor():
    """The other half of the same question. The default is the sandboxed one, so an
    operator gets isolation without configuring it — and a cluster that cannot honour
    the RuntimeClass refuses the pod rather than running it unsandboxed."""
    driver = D.resolve_driver(env={D.DRIVER_ENV: "kubernetes"})
    assert driver.runtime_class == D.DEFAULT_RUNTIME_CLASS
    assert driver.isolation == D.ISOLATION_SANDBOXED
    assert D.require_isolation_for_tenancy(driver, untrusted=True) is driver


def test_an_explicitly_declared_environment_configures_the_driver_it_selects():
    """D8's own rule, applied to this seam. Passing `env` selected the driver and then
    let it read `os.environ` to configure itself — invisible while `local` read nothing
    from the environment, and decisive once the reading decides whether a driver claims
    to be sandboxed."""
    sandboxed = D.resolve_driver(env={D.DRIVER_ENV: "docker",
                                     D.RUNTIME_ENV: D.GVISOR_RUNTIME,
                                     D.IMAGE_ENV: "rya:test"})
    assert sandboxed.isolation == D.ISOLATION_SANDBOXED
    plain = D.resolve_driver(env={D.DRIVER_ENV: "docker", D.IMAGE_ENV: "rya:test"})
    assert plain.isolation == D.ISOLATION_SHARED_KERNEL


def test_a_sandboxed_driver_is_admitted_for_untrusted_tenancy():
    """The check reads the declaration, not the name. This is what makes #14 a
    driver change rather than a change to this predicate."""
    driver = FakeDocker(isolation=D.ISOLATION_SANDBOXED)
    assert D.require_isolation_for_tenancy(driver, untrusted=True) is driver


def test_trusted_tenancy_is_admitted_on_any_driver():
    """The multi-tenant mode that ships today is deliberately the trusted one, and
    quietly raising its requirements would refuse to start for every existing
    deployment."""
    assert D.require_isolation_for_tenancy(D.LocalDriver(), untrusted=False) is not None


def test_the_posture_is_declared_rather_than_inferred_from_multitenancy():
    """`RYA_MULTITENANT=1` says workspaces are isolated; it does not say tenants are
    hostile. Whether they are is a claim about the business, so it has its own
    flag."""
    assert D.untrusted_tenancy_enabled({"RYA_MULTITENANT": "1"}) is False
    assert D.untrusted_tenancy_enabled({D.UNTRUSTED_ENV: "1"}) is True
    assert D.untrusted_tenancy_enabled({D.UNTRUSTED_ENV: "yes"}) is True
    assert D.untrusted_tenancy_enabled({D.UNTRUSTED_ENV: "0"}) is False


def test_an_isolation_level_this_build_does_not_know_is_treated_as_the_weakest():
    """A driver from a future release naming a level this build has never heard of
    must not be trusted MORE than one it understands."""
    with pytest.raises(RyaError):
        D.require_isolation_for_tenancy(FakeDocker(isolation="hyper-secure"), untrusted=True)


def test_a_driver_that_forgets_to_declare_isolation_inherits_none():
    class Forgetful(D.ExecutionDriver):
        name = "forgetful"

    assert Forgetful().isolation == D.ISOLATION_NONE
    assert Forgetful().describe()["supportsUntrusted"] is False


# ---- what a driver launches -------------------------------------------------

def test_every_driver_builds_the_same_worker_command_line():
    """`worker_argv` lives on the base class so a flag added for one substrate
    cannot silently apply to only that one."""
    spec = D.WorkerSpec(workspace="acme", agent="support", version_id="ver_7",
                        idle_exit_seconds=30, poll_seconds=1.5)
    local = D.LocalDriver().worker_argv(spec)
    fake = FakeDocker().worker_argv(spec)
    assert local == fake
    assert local[-10:] == ["--workspace", "acme", "--interval", "1.5",
                           "--concurrency", "1", "--agent", "support",
                           "--version", "ver_7", "--idle-exit", "30"][-10:]
    assert local[local.index("--version") + 1] == "ver_7"
    assert local[local.index("--agent") + 1] == "support"


def test_an_unpinned_spec_passes_the_environment_instead_of_a_version():
    """No pinned version and an environment named: the worker resolves the pointer
    itself, because the pointer can move between the decision and the launch and
    the resolver should be the process that will serve the result."""
    spec = D.WorkerSpec(workspace="acme", agent="support", environment="prod")
    argv = D.LocalDriver().worker_argv(spec)
    assert "--version" not in argv
    assert argv[argv.index("--env") + 1] == "prod"


def test_the_launched_worker_is_forced_onto_the_specs_environment():
    """Phase 2 paid for this: the api reads the environment pointer to decide a
    run's pin, so an api on `dev` with a worker on `prod` leaves every turn
    unclaimed. A supervisor knows which environment it scheduled for, so it stops
    being a convention."""
    spec = D.WorkerSpec(workspace="acme", agent="support", environment="prod")
    env = D.LocalDriver().worker_env(spec)
    assert env["RYA_ENVIRONMENT"] == "prod"


def test_the_worker_command_is_this_builds_own_interpreter():
    """D5 — one deployable, nothing to version between the halves — only holds if
    the process that schedules launches its OWN code. A `PATH` lookup can resolve
    to a different install."""
    import sys

    assert D.worker_command({}) == [sys.executable, "-m", "rya.cli"]


def test_the_worker_command_can_be_overridden_for_an_entrypoint_wrapper():
    cmd = D.worker_command({D.WORKER_COMMAND_ENV: "tini -- /usr/local/bin/rya"})
    assert cmd == ["tini", "--", "/usr/local/bin/rya"]


def test_a_spec_and_a_worker_key_spell_the_same_key():
    """The supervisor compares what it launched against what registered itself. If
    the two spellings drift, the fleet looks permanently understaffed and it starts
    a worker every tick."""
    from rya.worker import WorkerKey

    spec = D.WorkerSpec(workspace="acme", agent="support", version_id="ver_7")
    assert spec.key == WorkerKey("acme", "support", "ver_7").concurrency_key()
    unpinned = D.WorkerSpec(workspace="acme", agent="support")
    assert unpinned.key == WorkerKey("acme", "support").concurrency_key()


def test_a_handle_is_not_a_registration():
    """A handle exists the instant a driver launches something; a registration
    exists only once that process reached the database. The gap between them is
    where a crash-on-boot lives, so the supervisor must not conflate them."""
    driver = FakeDocker()
    handle = driver.start(D.WorkerSpec(workspace="w", agent="a", version_id="v"))
    assert handle.describe()["native"]["container"] == "c1"
    assert handle.key == "w:a:v"
    assert driver.list("w:a:v") == [handle]
    assert driver.list("other:key") == []


# ---- the local driver -------------------------------------------------------

def test_the_local_driver_starts_and_stops_a_real_process(tmp_path):
    driver = D.LocalDriver(log_dir=tmp_path / "logs")
    # `--help` exits immediately, which is all this needs: the point is that the
    # driver launches THIS build's CLI and can reap it, not that a worker runs.
    spec = D.WorkerSpec(workspace="default", agent="a",
                        env={D.WORKER_COMMAND_ENV: f"{__import__('sys').executable} -m rya.cli --help"})
    handle = driver.start(spec)
    assert handle.native["pid"] > 0
    driver.stop(handle, timeout=15)
    assert driver.list() == []


def test_a_launch_that_cannot_execute_says_what_it_tried_to_run(tmp_path):
    driver = D.LocalDriver()
    spec = D.WorkerSpec(workspace="default", agent="a",
                        env={D.WORKER_COMMAND_ENV: "/nonexistent/rya-binary"})
    with pytest.raises(RyaError) as e:
        driver.start(spec)
    assert e.value.code == "E_WORKER_START_FAILED"
    assert "/nonexistent/rya-binary" in (e.value.hint or "")


def test_the_worker_argv_carries_the_agent_the_supervisor_scheduled_for():
    """A Phase 3 defect, found in Phase 4.

    The supervisor decides a key of (workspace, AGENT, version) and this argv did not
    carry the agent, so a launched worker resolved its own from the mounted
    `rya.agent.yaml`. With `--version` the version record names the agent and the
    omission was invisible; with `--env` — how the supervisor schedules an unpinned key
    — it meant deciding to serve `billing` and launching a worker serving whatever the
    file on disk happened to name.
    """
    driver = D.LocalDriver()
    argv = driver.worker_argv(D.WorkerSpec(workspace="ws_a", agent="billing",
                                           environment="prod"))
    assert "--agent" in argv
    assert argv[argv.index("--agent") + 1] == "billing"
    # And it is on the SHARED builder, so every driver emits it — the whole reason
    # `worker_argv` lives on the base class.
    for name in sorted(D.DRIVERS):
        built = D.resolve_driver(env={D.DRIVER_ENV: name, D.IMAGE_ENV: "rya:test"})
        assert "--agent" in built.worker_argv(
            D.WorkerSpec(workspace="w", agent="billing")), name


def test_an_unnamed_agent_omits_the_flag_rather_than_passing_an_empty_one():
    """`rya dev` and the single-tenant working-tree mode have no agent to name, and
    `--agent ''` would resolve the environment pointer for an agent called nothing."""
    argv = D.LocalDriver().worker_argv(D.WorkerSpec(workspace="ws_a", agent=""))
    assert "--agent" not in argv
