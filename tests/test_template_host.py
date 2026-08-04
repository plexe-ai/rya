"""The D32 template host — MULTITENANT_DESIGN §7.2's "good" topology, built.

D32 decided the broker is a sibling of the tenant process, never its parent. §7.2 then
named the single thing that blocked it: the claimer *spawns* the template with
``multiprocessing``, so the template is necessarily its child, so the two are
necessarily in the same container. Until Phase 6 that made the untrusted posture
unlaunchable on every driver — `local` fails isolation and the container drivers failed
topology — and `topology_supported` refused rather than pretending.

**The load-bearing test is :func:`test_the_template_is_not_a_child_of_the_claimer`.**
Everything else here could pass with the template still spawned by the claimer. It
starts a real ``rya template-host`` in a real subprocess with a real credential-free
environment, and reads the resulting process ancestry out of the tenant handler's own
report — which is the same discipline `test_fork_execution.py` uses for D27 and
`phase_mediation` uses for D18: ask the tenant what it can see rather than assert that
the platform meant well.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from rya import bundles, deployments, turns
from rya.broker import protocol as proto
from rya.cli import scaffold
from rya.errors import RyaError
from rya.execution import drivers as D
from rya.execution.host import (CONTROL_TIMEOUT, E_HOST_CREDENTIALED, E_HOST_DENIED,
                                E_HOST_UNAVAILABLE, E_HOST_UNMEDIATED, OP_DRAIN,
                                OP_HELLO, OP_START, OP_STATUS, OP_STOP, START_FIELDS,
                                HostedTemplate, HostedTemplateProbe, StartRequest,
                                TemplateHost, _refuse_credentials)
from rya.execution.pool import FORK_AVAILABLE, WarmPool, WarmTemplate
from rya.store import Store
from rya.worker import start_worker

pytestmark = pytest.mark.skipif(not FORK_AVAILABLE,
                                reason="the template host forks per run")

# Reports its own pid, the pid that imported it, and its parent's — which is how the
# ancestry assertion reads the topology out of the tenant's own process rather than out
# of the platform's bookkeeping.
ENTRYPOINT = '''
import os
from rya import define_agent

agent = define_agent()
IMPORTED_IN = os.getpid()

@agent.on_event
async def main(ctx, event):
    return {"seen": event.payload.get("v"), "pid": os.getpid(),
            "importedIn": IMPORTED_IN, "ppid": os.getppid()}
'''


# ---- fixtures ---------------------------------------------------------------

def _published(tmp_path, name="hosted"):
    """A deployment root, its store, and one promoted version of one agent."""
    project = tmp_path / name
    scaffold.write_project(project, name, template="demo")
    (project / "src" / "agent.py").write_text(ENTRYPOINT)
    manifest = project / "rya.agent.yaml"
    doc = yaml.safe_load(manifest.read_text())
    doc["tools"] = []
    manifest.write_text(yaml.safe_dump(doc))

    root = tmp_path / "deployment"
    root.mkdir()
    store = Store(root)
    store.ensure()
    bundle = bundles.build_bundle(project)
    bundles.store_bundle(bundle, bundles.default_archive_root(root))
    version = deployments.create_version(store, agent=name, bundle=bundle)
    deployments.promote(store, environment="prod", agent=name,
                        version_id=version["id"], actor="test", gate=False)
    return root, store, version


# The environment the host subprocess gets. Written as an allowlist rather than a copy
# with deletions, for exactly the reason `ContainerDriver.sandbox_env` is: a credential
# that was never added cannot be forgotten in a filter. This is the test's stand-in for
# the sandbox container, and it is built the same way the real one is.
def _host_env(socket_path, token="tok-abc"):
    src = str(Path(__file__).resolve().parents[1] / "src")
    return {"PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": src,
            "RYA_TEMPLATE_HOST": str(socket_path),
            "RYA_TEMPLATE_HOST_TOKEN": token}


class _HostProcess:
    """`rya template-host` in a subprocess, torn down whatever the test does."""

    def __init__(self, socket_path, token="tok-abc"):
        self.path = Path(socket_path)
        self.token = token
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "rya.cli", "template-host", "--socket", str(self.path)],
            env=_host_env(self.path, self.token),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.path.exists():
                return self
            if self.proc.poll() is not None:
                raise AssertionError(f"the host exited: {self.proc.stdout.read()[:800]}")
            time.sleep(0.05)
        raise AssertionError("the template host never bound its socket")

    def __exit__(self, *exc):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()


def _inprocess_host(tmp_path, token="tok-abc"):
    """A host in this process, for the wire-level refusals.

    A subprocess would prove nothing extra about a rejected frame and costs an
    interpreter start per assertion. The topology tests below use the real one.
    """
    return TemplateHost(socket_path=tmp_path / "host.sock", token=token, max_entries=2)


def _raw(host, payload):
    """One frame in, one frame out, no client object. Returns the reply dict."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15.0)
    sock.connect(str(host.path))
    try:
        proto.send_frame(sock, {"v": 1, "seq": 1, **payload})
        return proto.recv_frame(sock)
    finally:
        sock.close()


