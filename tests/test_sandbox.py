"""The sandbox drivers and the network boundary (D23/D24, issues #14 and #15).

Neither gVisor nor a Kubernetes cluster is available in CI, so the split matters:
everything that *decides* something is pure and tested here, and the parts that shell
out to `docker`/`kubectl` are tested through their rendered command lines and
manifests. That is deliberate rather than a compromise — the alternative is a
security check that only runs on a machine with gVisor installed and is therefore
never exercised, which is how the residual in §9 risk 8 came to exist in the first
place.

The load-bearing assertions are :func:`test_a_container_on_a_host_kernel_is_refuted`,
:func:`test_a_sandbox_environment_is_built_from_nothing` and
:func:`test_the_launch_gate_needs_all_three_conditions`.
"""

import pytest

from rya import egress as E
from rya.errors import RyaError
from rya.execution import drivers as D
from rya.guard import GuardPolicy, resolve_policy


# ---- the isolation probe (§9 risk 8's residual) ----------------------------
#
# Captured from `runsc release-20260727.0` by `scripts/verify_gvisor.sh`, not typed
# from memory. The previous fixtures said `4.4.0` and were wrong — see
# `test_the_probe_recognises_the_sentry_it_was_actually_run_against`.
REAL_GVISOR_VERSION = ("Linux version 4.19.0-gvisor #1 SMP "
                       "Sun Jan 10 15:06:54 PST 2016")
REAL_GVISOR_DMESG = "[    0.000000] Starting gVisor..."


def test_gvisor_is_recognised_from_its_own_boot_log():
    probe = D.read_isolation_signals(
        f"version={REAL_GVISOR_VERSION}\n"
        f"dmesg={REAL_GVISOR_DMESG}\nsentry=fd,status,",
        driver="kubernetes", declared=D.ISOLATION_SANDBOXED)
    assert probe.verified is True
    assert probe.effective == D.ISOLATION_SANDBOXED


def test_gvisor_is_recognised_from_its_synthetic_kernel_version():
    """dmesg is often unreadable in a hardened container, which is why it is not the
    only signal. gVisor synthesises /proc/version rather than passing the host's
    through, and the synthetic string is suffixed `-gvisor`."""
    probe = D.read_isolation_signals(
        f"version={REAL_GVISOR_VERSION}\ndmesg=",
        driver="docker", declared=D.ISOLATION_SANDBOXED)
    assert probe.verified is True
    assert "synthetic" in probe.detail


def test_the_probe_recognises_the_sentry_it_was_actually_run_against():
    """Phase 6 ran gVisor for the first time, and the probe was wrong.

    The marker was the literal ``4.4.0``, taken from a captured fixture, and §9 risk
    8's whole point is that a fixture is a recording rather than a measurement.
    `release-20260727.0` reports ``4.19.0-gvisor``, so the check missed it — and
    missing it was not a missing signal, it was an INVERTED one: a version string that
    is not gVisor's is treated as positive evidence of a host kernel, so a genuine
    sandbox was actively refuted, `effective` downgraded to `shared-kernel`, and
    `require_untrusted_posture` refused.

    It refused in exactly the configuration the platform launches, too. `--cap-drop=ALL`
    is in `hardening_args` unconditionally, and that is usually what makes `dmesg` —
    the signal still working — unreadable. So the deployments that would have hit this
    are the correctly hardened ones.

    Both halves are asserted: the real string verifies, and it verifies with dmesg
    suppressed.
    """
    both = D.read_isolation_signals(
        f"version={REAL_GVISOR_VERSION}\ndmesg={REAL_GVISOR_DMESG}",
        driver="docker", declared=D.ISOLATION_SANDBOXED)
    hardened = D.read_isolation_signals(
        f"version={REAL_GVISOR_VERSION}\ndmesg=",
        driver="docker", declared=D.ISOLATION_SANDBOXED)
    assert both.verified is hardened.verified is True
    assert hardened.effective == D.ISOLATION_SANDBOXED

    # And the marker is the suffix rather than a kernel number, because gVisor has
    # moved its reported version at least once and will again.
    assert D.GVISOR_VERSION_MARKER == "-gvisor"
    for moved in ("Linux version 4.4.0-gvisor", "Linux version 6.1.0-gvisor #3 SMP"):
        assert D.read_isolation_signals(f"version={moved}\ndmesg=", driver="docker",
                                        declared=D.ISOLATION_SANDBOXED).verified is True


def test_a_container_on_a_host_kernel_is_refuted():
    """The failure §9 risk 8 names: a driver told to use runsc and silently given
    runc. The manifest looks perfect and the isolation is not there."""
    probe = D.read_isolation_signals(
        "version=Linux version 6.17.0-1017-aws #18~24.04.1-Ubuntu\ndmesg=",
        driver="kubernetes", declared=D.ISOLATION_SANDBOXED)
    assert probe.verified is False
    # And the DECLARATION is downgraded, so a fail-closed caller sees the truth.
    assert probe.effective == D.ISOLATION_SHARED_KERNEL
    assert "REFUTED" not in probe.detail  # the wording lives on the report, not here
    assert "shares the host's kernel" in probe.detail