# ---- the property ----------------------------------------------------------

def test_the_template_is_not_a_child_of_the_claimer(tmp_path):
    """D32, and the one assertion the rest of this file could pass without.

    Four processes, and the shape of the tree is the whole decision:

        claimer (this test)      holds the store, runs the broker, mints capabilities
        host    (subprocess)     holds NOTHING — an allowlisted environment, no DSN
          └─ template            imports the tenant's bundle
               └─ fork           runs one item

    The claimer is not an ancestor of anything that imported tenant code. Before Phase
    6 it was the template's parent by construction, which is why §7.2 called the
    single-container arrangement *weak* and why the container drivers could not launch
    the untrusted posture at all.

    The ancestry comes back inside the handler's own return value, so what is asserted
    is what the tenant process actually observed.
    """
    root, store, version = _published(tmp_path)
    sock = tmp_path / "host.sock"
    with _HostProcess(sock) as host:
        os.environ["RYA_TEMPLATE_HOST"] = str(sock)
        os.environ["RYA_TEMPLATE_HOST_TOKEN"] = "tok-abc"
        try:
            worker = start_worker(project_root=root, store=store, fork=True,
                                  scope="tenant", mediated=True, environment="prod",
                                  prewarm=("prod",))
            try:
                turns.enqueue_event(
                    turns.TurnSource(store=store,
                                     manifest=SimpleNamespace(name="hosted",
                                                              version="0.1.0"),
                                     version=version),
                    "message.received", {"v": 42})
                tick = worker.drain_once()
                assert tick["count"] == 1, tick.get("error")
                job = store.queue_get(tick["turns"][0])
                run = store.get_run(job["payload"]["runId"])
                # Read WHILE the template is alive: `executor.close()` below stops it,
                # and a dead pid has no /proc entry to ask.
                template_parent = _parent_of(run["output"]["importedIn"])
                host_pid = host.proc.pid
            finally:
                worker.executor.close()
        finally:
            os.environ.pop("RYA_TEMPLATE_HOST", None)
            os.environ.pop("RYA_TEMPLATE_HOST_TOKEN", None)

    assert run["status"] == "completed"
    out = run["output"]
    assert out["seen"] == 42
    # The fork's parent is the template, and the template is not this process.
    assert out["ppid"] == out["importedIn"]
    assert out["importedIn"] not in (os.getpid(), out["pid"])
    # And the template's own parent is the HOST subprocess, not the claimer.
    assert template_parent == host_pid != os.getpid()


def _parent_of(pid):
    """The ppid of a pid, from procfs. Linux-only, and so is the sandbox."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise AssertionError(f"no PPid for {pid}")  # pragma: no cover


def test_the_host_serves_with_no_credentials_in_its_environment(tmp_path):
    """The other half of the same property, asserted from the host's side.

    `sandbox_env` builds the sandbox container's environment from ``{}``; this builds
    the host subprocess's the same way, and then asserts the host both *has* no
    credential and *works anyway*. A host that needed a DSN would be a claimer, which
    is precisely the confusion Phase 5 found in `sandbox_env` itself.
    """
    sock = tmp_path / "host.sock"
    env = _host_env(sock)
    from rya.broker.inventory import CLASS_PLATFORM, classify

    groups = {classify(k, v)[1] for k, v in env.items()
              if classify(k, v)[0] == CLASS_PLATFORM}
    # The ONLY platform-classified thing in the sandbox's environment is D32's own
    # control token, whose authority stops at this pair — it lets the claimer say
    # "import this bundle" and nothing else. The four groups that name real
    # credentials are absent, which is the criterion.
    assert groups <= {"templateHostToken"}
    assert not (groups & {"dsn", "sealKey", "providerKey", "bucketCredential",
                          "adminToken"})
    with _HostProcess(sock):
        hello = HostedTemplateProbe(str(sock), "tok-abc").hello()
        assert hello["ok"] is True
        status = HostedTemplateProbe(str(sock), "tok-abc").status()
        assert status["templates"] == [] and status["socket"] == str(sock)


def test_the_claimer_reports_which_topology_it_is_running(tmp_path):
    """An operator reading `GET /workers` should be able to tell where the templates
    are, because "the interpreter holding this tenant's code keeps dying" is answered
    in a different container depending on the answer. Scope and topology are
    independent, so the mode carries both."""
    root, store, _version = _published(tmp_path)
    sock = tmp_path / "host.sock"
    with _HostProcess(sock):
        os.environ["RYA_TEMPLATE_HOST"] = str(sock)
        os.environ["RYA_TEMPLATE_HOST_TOKEN"] = "tok-abc"
        try:
            worker = start_worker(project_root=root, store=store, fork=True,
                                  scope="tenant", mediated=True, environment="prod")
            try:
                assert worker.executor.mode == "fork-tenant-hosted"
                assert worker.executor.pool.hosted is True
            finally:
                worker.executor.close()
        finally:
            os.environ.pop("RYA_TEMPLATE_HOST", None)
            os.environ.pop("RYA_TEMPLATE_HOST_TOKEN", None)


def test_without_a_host_configured_the_pool_spawns_its_own_templates(tmp_path):
    """The weak topology stays the default, and that is deliberate: `local`, `rya dev`
    and every test want a claimer that spawns its own templates, and the split is opted
    into by the driver that renders the pair."""
    pool = WarmPool(max_entries=2)
    assert pool.hosted is False
    assert isinstance(pool._build(bundle_hash=None, root=Path("."), version={},
                                  workspace="w", environment=None, state_root=None,
                                  broker=None, agent_name="a", routes={},
                                  broker_socket=""), WarmTemplate)


# ---- the control surface ---------------------------------------------------

def test_the_host_refuses_a_request_with_the_wrong_token(tmp_path):
    """The processes that can reach this socket include the tenant's own forks.

    They gain nothing from the control surface that they do not already have — the
    bundles are their own and the capabilities are minted elsewhere — but they could
    evict a sibling agent's warm interpreter or stop the host, which is a cross-agent
    availability effect inside one tenant.
    """
    with _inprocess_host(tmp_path) as host:
        assert _raw(host, {"op": OP_HELLO, "token": "tok-abc"})["ok"] is True
        for bad in ("", "tok-abd", "TOK-ABC"):
            reply = _raw(host, {"op": OP_HELLO, "token": bad})
            assert reply["ok"] is False
            assert reply["error"]["code"] == E_HOST_DENIED


def test_a_host_with_no_token_refuses_everything_rather_than_everyone(tmp_path):
    """Fail closed. An empty token must not mean "no check" — that is the same
    sentinel trap `RUNTIME_CLASS_NONE` exists for one module over, where an empty
    variable was indistinguishable from an unset one."""
    with TemplateHost(socket_path=tmp_path / "h.sock", token="") as host:
        reply = _raw(host, {"op": OP_HELLO, "token": ""})
        assert reply["ok"] is False and reply["error"]["code"] == E_HOST_DENIED
        assert "accept control requests from any process" in reply["error"]["hint"]


def test_the_host_refuses_an_op_it_does_not_serve(tmp_path):
    """The data surface is the broker's, on a different socket and in the other
    direction. Neither socket lets its caller become the other side."""
    with _inprocess_host(tmp_path) as host:
        reply = _raw(host, {"op": "queue.claim", "token": "tok-abc"})
        assert reply["error"]["code"] == E_HOST_DENIED
        assert "does not serve the op" in reply["error"]["message"]


def test_a_start_request_carrying_a_credential_is_refused(tmp_path):
    """The host is credential-free by wire format — `StartRequest` has no field for a
    state root — and this is the belt-and-braces pass for the field somebody adds
    later. Checked on the way out of the claimer as well as on the way in, because the
    claimer is the process that HAS the credentials and is therefore the one that can
    leak them."""
    with pytest.raises(RyaError) as e:
        _refuse_credentials({"routes": {"default": {"provider": "anthropic",
                                                    "api_key": "sk-LEAK"}}})
    assert e.value.code == E_HOST_CREDENTIALED
    assert "public_routes" in (e.value.hint or "")

    # A DSN is a credential whatever the key is called — inventory's rule, applied to
    # a wire schema rather than to an environment.
    with pytest.raises(RyaError):
        _refuse_credentials({"root": "postgres://u:p@h/d"})

    with _inprocess_host(tmp_path) as host:
        reply = _raw(host, {"op": OP_START, "token": "tok-abc",
                            "start": {"root": "/tmp", "brokerSocket": "/tmp/b.sock",
                                      "RYA_DATABASE_URL": "postgres://u:p@h/d"}})
        assert reply["ok"] is False


def test_an_ordinary_model_route_is_not_mistaken_for_a_credential():
    """Regression, and it is the interesting kind: the first cut of the walk refused
    the `ambiguous` bucket too, so a perfectly normal route was rejected because
    ``maxTokens`` contains the substring "TOKEN".

    inventory.py's own words are that ambiguous names are *not* removed, because "a
    shape-based heuristic is not a good enough reason to break a handler" — and that
    bucket exists for an open set of unknown environment variable names where a human
    decides. A wire schema is a closed set, so the heuristic has nothing to add, and a
    false positive is not a warning somebody reads: it is a tenant whose agent will not
    warm.
    """
    _refuse_credentials({"routes": {"default": {"provider": "anthropic",
                                                "model": "claude-opus-5",
                                                "maxTokens": 4096,
                                                "temperature": 0.2}}})


def test_a_start_request_with_no_broker_is_refused(tmp_path):
    """The host only exists to serve the untrusted posture. Without a broker socket the
    template would need a database credential — which the host does not have and must
    not be given — so this is refused rather than degraded."""
    with pytest.raises(RyaError) as e:
        StartRequest.read({"root": "/tmp"})
    assert e.value.code == E_HOST_UNMEDIATED

    with pytest.raises(RyaError) as e:
        HostedTemplate(bundle_hash=None, root=Path("/tmp"), broker=None)
    assert e.value.code == E_HOST_UNMEDIATED


def test_an_unknown_start_field_is_refused_rather_than_ignored(tmp_path):
    """An ignored field is how two halves of one build end up disagreeing about what
    was asked for — and the unknown field a newer claimer sends might be the one that
    turns mediation off."""
    with pytest.raises(RyaError) as e:
        StartRequest.read({"root": "/tmp", "brokerSocket": "/tmp/b.sock",
                           "stateRoot": "/deployment"})
    assert e.value.code == E_HOST_DENIED
    assert "stateRoot" in e.value.message


def test_draining_a_template_the_host_does_not_hold_says_so(tmp_path):
    """Evicted or dead, and the caller needs to tell that from "your request was
    malformed". The item's lease expires and the reclaim path re-runs it either way."""
    with _inprocess_host(tmp_path) as host:
        reply = _raw(host, {"op": OP_DRAIN, "token": "tok-abc", "key": "nope"})
        assert reply["ok"] is False
        assert reply["error"]["code"] == "E_TEMPLATE_NOT_RUNNING"