def test_an_unreadable_probe_is_unknown_rather_than_either_answer():
    """Honest ignorance is its own state. The launch gate refuses it, which is a
    different decision from refuting it — and the operator's next step differs too."""
    probe = D.read_isolation_signals("", driver="docker",
                                     declared=D.ISOLATION_SANDBOXED)
    assert probe.verified is None
    assert probe.effective == D.ISOLATION_SANDBOXED   # not downgraded; just unproven


def test_a_docker_driver_with_no_runsc_refutes_itself_without_running_anything():
    """It does not need to probe to know: a container on the default runtime shares
    the host kernel by construction, and saying so is more useful than declining."""
    driver = D.DockerDriver(image="rya:test", env={})
    probe = driver.verify_isolation()
    assert probe.verified is False
    assert "no runsc runtime is configured" in probe.detail


def test_a_kubernetes_driver_with_the_runtime_class_opted_out_refutes_itself():
    driver = D.KubernetesDriver(image="rya:test",
                                env={D.K8S_RUNTIME_CLASS_ENV: D.RUNTIME_CLASS_NONE})
    probe = driver.verify_isolation()
    assert probe.verified is False
    assert D.K8S_RUNTIME_CLASS_ENV in probe.detail


# ---- the sandbox environment (D18 at the substrate) ------------------------

def test_a_sandbox_environment_is_built_from_nothing(monkeypatch):
    """Where the exit criterion is genuinely met rather than scrubbed after the fact.

    ``worker_env`` starts from ``os.environ`` because it launches platform code;
    ``sandbox_env`` starts from ``{}`` because it launches a container that will import
    tenant code. The credentials are not filtered out — they were never added, so there
    is no list to forget to update.
    """
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("RYA_SECRET_KEY", "seal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-POOLED")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "bucket")
    monkeypatch.setenv("SOMETHING_ENTIRELY_UNRELATED", "1")

    driver = D.DockerDriver(image="rya:test", env={})
    spec = D.WorkerSpec(workspace="ws_a", agent="support", environment="prod")
    sandbox = driver.sandbox_env(spec, token="tok")
    assert set(sandbox) == {"RYA_ENVIRONMENT", "RYA_WORKSPACE", "RYA_AGENT",
                            "RYA_BROKER", "RYA_EGRESS",
                            # D32: the two socket paths and the control token. None of
                            # them is a credential to anything outside this pair.
                            "RYA_TEMPLATE_HOST", "RYA_TEMPLATE_HOST_TOKEN",
                            "RYA_BROKER_SOCKET"}
    # The claimer's environment, by contrast, legitimately carries all of it.
    assert "RYA_DATABASE_URL" in driver.worker_env(spec)


def test_the_two_halves_of_the_pair_get_opposite_environments(monkeypatch):
    """D32, and the assertion Phase 5 could not make because there was one half.

    Phase 5's finding was that `sandbox_env` and `worker_env` describe *opposite*
    processes and both were being used for the same container. The fix is not that one
    of them was wrong — it is that there are two containers. This asserts the split in
    the direction that matters: every credential is on the claimer and none is on the
    sandbox, from one launch, with one token shared between them.
    """
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("RYA_SECRET_KEY", "seal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-POOLED")
    driver = D.DockerDriver(image="rya:test", env={})
    spec = D.WorkerSpec(workspace="ws_a", agent="support", environment="prod")

    sandbox = driver.sandbox_env(spec, token="shared")
    claimer = driver.claimer_env(spec, token="shared")

    for name in ("RYA_DATABASE_URL", "RYA_SECRET_KEY", "ANTHROPIC_API_KEY"):
        assert name not in sandbox, f"{name} reached the sandbox half"
        assert name in claimer, f"{name} is missing from the claimer half"
    # The three values that must AGREE, or the two halves never meet.
    for name in ("RYA_TEMPLATE_HOST", "RYA_TEMPLATE_HOST_TOKEN", "RYA_BROKER_SOCKET"):
        assert sandbox[name] == claimer[name] != ""
    # And mediation is forced on both: a claimer with it off would spawn templates in
    # its own container, which is the credentialed one.
    assert sandbox["RYA_BROKER"] == claimer["RYA_BROKER"] == "1"


def test_the_host_token_is_scrubbed_before_tenant_code_runs():
    """It is not a credential to anything outside the pair, and it is still scrubbed.

    A tenant that can drive the control surface can import bundles into its own
    sandbox, evict a sibling agent's warm interpreter, or stop the host — a
    cross-agent availability effect inside one tenant, which is the class D22 and D33
    close elsewhere. So it is classified `platform` rather than left in the ambiguous
    bucket its name would otherwise put it in, and the template's existing scrub
    removes it with no new code.
    """
    from rya.broker.inventory import CLASS_PLATFORM, classify, scrub_environment

    assert classify("RYA_TEMPLATE_HOST_TOKEN", "abc")[0] == CLASS_PLATFORM
    env = {"RYA_TEMPLATE_HOST_TOKEN": "abc", "RYA_TEMPLATE_HOST": "/run/rya/host.sock"}
    removed = scrub_environment(env)
    assert "RYA_TEMPLATE_HOST_TOKEN" in removed
    # The PATH survives, and that is deliberate: a template that knows where the host
    # is and cannot ask it for anything is the state we want tenant code to be in.
    assert env == {"RYA_TEMPLATE_HOST": "/run/rya/host.sock"}