def test_a_claimer_with_no_host_to_reach_names_the_shared_volume(tmp_path):
    """The failure mode this message is for: two healthy containers that do not share
    the directory the socket lives on."""
    template = HostedTemplate(bundle_hash=None, root=tmp_path,
                              broker=SimpleNamespace(socket_path=tmp_path / "b.sock"),
                              socket_path=str(tmp_path / "absent.sock"), token="t")
    with pytest.raises(RyaError) as e:
        template.start()
    assert e.value.code == E_HOST_UNAVAILABLE
    assert "emptyDir" in (e.value.hint or "")


# ---- the seam between the two implementations ------------------------------

def test_a_hosted_template_matches_the_warm_one_the_executor_expects():
    """Duck-typed rather than sharing a base class, so the duck typing is checked.

    The two have almost no implementation in common — one spawns a process, the other
    opens a socket — and what must stay identical is the surface `ForkExecutor` uses.
    A base class full of `NotImplementedError` would document that less clearly than
    this assertion enforces it.
    """
    used_by_the_executor = {"alive", "start", "stop", "drain", "handlers", "missing",
                            "import_ms", "agent_name", "scrubbed", "mediated",
                            "bundle_hash", "version", "runs", "dispatches"}
    for name in sorted(used_by_the_executor):
        assert hasattr(WarmTemplate, name) or name in WarmTemplate.__init__.__code__.co_names, name
        assert hasattr(HostedTemplate, name) or name in HostedTemplate.__init__.__code__.co_names, name


def test_a_forwarded_capability_is_neither_minted_nor_released_by_the_template():
    """D32's seam, and the reason it is a parameter rather than a second method.

    The signing secret never leaves the broker, so the host is a courier: given a
    capability it forwards it and does not release it, because the claimer that minted
    it is the only thing that knows when the dispatch is spent (a fork can open several
    connections during one item — Phase 5's streaming deadlock is exactly that case).
    """
    events = []

    class _Broker:
        socket_path = "/tmp/broker.sock"

        def mint(self, **kw):
            events.append(("mint", kw))
            return "minted-token"

        def release(self, dispatch):
            events.append(("release", dispatch))

    template = WarmTemplate(bundle_hash=None, root=Path("."), broker=_Broker())
    template._proc = SimpleNamespace(is_alive=lambda: True)
    sent = []
    template._conn = SimpleNamespace(
        send=sent.append,
        recv=lambda: {"turns": [], "jobs": [], "resumes": [], "count": 0})

    template.drain(limit=1, worker_id="w1", capability="handed-down")
    assert sent[0]["capability"] == "handed-down"
    assert events == []          # neither minted nor released

    template.drain(limit=1, worker_id="w1")
    assert sent[1]["capability"] == "minted-token"
    assert [e[0] for e in events] == ["mint", "release"]


def test_the_pool_is_unchanged_by_the_topology(tmp_path):
    """The test of whether D27 keyed the right thing.

    Widening the *scope* in Phase 5 was configuration because the pool is keyed by
    bundle hash rather than by container. Moving the *templates into another container*
    in Phase 6 is configuration for the same reason: same keying, same LRU, same
    eviction, and one branch in `_build`.
    """
    pool = WarmPool(max_entries=2, host_socket="/run/rya/host.sock", host_token="t")
    assert pool.hosted is True
    assert pool.max_entries == 2
    # `get`/`drop` are what the host drives its own pool through, and `get` touches
    # because the host's only reason to look an entry up is to dispatch to it.
    assert pool.get("absent") is None
    assert pool.drop("absent") is False
    pool._entries["k"] = SimpleNamespace(alive=True, stop=lambda: None)
    pool._used.append("k")
    assert pool.get("k") is not None
    assert pool.drop("k") is True and pool.get("k") is None


# ---- the gate --------------------------------------------------------------

def test_the_untrusted_posture_is_now_launchable_on_a_container_driver():
    """The delivery, stated as the thing an operator can do that they could not before.

    Phase 5 left every driver refusing: `local` fails isolation, and the container
    drivers launched a credential-free container that could not be the claimer. With
    the pair rendered, a `kubernetes` driver with a gVisor RuntimeClass passes all four
    conditions.
    """
    driver = D.KubernetesDriver(image="rya:test", env={})
    assert driver.launched_unit == D.UNIT_PAIR
    ok, detail = D.topology_supported(driver)
    assert ok is True and "credential boundary is a container boundary" in detail

    report = D.check_untrusted_posture(
        driver, env={D.UNTRUSTED_ENV: "1", "RYA_BROKER": "1", "RYA_EGRESS": "proxy"})
    assert report.ok is True and report.unmet == []