def test_a_sandbox_always_gets_mediation_and_a_restricted_network():
    """Set by the driver rather than passed in: a container launched by this driver
    with either of them off would be a sandbox with an open network and a database
    credential, which is the configuration the phase exists to make unreachable."""
    driver = D.DockerDriver(image="rya:test", env={})
    sandbox = driver.sandbox_env(D.WorkerSpec(workspace="w", agent="a"))
    assert sandbox["RYA_BROKER"] == "1"
    assert sandbox["RYA_EGRESS"] == E.MODE_PROXY


# ---- what docker is actually asked to run ---------------------------------

def _fake_docker(driver, spec):
    """Run `start` with `docker` stubbed out. Returns every argv it would have run."""
    argv = []

    def fake_run(cmd, **kw):
        argv.append(cmd)
        return "deadbeefcafe\n"

    import rya.execution.drivers as mod
    original, mod._run = mod._run, fake_run
    try:
        handle = driver.start(spec)
    finally:
        mod._run = original
    return handle, argv


def test_the_docker_command_line_hardens_and_limits_and_cuts_the_network():
    driver = D.DockerDriver(image="rya:test", runtime=D.GVISOR_RUNTIME,
                            memory="256m", cpus="0.5", env={})
    spec = D.WorkerSpec(workspace="ws_a", agent="support")
    _handle, argv = _fake_docker(driver, spec)
    # volume create, sandbox, claimer — in that order, and the order is load-bearing:
    # see `DockerDriver.start`.
    assert argv[0][:3] == ["docker", "volume", "create"]
    sandbox = " ".join(argv[1])
    assert "--runtime runsc" in sandbox            # D23
    assert "--network none" in sandbox             # D24
    assert "--read-only" in sandbox
    assert "--cap-drop=ALL" in sandbox
    assert "no-new-privileges" in sandbox
    assert "--memory=256m" in sandbox and "--cpus=0.5" in sandbox
    assert f"--pids-limit={D.DEFAULT_PIDS}" in sandbox
    assert "--user 10001:10001" in sandbox
    # And the argv the sandbox container runs is the template host, not a worker.
    assert argv[1][-len(driver.host_argv(spec)):] == driver.host_argv(spec)
    assert "worker" not in driver.host_argv(spec)


def test_the_claimer_half_gets_a_network_and_no_gvisor_and_still_hardening():
    """The three differences between the pair's halves, each one the boundary.

    gVisor is for the process running tenant code; paying its syscall tax on a claimer
    that imports nothing would be cost with no security content. The network is the
    reason the other half can have none. And the hardening is on both, because
    read-only roots are not about whose code is running.
    """
    driver = D.DockerDriver(image="rya:test", runtime=D.GVISOR_RUNTIME, env={})
    spec = D.WorkerSpec(workspace="ws_a", agent="support")
    _handle, argv = _fake_docker(driver, spec)
    claimer = " ".join(argv[2])
    assert "--runtime runsc" not in claimer
    assert "--network none" not in claimer
    assert "--read-only" in claimer and "--cap-drop=ALL" in claimer
    assert f"--memory={driver.claimer_memory}" in claimer
    assert argv[2][-len(driver.worker_argv(spec)):] == driver.worker_argv(spec)


def test_both_halves_of_the_pair_mount_the_same_socket_volume():
    """The failure this prevents is the most confusing one available here: two healthy
    containers, one socket directory each, and no contact between them."""
    driver = D.DockerDriver(image="rya:test", env={})
    handle, argv = _fake_docker(driver, D.WorkerSpec(workspace="ws_a", agent="support"))
    volume = argv[0][3]
    assert handle.native["volume"] == volume
    mount = f"{volume}:{D.SOCKET_DIR}"
    assert mount in argv[1] and mount in argv[2]


def test_stopping_a_pair_removes_the_sandbox_and_the_volume_too():
    """A named volume per launch that nobody deletes is an unbounded leak on a
    long-lived host — the same class as the broker socket directories Phase 5 found by
    counting /tmp rather than by a test. Counted here instead."""
    driver = D.DockerDriver(image="rya:test", env={})
    handle, _argv = _fake_docker(driver, D.WorkerSpec(workspace="ws_a", agent="support"))
    stopped = []

    import rya.execution.drivers as mod
    original, mod._run = mod._run, lambda cmd, **kw: stopped.append(cmd) or ""
    try:
        driver.stop(handle)
    finally:
        mod._run = original
    joined = [" ".join(c) for c in stopped]
    assert any(handle.id in c and "rm" in c for c in joined)
    assert any(handle.native["sandboxName"] in c and "rm" in c for c in joined)
    assert any("volume rm" in c and handle.native["volume"] in c for c in joined)