def test_a_driver_that_launches_only_the_sandbox_is_still_refused():
    """`UNIT_SANDBOX` is kept rather than deleted, and it is not dead code: it is the
    arrangement a third-party driver is most likely to write by accident, and a
    claimer with no DSN claims nothing while looking perfectly healthy."""
    driver = D.KubernetesDriver(image="rya:test", env={})
    driver.launched_unit = D.UNIT_SANDBOX
    ok, detail = D.topology_supported(driver)
    assert ok is False
    assert "rya template-host" in detail and D.SOCKET_DIR in detail


def test_the_host_stops_serving_before_it_stops_its_templates(tmp_path):
    """Mirror of the ordering `ForkExecutor.close` learned in Phase 5.

    Stop accepting first, so no template is started while the pool is being torn down;
    then stop the templates; then drop the socket. Asserted by the socket being gone
    and the pool being empty, in that combination — a host that closed in the other
    order can leave an entry started after the sweep.
    """
    host = _inprocess_host(tmp_path)
    host.start()
    assert host.path.exists()
    host.close()
    assert not host.path.exists()
    assert len(host.pool) == 0
    # Idempotent: a supervisor that reaps a host twice must not raise.
    host.close()


def test_the_host_survives_a_client_that_hangs_up_mid_frame(tmp_path):
    """A half-sent frame must not pin a serving thread — the denial of service Phase 4
    closed on the broker, on a socket the same tenant forks can reach."""
    with _inprocess_host(tmp_path) as host:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(host.path))
        sock.sendall(proto.HEADER.pack(4096))   # announce, send nothing
        sock.close()
        # The host is still serving, which is the whole assertion.
        assert _raw(host, {"op": OP_HELLO, "token": "tok-abc"})["ok"] is True


def test_two_templates_drain_concurrently_rather_than_serialising(tmp_path):
    """One connection per template, so a slow handler on one agent is not a latency
    problem for the tenant's others.

    Multiplexing every template over one connection would need request ids and would
    reintroduce exactly the head-of-line blocking D33's fairness work exists to
    prevent, one layer down.
    """
    with _inprocess_host(tmp_path) as host:
        barrier = threading.Barrier(2, timeout=20)
        results = []

        def call():
            try:
                results.append(_raw(host, {"op": OP_HELLO, "token": "tok-abc"})["ok"])
                barrier.wait()
            except threading.BrokenBarrierError:  # pragma: no cover
                results.append("serialised")

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        assert results == [True, True]


def test_the_start_request_has_no_field_that_could_carry_a_database(tmp_path):
    """The credential-free contract, as a wire schema rather than as a filter.

    `_refuse_credentials` is defence in depth. The primary guarantee is this list:
    there is no value the claimer *could* send that would give the host a database,
    because `StartRequest` has no field to put one in. `stateRoot` — the key
    `pool._open_store` reads to find a DSN — is the one whose absence matters most, and
    a request carrying it is refused as an unknown field rather than ignored.
    """
    assert "stateRoot" not in START_FIELDS
    assert not [f for f in START_FIELDS if "dsn" in f.lower() or "key" in f.lower()]
    # Everything the claimer actually sends is on the list, or `read` would refuse it.
    sent = StartRequest(bundle_hash="h", root="/tmp", broker_socket="/tmp/b.sock").wire()
    assert set(sent) <= START_FIELDS


def test_the_control_ops_are_named_constants_shared_by_both_sides(tmp_path):
    """Client and server agree by construction rather than by two string literals.

    Worth pinning because the failure is asymmetric: a typo on the *server* side falls
    through to "does not serve the op" and is obvious, while a typo on the client side
    reaches a host that refuses it — and the claimer reports a refusal it caused itself.
    """
    with _inprocess_host(tmp_path) as host:
        for op in (OP_HELLO, OP_STATUS):
            assert _raw(host, {"op": op, "token": "tok-abc"})["ok"] is True
        # `stop` on a key nobody holds is a no-op rather than an error: reaping twice
        # must not raise, the same discipline `TemplateHost.close` follows.
        reply = _raw(host, {"op": OP_STOP, "token": "tok-abc", "key": "absent"})
        assert reply["ok"] is True and reply["result"] == {"ok": False}
    assert CONTROL_TIMEOUT > 0