def test_a_rebuilt_handle_can_still_tear_down_the_whole_pair():
    """`list` remembers nothing, so the sandbox's name and the volume's are derived
    from the claimer's rather than stored. Without that, `stop` on a rebuilt handle
    would kill the claimer and orphan the other two on every supervisor restart."""
    driver = D.DockerDriver(image="rya:test", env={})
    import rya.execution.drivers as mod
    original = mod._run
    mod._run = lambda cmd, **kw: "rya-ws_a-support-v7-abc\tws_a:support:v7\tws_a\tsupport\n"
    try:
        handle = driver.list()[0]
    finally:
        mod._run = original
    assert handle.native["sandboxName"] == "rya-ws_a-support-v7-abc-sandbox"
    assert handle.native["volume"] == "rya-ws_a-support-v7-abc-sock"


def test_a_docker_inventory_counts_a_pair_once():
    """Both halves carry `rya.managed`, so an unfiltered `docker ps` would report two
    workers per launch — and `supervisor.observe` would reap the difference."""
    driver = D.DockerDriver(image="rya:test", env={})
    seen = []
    import rya.execution.drivers as mod
    original = mod._run
    mod._run = lambda cmd, **kw: seen.append(cmd) or ""
    try:
        driver.list()
    finally:
        mod._run = original
    assert f"label={D.LABEL_UNIT}={D.UNIT_CLAIMER}" in seen[0]


def test_hardening_applies_at_every_isolation_level_and_does_not_change_the_claim():
    """§7: hardened Docker "is materially better than the default and is the right
    configuration for the docker driver. It still cannot contain a kernel bug, which
    is why it declares shared-kernel". Both halves, asserted."""
    plain = D.DockerDriver(image="rya:test", env={})
    assert "--cap-drop=ALL" in plain.hardening_args()
    assert plain.isolation == D.ISOLATION_SHARED_KERNEL


def test_a_container_driver_refuses_to_start_without_an_image():
    driver = D.DockerDriver(env={})
    with pytest.raises(RyaError) as e:
        driver.start(D.WorkerSpec(workspace="w", agent="a"))
    assert e.value.code == "E_WORKER_START_FAILED"
    assert "SAME build as the supervisor" in (e.value.hint or "")


def test_a_docker_inventory_is_rebuilt_from_labels_not_remembered():
    """The gap `LocalDriver.list` documents it cannot close: a supervisor that
    restarted has no memory of what it launched, and a substrate query does not need
    one."""
    driver = D.DockerDriver(image="rya:test", env={})
    import rya.execution.drivers as mod
    original = mod._run
    mod._run = lambda cmd, **kw: (
        "rya-ws_a-support-v7-abc\tws_a:support:v7\tws_a\tsupport\n"
        "rya-ws_b-billing-local-def\tws_b:billing:local\tws_b\tbilling\n")
    try:
        handles = driver.list()
        assert {h.key for h in handles} == {"ws_a:support:v7", "ws_b:billing:local"}
        assert all(h.native["rebuiltFromLabels"] for h in handles)
        assert driver.list(key="ws_a:support:v7")[0].spec.version_id == "v7"
        # An unpinned key round-trips to version_id=None, not to the string "local".
        assert driver.list(key="ws_b:billing:local")[0].spec.version_id is None
    finally:
        mod._run = original


# ---- the kubernetes manifest ----------------------------------------------

def test_the_pod_asks_for_the_runtime_class_and_mounts_no_service_account_token():
    driver = D.KubernetesDriver(image="rya:test", env={})
    pod = driver.render(D.WorkerSpec(workspace="ws_a", agent="support",
                                     environment="prod"))
    spec = pod["spec"]
    assert spec["runtimeClassName"] == "gvisor"
    # A mounted token is a credential for the cluster's API — a platform credential in
    # a tenant process by any reading.
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    sandbox = next(c for c in spec["containers"] if c["name"] == "sandbox")
    assert sandbox["securityContext"]["readOnlyRootFilesystem"] is True
    assert sandbox["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # The TENANT's limits are on the sandbox half, not the claimer's: one cgroup around
    # every warm interpreter this tenant owns (§7.1's "per-agent resource limits" trade).
    assert sandbox["resources"]["limits"]["memory"] == D.DEFAULT_MEMORY
    names = {e["name"] for e in sandbox["env"]}
    assert names == {"RYA_ENVIRONMENT", "RYA_WORKSPACE", "RYA_AGENT",
                     "RYA_BROKER", "RYA_EGRESS", "RYA_TEMPLATE_HOST",
                     "RYA_TEMPLATE_HOST_TOKEN", "RYA_BROKER_SOCKET"}


def test_the_pod_is_the_pair_and_the_two_containers_share_one_socket_volume():
    """D32 on the substrate that makes it easiest: one pod, one lifecycle, one
    `emptyDir` for the two sockets, and the existing NetworkPolicy over both.

    The `emptyDir` is the one part of this arrangement Phase 5 had right before the
    rest existed — it used to carry a comment claiming a sidecar `render` did not
    produce. This asserts the sidecar is there now.
    """
    driver = D.KubernetesDriver(image="rya:test", env={})
    pod = driver.render(D.WorkerSpec(workspace="ws_a", agent="support"), token="tok")
    containers = {c["name"]: c for c in pod["spec"]["containers"]}
    assert set(containers) == {"claimer", "sandbox"}
    # The claimer runs the worker; the sandbox runs the host and never claims.
    assert "worker" in containers["claimer"]["command"]
    assert "template-host" in containers["sandbox"]["command"]
    assert "worker" not in containers["sandbox"]["command"]
    # Both mount the socket volume, and it is in memory so neither socket touches disk.
    for container in containers.values():
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts["broker"] == D.SOCKET_DIR
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["broker"]["emptyDir"]["medium"] == "Memory"
    # No `shareProcessNamespace`: the claimer's memory must not be readable from the
    # container that runs tenant code, even though the two are scheduled together.
    assert "shareProcessNamespace" not in pod["spec"]


def test_the_pods_two_halves_get_opposite_environments(monkeypatch):
    """The k8s mirror of the docker assertion, because `render` builds its own env
    lists and a driver that got this right in one substrate and wrong in the other is
    exactly the three-behaviours outcome D26 exists to avoid."""
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    driver = D.KubernetesDriver(image="rya:test", env={})
    pod = driver.render(D.WorkerSpec(workspace="ws_a", agent="support"), token="tok")
    containers = {c["name"]: {e["name"]: e["value"] for e in c["env"]}
                  for c in pod["spec"]["containers"]}
    assert "RYA_DATABASE_URL" not in containers["sandbox"]
    assert containers["claimer"]["RYA_DATABASE_URL"] == "postgres://u:p@h/d"
    assert (containers["claimer"]["RYA_TEMPLATE_HOST_TOKEN"]
            == containers["sandbox"]["RYA_TEMPLATE_HOST_TOKEN"] == "tok")


def test_the_pod_omits_the_runtime_class_when_it_is_opted_out():
    driver = D.KubernetesDriver(image="rya:test",
                                env={D.K8S_RUNTIME_CLASS_ENV: D.RUNTIME_CLASS_NONE})
    pod = driver.render(D.WorkerSpec(workspace="w", agent="a"))
    assert "runtimeClassName" not in pod["spec"]


def test_the_network_policy_denies_egress_outright():
    """Explicitly an empty list rather than an omitted key: both mean deny-all and
    only one of them reads like a decision."""
    driver = D.KubernetesDriver(image="rya:test", env={})
    policy = driver.network_policy(D.WorkerSpec(workspace="w", agent="support"))
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["spec"]["egress"] == []
    assert policy["spec"]["podSelector"]["matchLabels"] == {"rya-managed": "1"}


# ---- the network posture (D24) --------------------------------------------

def _rule(url, action="allow", kind="prefix"):
    return {"action": action, "kind": kind, "pattern": url}


def _policy(allow):
    return resolve_policy({"default": "deny", "ssrf": False,
                           "rules": [_rule(u) for u in allow]})


def test_the_posture_takes_hosts_from_allow_rules_only():
    """Deny rules are not translated: a network allowlist is already deny-by-default,
    so a deny rule is either redundant here or finer-grained than this layer can
    express — and turning "deny this path on an allowed host" into a network rule
    would either lose the path or block the host."""
    gp = resolve_policy({"default": "deny", "ssrf": False, "rules": [
        _rule("https://api.stripe.com/v1/"),
        _rule("https://api.stripe.com/v1/refunds", action="deny")]})
    posture = E.posture_from_policy(gp)
    assert posture.hosts == ("api.stripe.com",)
    assert posture.etag == gp.etag


def test_the_posture_permits_on_host_and_port_because_that_is_all_it_can_see():
    posture = E.NetworkPosture(mode=E.MODE_PROXY, hosts=("api.stripe.com",),
                               ports=(443,))
    assert posture.permits("https://api.stripe.com/v1/charges")[0] is True
    assert posture.permits("https://evil.example.com/")[0] is False
    assert posture.permits("http://api.stripe.com/v1/")[0] is False   # port 80


def test_the_local_arm_says_it_enforces_nothing():
    """Honest rather than degraded: `local` declares isolation "none" and the
    untrusted posture already refuses it."""
    posture = E.NetworkPosture(mode=E.MODE_NONE)
    assert posture.enforced is False
    allowed, reason = posture.permits("https://anywhere.example.com/")
    assert allowed is True and "no network restriction" in reason


def test_resolve_egress_defaults_to_claiming_nothing():
    service = E.resolve_egress(env={})
    assert service.posture.mode == E.MODE_NONE
    assert service.posture.enforced is False


# ---- the two verdicts, and the divergence --------------------------------

def _service(allow, hosts, etag=None):
    gp = _policy(allow)
    posture = E.NetworkPosture(mode=E.MODE_PROXY, hosts=tuple(hosts), ports=(443,),
                               etag=etag if etag is not None else gp.etag)
    return E.EgressService(posture=posture, policy_source=gp)


def test_both_layers_agreeing_to_allow_lets_the_request_through():
    service = _service(["https://api.stripe.com/"], ["api.stripe.com"])
    verdict = service.check("https://api.stripe.com/v1/charges", "POST")
    assert verdict["allowed"] is True and verdict["diverged"] is False


def test_a_non_allowlisted_host_is_refused_by_both_and_named_as_such():
    service = _service(["https://api.stripe.com/"], ["api.stripe.com"])
    with pytest.raises(RyaError) as e:
        service.fetch("https://evil.example.com/exfiltrate", method="POST")
    assert e.value.code == "E_EGRESS_DENIED"
    assert "both the guard policy and the sandbox's network" in e.value.message


def test_a_stale_sandbox_snapshot_is_a_recorded_divergence_and_fails_closed():
    """The ordinary consequence of a policy change, and the most likely way the two
    layers disagree in production: the network posture is a snapshot taken when the
    sandbox started, and the policy is live."""
    # The policy now allows a host the sandbox's network snapshot does not know about.
    service = _service(["https://api.newvendor.com/"], ["api.stripe.com"],
                       etag="etag-from-before-the-policy-change")
    verdict = service.check("https://api.newvendor.com/v1/x", "POST")
    assert verdict["guard"] == "allow" and verdict["network"] == "block"
    assert verdict["diverged"] is True
    assert verdict["allowed"] is False          # fail closed
    recorded = service.divergences()
    assert len(recorded) == 1
    assert "predates the current guard policy" in recorded[0]["reason"]


def test_a_revoked_rule_the_network_still_permits_also_fails_closed():
    """The other direction, and the reason "trust the snapshot" is not an answer
    either: the operator has already revoked this and the network has not caught up."""
    service = _service([], ["api.stripe.com"], etag="stale")
    verdict = service.check("https://api.stripe.com/v1/charges", "POST")
    assert verdict["guard"] == "block" and verdict["network"] == "allow"
    assert verdict["allowed"] is False
    assert verdict["diverged"] is True


def test_an_unconfigured_guard_is_not_treated_as_agreement():
    """`guard.enforced` is False when no policy exists anywhere — that absence must
    not read as "the operator allowed this"."""
    posture = E.NetworkPosture(mode=E.MODE_PROXY, hosts=("api.stripe.com",),
                               ports=(443,))
    service = E.EgressService(posture=posture,
                              policy_source=GuardPolicy(policy={}, etag="", version="none",
                                                        source="none"))
    verdict = service.check("https://api.stripe.com/v1/charges", "POST")
    assert verdict["guard"] == "unset"
    assert verdict["diverged"] is False       # nothing to diverge from
    assert verdict["allowed"] is True         # the network is the only verdict here


def test_divergences_are_bounded_and_keep_the_most_recent():
    """A divergence storm is a policy change that has not propagated, and the recent
    entries are the ones describing the current state."""
    service = _service(["https://a.example.com/"], [], etag="stale")
    service.max_divergences = 5
    for i in range(20):
        service.check(f"https://a.example.com/{i}", "GET")
    recorded = service.divergences()
    assert len(recorded) == 5
    assert recorded[-1]["url"].endswith("/19")


def test_reconcile_rolls_divergences_up_across_the_fleet_with_an_action():
    """"Is any sandbox enforcing a stale allowlist" is a question about the fleet, and
    the answer an operator wants is a count and one example, not a log."""
    stale = _service(["https://a.example.com/"], [], etag="old-etag")
    stale.check("https://a.example.com/x", "GET")
    fresh = _service(["https://b.example.com/"], ["b.example.com"])
    fresh.check("https://b.example.com/x", "GET")
    out = E.reconcile([stale, fresh])
    assert out["total"] == 1 and out["stale"] == 1
    assert "recycle the sandboxes" in out["action"]


def test_a_clean_fleet_reports_no_action_to_take():
    fresh = _service(["https://b.example.com/"], ["b.example.com"])
    fresh.check("https://b.example.com/x", "GET")
    out = E.reconcile([fresh])
    assert out["total"] == 0 and out["example"] is None


def test_the_posture_resolution_drops_private_addresses():
    """A host that resolves into the private range is an SSRF target, and a network
    rule built from it would punch a hole into the cluster."""
    posture = E.NetworkPosture(mode=E.MODE_PROXY, hosts=("localhost",))
    assert posture.resolve_hosts()["localhost"] == []


def test_an_unresolvable_host_narrows_the_rule_rather_than_failing_the_sandbox():
    posture = E.NetworkPosture(mode=E.MODE_PROXY,
                               hosts=("nx.invalid.rya-test.example",))
    assert posture.resolve_hosts()["nx.invalid.rya-test.example"] == []


def test_an_unknown_egress_mode_is_refused_by_name():
    with pytest.raises(RyaError) as e:
        E.resolve_egress(env={E.MODE_ENV: "firewall"})
    assert e.value.code == "E_EGRESS_UNAVAILABLE"
    assert E.MODE_NONE in (e.value.hint or "")


# ---- the launch gate ------------------------------------------------------

def _gate_env(**over):
    base = {D.UNTRUSTED_ENV: "1", "RYA_BROKER": "1", E.MODE_ENV: E.MODE_PROXY}
    base.update(over)
    return base


def _sandboxed_k8s():
    """A `kubernetes` driver whose isolation probe confirms gVisor."""
    driver = D.KubernetesDriver(image="rya:test", env={})
    driver._probe = D.IsolationProbe(driver="kubernetes",
                                     declared=D.ISOLATION_SANDBOXED, verified=True)
    driver.verify_isolation = lambda: driver._probe        # type: ignore[method-assign]
    return driver


def test_the_launch_gate_needs_every_condition():
    """MULTITENANT_PLAN §6: "half a security boundary is not a security boundary".
    Separate warnings would be separate things to miss.

    Rewritten twice, and the pair of rewrites is the record. Phase 4 asserted that
    D18+D23+D24 on a `kubernetes` driver PASSED — false, because the pod's one
    container was configured by `sandbox_env` and had no database credential, so it
    would have started and claimed nothing. Phase 5 turned that into a refusal it could
    not yet lift. Phase 6 built the template host and the pair, so the assertion below
    is on a driver that declares `sandbox` — the arrangement that is still broken, and
    the one a third-party driver is most likely to write by accident.
    """
    driver = _sandboxed_k8s()
    driver.launched_unit = D.UNIT_SANDBOX
    # Three of four in force, isolation confirmed — and still refused, by name.
    with pytest.raises(RyaError) as e:
        D.require_untrusted_posture(driver, env=_gate_env())
    assert e.value.code == "E_ISOLATION_INSUFFICIENT"
    assert "broker topology (D32)" in e.value.message
    # The refusal names the fix, not just the fault: `rya template-host` is what the
    # operator has to run in the other container.
    assert "rya template-host" in e.value.message

    driver = _sandboxed_k8s()
    for missing, key, value in (("credential mediation (D18)", "RYA_BROKER", ""),
                                ("network egress (D24)", E.MODE_ENV, E.MODE_NONE)):
        with pytest.raises(RyaError) as e:
            D.require_untrusted_posture(driver, env=_gate_env(**{key: value}))
        assert e.value.code == "E_ISOLATION_INSUFFICIENT"
        assert missing in e.value.message


def test_the_gate_passes_when_all_four_conditions_hold():
    """The positive case, and as of Phase 6 it is a SHIPPED driver rather than a stub.

    This is the assertion the whole untrusted posture reduces to: a `kubernetes` driver
    with a gVisor RuntimeClass, mediation on, egress restricted, rendering the D32 pair
    — and the gate returns the driver rather than raising. Phase 5's version of this
    test had to fake `launched_unit` because no driver launched the pair; that the fake
    is gone is the delivery.
    """
    driver = _sandboxed_k8s()
    assert driver.launched_unit == D.UNIT_PAIR
    assert D.require_untrusted_posture(driver, env=_gate_env()) is driver


def test_the_gate_names_every_unmet_condition_at_once():
    """Being told about one missing piece at a time, four deploys in a row, is how a
    launch checklist gets abandoned."""
    driver = D.LocalDriver()
    report = D.check_untrusted_posture(driver, env={D.UNTRUSTED_ENV: "1"})
    # Three, not four: `local` launches the claimer, so D32 is satisfied there — in its
    # weak form, with the credential boundary at the process boundary. What `local`
    # fails is isolation, which is the reason it cannot serve untrusted tenants and is
    # not the reason a container driver cannot.
    assert len(report.unmet) == 3
    assert report.topology_ok is True
    with pytest.raises(RyaError) as e:
        D.require_untrusted_posture(driver, env={D.UNTRUSTED_ENV: "1"}, verify=False)
    for expected in ("isolation (D23)", "credential mediation (D18)",
                     "network egress (D24)"):
        assert expected in e.value.message


def test_an_unverifiable_sandbox_is_refused_not_assumed():
    """Matching `isolation_rank`'s treatment of an unknown level: the safe direction
    is the default one. An operator who cannot probe their cluster does not have a
    verified sandbox, whatever the manifest says."""
    driver = D.KubernetesDriver(image="rya:test", env={})
    driver.verify_isolation = lambda: D.IsolationProbe(  # type: ignore[method-assign]
        driver="kubernetes", declared=D.ISOLATION_SANDBOXED, verified=None,
        detail="the probe pod could not be run: no cluster")
    report = D.check_untrusted_posture(driver, env=_gate_env(), verify=True)
    assert report.isolation_ok is False
    assert "inconclusive" in report.isolation_detail


def test_a_refuted_sandbox_is_reported_differently_from_an_unverifiable_one():
    """The two need different operator actions, so they must not collapse into one
    message."""
    driver = D.KubernetesDriver(image="rya:test", env={})
    driver.verify_isolation = lambda: D.IsolationProbe(  # type: ignore[method-assign]
        driver="kubernetes", declared=D.ISOLATION_SANDBOXED, verified=False,
        detail="/proc/version reports a host kernel")
    report = D.check_untrusted_posture(driver, env=_gate_env(), verify=True)
    assert report.isolation_ok is False
    assert "REFUTED" in report.isolation_detail


def test_the_trusted_posture_is_admitted_with_none_of_the_three():
    """Every self-host is this, and quietly raising its requirements would refuse to
    start for every existing deployment."""
    driver = D.LocalDriver()
    assert D.require_untrusted_posture(driver, env={}) is driver
    report = D.check_untrusted_posture(driver, env={})
    assert report.untrusted is False and report.unmet     # unmet, but not required


def test_the_gate_does_not_probe_unless_asked():
    """Probing costs a container start, and the supervisor evaluates the posture on a
    path where that would be paid per tick."""
    driver = D.DockerDriver(image="rya:test", runtime=D.GVISOR_RUNTIME, env={})
    calls = []
    driver.verify_isolation = lambda: (  # type: ignore[method-assign]
        calls.append(1) or D.IsolationProbe(driver="docker",
                                            declared=D.ISOLATION_SANDBOXED,
                                            verified=True))
    D.check_untrusted_posture(driver, env=_gate_env(), verify=False)
    assert calls == []
    D.check_untrusted_posture(driver, env=_gate_env(), verify=True)
    assert calls == [1]


# ---- ctx.egress: the replacement for the leaf-tool's raw request ------------

def _ctx(tmp_path, policy, broker=None):
    """A RuntimeContext with nothing but what ctx.egress needs."""
    from types import SimpleNamespace

    from rya.sdk.context import RuntimeContext
    from rya.store import Store

    store = Store(tmp_path / ".rya")
    store.ensure()
    store.policy_set("guard", policy)
    manifest = SimpleNamespace(name="a", version="0.1.0", tools=[],
                               model=SimpleNamespace(provider="mock", default="mock-llm",
                                                     routes={}, fallback=None),
                               secrets={})
    run = {"id": "run_1", "agent": "a", "journal": {}, "trace": []}
    return RuntimeContext(store=store, manifest=manifest, run=run, tools={}, models={},
                          project_root=tmp_path, broker=broker)


def test_ctx_egress_is_journaled_so_a_replay_does_not_reissue_it(tmp_path):
    """An outbound call is a side effect, which is exactly why a leaf could never
    have this: a replay after an approval pause must return the memoized response."""
    calls = []

    class FakeBroker:
        def egress_fetch(self, url, **kw):
            calls.append(url)
            return {"status": 200, "headers": {}, "body": "ok"}

    ctx = _ctx(tmp_path, {"default": "allow", "ssrf": False, "rules": []},
               broker=FakeBroker())
    first = ctx.egress.fetch("https://api.stripe.com/v1/charges", method="POST")
    assert first["status"] == 200 and calls == ["https://api.stripe.com/v1/charges"]

    # Replay: same ordinal, same content key, so the memoized result comes back and
    # the upstream is NOT called a second time.
    ctx._seq = 0
    again = ctx.egress.fetch("https://api.stripe.com/v1/charges", method="POST")
    assert again == first
    assert calls == ["https://api.stripe.com/v1/charges"]


def test_ctx_egress_goes_through_the_broker_when_mediated(tmp_path):
    """So the credential-free process does not have to make the call itself."""
    seen = {}

    class FakeBroker:
        def egress_fetch(self, url, **kw):
            seen.update({"url": url, **kw})
            return {"status": 204, "headers": {}, "body": ""}

    ctx = _ctx(tmp_path, {"default": "allow", "ssrf": False, "rules": []},
               broker=FakeBroker())
    ctx.egress.fetch("https://api.example.com/x", method="PUT", body={"a": 1})
    assert seen["url"] == "https://api.example.com/x"
    assert seen["method"] == "PUT" and seen["body"] == {"a": 1}


def test_ctx_egress_applies_the_guard_policy_in_the_trusted_posture(tmp_path):
    """A handler written against ctx.egress behaves the same on a laptop, including
    being refused — otherwise the allowlist would only be real in production."""
    ctx = _ctx(tmp_path, {"default": "deny", "ssrf": False,
                          "rules": [_rule("https://allowed.example.com/")]})
    with pytest.raises(RyaError) as e:
        ctx.egress.fetch("https://blocked.example.com/x")
    assert e.value.code in ("E_EGRESS_BLOCKED", "E_EGRESS_DENIED")


# ---- the gate has to be REACHED, not just exist ---------------------------

def test_start_worker_itself_refuses_the_untrusted_posture(tmp_path, monkeypatch):
    """The call site Phase 3 was missing, and the e2e is what found it.

    `require_isolation_for_tenancy` was wired into `rya supervisor` only, so a
    hand-started `rya worker` walked straight past it: RYA_UNTRUSTED_TENANTS=1 on the
    local driver started happily and served untrusted tenants on a shared kernel with a
    database credential in the process. A gate the supervisor honours and the worker
    ignores is a convention with a nice error message — §9 risk 8's failure exactly.

    Asserted at `start_worker` rather than in the CLI because that is what EVERY route
    to a running worker goes through: the CLI, the supervisor's launched process, and a
    test. A check in one caller is a check one caller can be added around.
    """
    from rya.cli import scaffold
    from rya.store import Store
    from rya.worker import start_worker

    root = tmp_path / "proj"
    scaffold.write_project(root, "gated", template="minimal")
    store = Store(root / ".rya")
    store.ensure()
    monkeypatch.setenv(D.UNTRUSTED_ENV, "1")

    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, agent_name="gated")
    assert e.value.code == "E_ISOLATION_INSUFFICIENT"
    assert "isolation (D23)" in e.value.message


def test_start_worker_is_unaffected_in_the_trusted_posture(tmp_path, monkeypatch):
    """Every self-host is this, so the gate must cost it nothing."""
    from rya.cli import scaffold
    from rya.store import Store
    from rya.worker import start_worker

    root = tmp_path / "proj"
    scaffold.write_project(root, "ungated", template="minimal")
    store = Store(root / ".rya")
    store.ensure()
    monkeypatch.delenv(D.UNTRUSTED_ENV, raising=False)

    w = start_worker(project_root=root, store=store, agent_name="ungated")
    assert w.key.agent == "ungated"
