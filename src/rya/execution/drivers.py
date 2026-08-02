"""How a worker gets launched — the substrate seam (D26).

Scheduling **policy** is platform code and has one implementation
(:mod:`rya.execution.supervisor`). Only the launch **mechanism** is pluggable,
and this module is the plug::

    supervisor (D25, ours)  ──calls──►  ExecutionDriver (D26, pluggable)
      what to start, how many                start / stop / list / isolation

This is the third instance of a pattern the tree already has twice. ``open_store``
is "the seam that makes the OSS self-host and the managed cloud the same code"
(``store.py``), and ``resolve_bundle_store`` already spans a local directory, real
S3, MinIO, Ceph and R2 (``bundles.py``). The execution plane should be the third
rather than the exception, because D1 promises "one deployment, one topology, in
our infra, a customer's, or a laptop" and PLATFORM_DESIGN §8 makes self-hosting a
residency control. A plane that depended on one substrate's scheduler would make
that promise false.

**Why not KEDA, ECS autoscaling, or a substrate scheduler.** KEDA's Postgres
scaler is a genuinely good fit for queue-depth scale-to-zero *on Kubernetes* —
which is the problem. Delegating policy means KEDA on k8s, service autoscaling on
ECS and something else on a laptop: three schedulers, three behaviours, three test
matrices, for the component MULTITENANT_DESIGN §9 already calls the subtlest here.
One supervisor plus thin drivers is less total work and one behaviour. Same
instinct D5 applied to api↔worker: no wire protocol, nothing to version between
them. KEDA can become an accelerator on the `kubernetes` driver; it cannot be the
core.

**Declared isolation is the load-bearing part.** Once launching is pluggable,
"is Rya safe for untrusted tenants" stops having a single answer — and an
unenforced answer is a documentation hazard (§9 risk 8: "someone runs the hosted
product on the `docker` driver on a shared kernel and the documentation is not
wrong, just not consulted"). So every driver states what it contains, and
:func:`require_untrusted_posture` — the launch gate — refuses to start when the
declared posture asks for more than the deployment provides.
:func:`require_isolation_for_tenancy` is the narrower question (does the *driver*
isolate?) and is kept for the callers that only need that one. Both are the same
discipline ``worker.preflight`` already applies to a handler-set hole: fail closed,
before anything is claimed.

**Phase 4 adds the sandboxed drivers, and the declaration is now checked.** `docker`
and `kubernetes` ship here (#14/D23). The residual §9 risk 8 named — "the
declaration is a *claim*: nothing verifies that the `kubernetes` driver's pods
really landed on a gVisor `RuntimeClass`" — is closed by
:meth:`ExecutionDriver.verify_isolation`, which asks the sandbox itself what kernel
it is running under rather than trusting the flag that was passed. A driver whose
probe cannot confirm gVisor does not get to *claim* `sandboxed` in the untrusted
posture: :func:`require_untrusted_posture` refuses.

**One refusal, three conditions.** Isolation was the only condition Phase 3 could
check because it was the only one that existed. The untrusted posture needs all
three of D18, D23 and D24 — a sandbox with unmediated credentials is not safe, and
neither is a mediated one with an open network. So the check is now a single
function over all three, because half a security boundary is not a security
boundary and three separate warnings are three separate things to miss.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..errors import RyaError
from ..store import now_iso
from .scope import SCOPE_TENANT, SCOPE_VERSION, scope_key

log = logging.getLogger("rya.execution")

# ---- isolation levels -------------------------------------------------------
# What a driver *contains*, weakest first. These describe the boundary around
# tenant code, not the platform's own correctness: RLS and per-workspace stores
# apply at every level.
ISOLATION_NONE = "none"                    # a process on this host, same kernel, same user
ISOLATION_SHARED_KERNEL = "shared-kernel"  # a container: namespaces + cgroups, one kernel
ISOLATION_SANDBOXED = "sandboxed"          # an intercepted syscall surface (gVisor/runsc)
ISOLATION_MICROVM = "microvm"              # its own kernel (Firecracker, Fargate)

ISOLATION_ORDER = (ISOLATION_NONE, ISOLATION_SHARED_KERNEL,
                   ISOLATION_SANDBOXED, ISOLATION_MICROVM)

# D32: what a driver's launched process actually IS, which decides whether the broker
# can be anywhere the tenant is not.
#
# `local` launches a CLAIMER: platform code that holds the DSN, the seal keys and the
# pooled provider key, and spawns a template that imports tenant code. The credential
# boundary there is the process boundary between the two, which is real but weaker
# than a container boundary and much weaker than a sandbox boundary.
#
# The container drivers launch what their `sandbox_env` describes: a process with no
# credentials at all. That is the right environment for a *template* and the wrong one
# for a claimer — a claimer with no DSN opens a FileStore in its own container, claims
# nothing, and reports idle ticks that look exactly like "no work to do". So a
# container driver cannot host the claimer on its own. The pair it needs — a
# credentialed claimer container beside a credential-free sandbox container sharing the
# broker socket — became launchable once `execution/host.py` made the template startable
# independently, and `UNIT_PAIR` is a driver saying it renders both halves.
#
# `UNIT_SANDBOX` is kept rather than deleted, and it is not dead: it is what a driver
# declares if it launches only the tenant side, and `topology_supported` still refuses
# it. Removing it would remove the ability to describe the broken arrangement, which is
# the arrangement a third-party driver is most likely to write by accident.
UNIT_CLAIMER = "claimer"
UNIT_SANDBOX = "sandbox"
UNIT_PAIR = "pair"


# D17 + D23: hostile tenant code needs a boundary that contains a kernel escape.
# A container does not — "process isolation plus RLS contains a buggy tenant, not
# a hostile one" is the honesty list's own wording.
UNTRUSTED_MIN_ISOLATION = ISOLATION_SANDBOXED

# The declared posture. NOT derived from `multitenant_enabled()`: the multi-tenant
# mode that ships today is deliberately the TRUSTED one (one operator, many of
# their own workspaces), and quietly upgrading its requirements would refuse to
# start for every existing deployment. Whether tenants are hostile is a claim
# about the *business*, which no environment variable can infer — so the operator
# declares it and the platform then holds them to it.
UNTRUSTED_ENV = "RYA_UNTRUSTED_TENANTS"
DRIVER_ENV = "RYA_EXECUTION_DRIVER"
WORKER_COMMAND_ENV = "RYA_WORKER_COMMAND"

DEFAULT_DRIVER = "local"

# ---- sandbox configuration --------------------------------------------------
# The OCI runtime a container driver asks for. `runsc` is gVisor (D23); anything
# else — including the default `runc` — is a shared kernel.
RUNTIME_ENV = "RYA_CONTAINER_RUNTIME"
GVISOR_RUNTIME = "runsc"
IMAGE_ENV = "RYA_SANDBOX_IMAGE"

# D32: the pair's two sockets, and the one directory both containers mount.
#
# In-memory on both substrates, so a socket never touches a disk. The claimer binds
# the broker at `BROKER_SOCKET_PATH` and connects to the host at `HOST_SOCKET_PATH`;
# the sandbox does the mirror image. Two sockets rather than one because they point in
# opposite directions and carry opposite trust: the claimer drives the host (control,
# no data), and the sandbox's forks call the broker (data, no control). Multiplexing
# both over one socket would mean one authorisation surface for two audiences, which
# is how a control op ends up reachable with a dispatch capability.
SOCKET_DIR = "/run/rya"
BROKER_SOCKET_PATH = f"{SOCKET_DIR}/broker.sock"
HOST_SOCKET_PATH = f"{SOCKET_DIR}/host.sock"
K8S_NAMESPACE_ENV = "RYA_K8S_NAMESPACE"
K8S_RUNTIME_CLASS_ENV = "RYA_K8S_RUNTIME_CLASS"
DEFAULT_RUNTIME_CLASS = "gvisor"
# The explicit opt-out. Empty cannot mean it, because an empty environment variable
# is indistinguishable from an unset one throughout this codebase's `or` chains — so
# without a sentinel the `kubernetes` driver would ALWAYS claim `sandboxed`, and an
# operator on a cluster with no gVisor nodes would get pods that never schedule and
# no explanation. `none` says "use the node's default runtime", and the driver then
# declares `shared-kernel`, which is what it would be.
RUNTIME_CLASS_NONE = "none"
K8S_SERVICE_ACCOUNT_ENV = "RYA_K8S_SERVICE_ACCOUNT"

# Per-sandbox resource limits. Not optional in the untrusted posture: without them
# one tenant's runaway handler is the whole node's problem, which §9 risk 1 calls
# the product's cost floor. The defaults are deliberately modest — a handler that
# needs more should say so rather than inherit the host.
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1.0"
DEFAULT_PIDS = 256
MEMORY_ENV = "RYA_SANDBOX_MEMORY"
CPUS_ENV = "RYA_SANDBOX_CPUS"
# D32: the claimer half's limits, and they are separate from the sandbox's on purpose.
# `RYA_SANDBOX_MEMORY` means what it says — the tenant's warm interpreters — and an
# operator raising it for a tenant with ten hot versions should not also be raising the
# ceiling on a process that imports nothing and mostly waits on a socket. Small,
# because that is what a claimer is: a poll loop, a broker and no tenant code.
# Resolved per instance from the driver's own `env`, never at import time — that is
# D8's rule and the seam Phase 4 found broken: a driver that selects itself from a
# passed environment and then configures itself from `os.environ` is two sources of
# truth wearing one name.
CLAIMER_MEMORY_ENV = "RYA_CLAIMER_MEMORY"
CLAIMER_CPUS_ENV = "RYA_CLAIMER_CPUS"
DEFAULT_CLAIMER_MEMORY = "256m"
DEFAULT_CLAIMER_CPUS = "0.5"

# Labels, so a container driver can rebuild its inventory after a restart — the one
# thing `LocalDriver.list` documents that it cannot do.
LABEL_MANAGED = "rya.managed"
LABEL_KEY = "rya.key"
LABEL_WORKSPACE = "rya.workspace"
LABEL_AGENT = "rya.agent"
# D32: which half of the pair a container is. Read by `DockerDriver.list`, which
# counts claimers only — a pair is one worker, and an inventory that counted both
# halves would tell the supervisor it has twice the fleet it asked for.
LABEL_UNIT = "rya.unit"


def isolation_rank(level: str) -> int:
    try:
        return ISOLATION_ORDER.index(level)
    except ValueError:
        # An unknown level is treated as the weakest rather than rejected. A
        # driver from a future release naming a level this build has never heard
        # of must not be trusted MORE than one it understands.
        return 0


def untrusted_tenancy_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = env if env is not None else os.environ
    return (env.get(UNTRUSTED_ENV) or "").strip().lower() in ("1", "true", "yes")


# ---- what to launch ---------------------------------------------------------

@dataclass(frozen=True)
class WorkerSpec:
    """One worker to launch: the scheduling key plus how to run it.

    Deliberately a plain record rather than a ``WorkerKey`` with extras. A
    ``WorkerKey`` describes what a *running* worker is; this describes an intent
    that no process has acted on yet, and the two drift apart the moment a start
    fails.

    ``environment`` is on here for a reason that Phase 2 paid for. D21 has the api
    read the environment pointer to decide which version a queued run is pinned
    to, so an api on ``dev`` and a worker on ``prod`` agree on nothing and every
    turn sits unclaimed. While workers were started by a human or a compose file
    that agreement was a convention with no enforcement. A supervisor starting the
    worker *knows* which environment it is scheduling for, so it passes it
    explicitly and the mismatch stops being reachable by accident.
    """

    workspace: str
    agent: str
    version_id: Optional[str] = None
    bundle_hash: Optional[str] = None
    environment: Optional[str] = None
    project_root: Optional[Path] = None
    idle_exit_seconds: float = 60.0
    poll_seconds: float = 2.0
    concurrency: int = 1
    reason: str = "demand"          # why the supervisor asked for this
    env: Mapping[str, str] = field(default_factory=dict)
    # D27/#19-8b: whether this worker serves one (workspace, agent, version) or the
    # whole tenant. It belongs on the *spec* and not only in the environment because
    # the supervisor decides it and the driver has to spell it onto the argv — a
    # scope carried only in `os.environ` would be inherited by a `local` worker and
    # dropped by a container one, which is the class of bug `--agent` already was.
    scope: str = SCOPE_VERSION
    # Environments whose current version this worker should warm before it claims.
    # Empty by default; see `SupervisorPolicy.prewarm_environments`.
    prewarm: tuple = ()

    @property
    def key(self) -> str:
        """The same string ``WorkerKey.concurrency_key`` produces, on purpose.

        The supervisor compares what it launched against what registered itself,
        and those two identities have to be spelled the same way or the fleet
        looks permanently understaffed and it starts a worker per tick. Both now go
        through :func:`rya.execution.scope.scope_key`, so "the same way" is a
        function call rather than a thing to remember.
        """
        return scope_key(self.workspace, self.agent, self.version_id, scope=self.scope)

    def describe(self) -> dict:
        return {"workspace": self.workspace, "agent": self.agent,
                "versionId": self.version_id, "bundleHash": self.bundle_hash,
                "environment": self.environment, "key": self.key,
                "scope": self.scope, "reason": self.reason}


@dataclass
class WorkerHandle:
    """A launched worker, as the *driver* sees it.

    Not the same thing as the row a worker writes with ``worker_register``. A
    handle exists from the instant the driver launches something; a registration
    exists only once that process got far enough to talk to the database. The gap
    between them is where a crash-on-boot lives, so the supervisor needs both
    views and must not conflate them.
    """

    id: str
    driver: str
    spec: WorkerSpec
    native: Dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=now_iso)

    @property
    def key(self) -> str:
        return self.spec.key

    def describe(self) -> dict:
        return {"id": self.id, "driver": self.driver, "startedAt": self.started_at,
                "native": self.native, **self.spec.describe()}


@dataclass(frozen=True)
class IsolationProbe:
    """What a substrate turned out to be, next to what it claimed.

    Three states for ``verified`` and they are not the same question as "is it safe":

    ``True``
        The probe found gVisor. The declaration is confirmed.
    ``False``
        The probe ran and found a host kernel. The declaration is **wrong**, which is
        worse than an unverifiable one — a driver told to use `runsc` and silently
        given `runc` is exactly §9 risk 8's failure.
    ``None``
        No probe was possible. Honest ignorance.
    """

    driver: str
    declared: str
    verified: Optional[bool]
    detail: str = ""
    signals: Mapping[str, str] = field(default_factory=dict)

    @property
    def effective(self) -> str:
        """The isolation level a fail-closed caller should use.

        A refuted probe downgrades the level to ``shared-kernel``, because that is
        what a container on a host kernel actually is. An unverified one keeps the
        declaration — it may well be true — and is refused separately by
        :func:`require_untrusted_posture`, so the two failures stay distinguishable.
        """
        if self.verified is False and self.declared == ISOLATION_SANDBOXED:
            return ISOLATION_SHARED_KERNEL
        return self.declared

    def describe(self) -> dict:
        return {"driver": self.driver, "declared": self.declared,
                "verified": self.verified, "effective": self.effective,
                "detail": self.detail, "signals": dict(self.signals)}


# What gVisor looks like from inside. Each signal is independent, and one is enough:
# a sandbox that matches any of these is not running on the host kernel.
#
#   /proc/version   gVisor synthesises this rather than passing the host's through,
#                   and the synthetic string is suffixed `-gvisor`. Matching the
#                   SUFFIX rather than the kernel number is the correction Phase 6
#                   made after running a real sentry for the first time — see below.
#   dmesg           the sentinel gVisor writes at boot. Most direct, and often
#                   unreadable in a hardened container, which is why it is not the
#                   only signal.
#   /proc/self/…    gVisor's procfs is a reimplementation and lacks fields the real
#                   one has. Used as corroboration rather than proof.
#
# **The version marker used to be the literal "4.4.0", and that was wrong.** It came
# from a captured fixture, and §9 risk 0's whole point was that a fixture is not a
# measurement: `release-20260727.0` reports `Linux version 4.19.0-gvisor`, so the
# check missed it. The consequence was not a missing signal, it was an inverted one —
# `read_isolation_signals` treats "a version string that is not gVisor's" as positive
# evidence of a HOST kernel, so a genuine sandbox was actively REFUTED, `effective`
# downgraded to `shared-kernel`, and `require_untrusted_posture` refused. And it
# refused in exactly the configuration the driver itself always applies, because
# `--cap-drop=ALL` is what makes `dmesg` — the signal that was still working —
# unreadable.
#
# The suffix is the stable thing. gVisor has moved its reported kernel version at
# least once and will again; it has never not said `-gvisor`.
GVISOR_VERSION_MARKER = "-gvisor"
GVISOR_DMESG_MARKER = "gVisor"
ISOLATION_PROBE_SCRIPT = (
    "printf 'version=%s\\n' \"$(cat /proc/version 2>/dev/null | head -c 200)\"; "
    "printf 'dmesg=%s\\n' \"$(dmesg 2>/dev/null | head -c 200)\"; "
    "printf 'sentry=%s\\n' \"$(ls /proc/self/ 2>/dev/null | tr '\\n' ',' | head -c 200)\""
)


def read_isolation_signals(output: str, *, driver: str, declared: str) -> IsolationProbe:
    """Turn the probe script's output into a verdict.

    Pure, so the interesting half is testable without a container runtime — which
    matters more than usual here, because the alternative is a security check that
    only runs on a machine with gVisor installed and is therefore never exercised in
    CI.
    """
    signals: Dict[str, str] = {}
    for line in (output or "").splitlines():
        name, _, value = line.partition("=")
        if name:
            signals[name.strip()] = value.strip()
    version = signals.get("version", "")
    dmesg = signals.get("dmesg", "")
    if GVISOR_DMESG_MARKER.lower() in dmesg.lower():
        return IsolationProbe(driver=driver, declared=declared, verified=True,
                              detail="the sandbox's own boot log names gVisor",
                              signals=signals)
    if GVISOR_VERSION_MARKER in version.lower():
        return IsolationProbe(driver=driver, declared=declared, verified=True,
                              detail=f"/proc/version reports gVisor's synthetic kernel "
                                     f"({version[:48]!r})",
                              signals=signals)
    if version:
        return IsolationProbe(
            driver=driver, declared=declared, verified=False,
            detail=f"/proc/version reports a host kernel ({version[:60]!r}), so this "
                   "container shares the host's kernel whatever runtime was asked for",
            signals=signals)
    return IsolationProbe(driver=driver, declared=declared, verified=None,
                          detail="the probe produced no readable signal",
                          signals=signals)


class ExecutionDriver:
    """Launch, stop and inventory workers on one substrate.

    Four members, and the fourth is not optional: ``isolation`` is what
    :func:`require_isolation_for_tenancy` reads, so a driver that forgets to
    declare it inherits ``none`` and cannot be used for untrusted tenancy. That
    default is the safe direction.

    ``cold_start_target_ms`` is per driver rather than one global truth.
    ``worker.COLD_START_TARGET_MS`` was written when a worker meant a local
    process; a Fargate task is tens of seconds and a warm k8s pool is hundreds of
    milliseconds. Each driver either meets its own target or documents that it does
    not, which is more useful than one number that two of the four cannot hit.
    """

    name: str = "abstract"
    isolation: str = ISOLATION_NONE
    cold_start_target_ms: int = 2000
    # D32: what `start` actually launches. `claimer` means platform code that holds
    # the credentials and spawns tenant templates itself; `sandbox` means a
    # credential-free process that can only be the tenant side. It defaults to
    # `claimer` because that is what a driver written before D32 launches, and because
    # `topology_supported` reads it — a default of `sandbox` would silently refuse
    # every third-party driver.
    launched_unit: str = UNIT_CLAIMER

    def start(self, spec: WorkerSpec) -> WorkerHandle:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self, handle: WorkerHandle, *, timeout: float = 10.0) -> None:  # pragma: no cover
        raise NotImplementedError

    def list(self, key: Optional[str] = None) -> List[WorkerHandle]:  # pragma: no cover
        raise NotImplementedError

    # ---- shared -----------------------------------------------------------
    def describe(self) -> dict:
        return {"driver": self.name, "isolation": self.isolation,
                "launchedUnit": self.launched_unit,
                "coldStartTargetMs": self.cold_start_target_ms,
                "supportsUntrusted":
                    isolation_rank(self.isolation) >= isolation_rank(UNTRUSTED_MIN_ISOLATION)}

    def worker_argv(self, spec: WorkerSpec) -> List[str]:
        """The `rya worker` command line for ``spec``.

        Lives on the base class because every driver needs it and they must all
        produce the same one: the `docker` driver runs this argv inside a
        container, the `kubernetes` driver puts it in a pod's ``command``. If they
        each built their own, a flag added here would silently apply to one
        substrate — which is precisely the three-behaviours outcome D26 exists to
        avoid.
        """
        argv = [*worker_command(spec.env), "worker",
                "--workspace", spec.workspace,
                "--interval", str(spec.poll_seconds),
                "--concurrency", str(spec.concurrency)]
        if spec.scope == SCOPE_TENANT:
            # A tenant-scoped claimer takes no agent and no version: it resolves both
            # per item, from the queue. Passing either would silently narrow it back
            # to the scope this flag exists to widen — and it would do so while the
            # supervisor still believed one claimer was covering the whole tenant, so
            # the sibling agents' work would sit unserved with a worker registered
            # against their key.
            argv += ["--scope", SCOPE_TENANT, "--fork"]
            for name in spec.prewarm:
                argv += ["--prewarm", name]
            if spec.environment:
                argv += ["--env", spec.environment]
            if spec.idle_exit_seconds:
                argv += ["--idle-exit", str(spec.idle_exit_seconds)]
            return argv
        if spec.agent:
            # Added in Phase 4, and it was a real defect rather than a nicety. The
            # supervisor decides a key of (workspace, AGENT, version) and this argv
            # never carried the agent, so a launched worker resolved its own from the
            # mounted `rya.agent.yaml`. With `--version` the version record names the
            # agent and the omission was invisible; with `--env` — which is how the
            # supervisor schedules an unpinned key — it meant scheduling for `billing`
            # and launching a worker serving `support`. Exactly the class of cross-agent
            # mix-up D22 closed on the claim path, one layer up in the launch path.
            argv += ["--agent", spec.agent]
        if spec.version_id:
            argv += ["--version", spec.version_id]
        elif spec.environment:
            # No pinned version and an environment named: let the worker resolve
            # the pointer itself. Not equivalent to passing `--version` — the
            # pointer can move between the decision and the launch, and the worker
            # resolving it is the one that will actually serve the result.
            argv += ["--env", spec.environment]
        if spec.idle_exit_seconds:
            argv += ["--idle-exit", str(spec.idle_exit_seconds)]
        return argv

    def worker_env(self, spec: WorkerSpec) -> Dict[str, str]:
        """The environment a launched worker inherits.

        ``RYA_ENVIRONMENT`` is forced to the spec's environment rather than
        inherited, because the supervisor's whole reason for knowing it is to stop
        the api and its workers from disagreeing (see :class:`WorkerSpec`).

        **Inheriting ``os.environ`` is correct here and wrong for a sandbox.** A
        worker launched by this method is *platform* code — it is the claimer, and it
        needs the DSN and the keys precisely so that its forks do not. A container
        driver overrides this with :meth:`sandbox_env`, which builds the environment
        from nothing instead, and that difference is where D18's exit criterion is
        genuinely met rather than merely scrubbed.
        """
        out = {**os.environ, **{k: str(v) for k, v in (spec.env or {}).items()}}
        if spec.environment:
            out["RYA_ENVIRONMENT"] = spec.environment
        if spec.project_root is not None:
            out["RYA_PROJECT"] = str(spec.project_root)
        return out

    def verify_isolation(self) -> "IsolationProbe":
        """Ask the substrate what it is, rather than trusting what was declared.

        §9 risk 8's residual, and the reason it is a method on the base class: the
        declaration is a class attribute, so the only thing that can contradict it is
        a measurement. A driver that cannot measure returns an ``unknown`` probe, and
        :func:`require_untrusted_posture` treats ``unknown`` as a refusal — the same
        direction :func:`isolation_rank` takes for an unrecognised level.
        """
        return IsolationProbe(driver=self.name, declared=self.isolation,
                              verified=None,
                              detail="this driver cannot probe its own substrate")


def worker_command(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """How to invoke this build's CLI, as an argv prefix.

    ``sys.executable -m rya.cli`` rather than a ``rya`` found on ``PATH``. The
    supervisor must launch **its own** code: D5's "one deployable, nothing to
    version between them" only holds if the process that schedules and the process
    that executes are the same build, and a ``PATH`` lookup can resolve to a
    different install (a layered venv, a system-wide `pipx`, an operator's
    shell rc). Going through ``sys.executable`` guarantees the same interpreter and
    the same ``sys.path``, so it is the same ``rya`` the supervisor imported.

    ``RYA_WORKER_COMMAND`` overrides it, which is what an image with an entrypoint
    wrapper needs (``docker exec``, ``tini``, a `uv run` shim).
    """
    env = env if env is not None else {}
    override = (env.get(WORKER_COMMAND_ENV) or os.environ.get(WORKER_COMMAND_ENV) or "").strip()
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "rya.cli"]


# ---- the local driver -------------------------------------------------------

class LocalDriver(ExecutionDriver):
    """A worker is a subprocess on this host. Isolation: **none**.

    What it is for: `rya dev`, a laptop, a single-node self-host, macOS where
    `runsc` cannot run at all, and — most importantly for Phase 3 — giving
    scheduling policy something real to run against before any container work
    exists. Every supervisor test in the suite drives this driver, which is how
    the same policy code can later be pointed at `docker` with nothing in the
    policy layer branching on substrate.

    What it is not for: untrusted tenants. It declares ``none`` and
    :func:`require_isolation_for_tenancy` refuses that combination at startup.
    """

    name = "local"
    isolation = ISOLATION_NONE
    # A local worker's cold start is bundle materialisation plus an import; the
    # existing 2000 ms budget was measured against exactly this shape.
    cold_start_target_ms = 2000

    def __init__(self, *, log_dir: Optional[Path] = None,
                 env: Optional[Mapping[str, str]] = None) -> None:
        # `env` is accepted and unused. Every driver takes it so `resolve_driver` can
        # thread a DECLARED environment through (D8) without inspecting signatures,
        # and this driver has nothing configurable to read from it. Dropping the
        # parameter would make the seam depend on which driver was chosen.
        self._procs: Dict[str, "subprocess.Popen"] = {}
        self._handles: Dict[str, WorkerHandle] = {}
        self.log_dir = Path(log_dir) if log_dir else None
        self._seq = 0

    def _mint(self) -> str:
        self._seq += 1
        return f"loc_{os.getpid()}_{self._seq}"

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        argv = self.worker_argv(spec)
        cwd = str(spec.project_root) if spec.project_root else None
        handle_id = self._mint()
        out: Any = subprocess.DEVNULL
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            out = open(self.log_dir / f"{handle_id}.log", "ab", buffering=0)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=self.worker_env(spec),
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            raise RyaError(
                "E_WORKER_START_FAILED",
                f"Could not launch a worker for {spec.key}: {exc}",
                hint=f"The command was {' '.join(argv)!r}. Set {WORKER_COMMAND_ENV} "
                     "if this build's CLI is reached some other way.",
            ) from exc
        handle = WorkerHandle(id=handle_id, driver=self.name, spec=spec,
                              native={"pid": proc.pid, "argv": argv})
        self._procs[handle_id] = proc
        self._handles[handle_id] = handle
        log.info("started worker %s for %s (pid %s, %s)", handle_id, spec.key,
                 proc.pid, spec.reason)
        return handle

    def stop(self, handle: WorkerHandle, *, timeout: float = 10.0) -> None:
        """Ask, then insist.

        ``terminate`` first because a worker's own shutdown is cooperative and
        bounded by its poll interval (``Worker.stop``), and a cooperative exit
        deregisters with a reason and finishes the item it is holding. SIGKILL is
        the fallback, and it produces exactly the state the liveness fix was
        written for: a registration nobody will ever deregister, which reads
        `lost` once the heartbeat ages out.
        """
        proc = self._procs.get(handle.id)
        self._handles.pop(handle.id, None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            deadline = time.monotonic() + max(0.0, timeout)
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                log.warning("worker %s ignored SIGTERM for %.0fs; killing", handle.id, timeout)
                proc.kill()
        self._procs.pop(handle.id, None)

    def list(self, key: Optional[str] = None) -> List[WorkerHandle]:
        """Handles **this process** launched and that are still running.

        Deliberately not a host scan. A `docker` driver can ask the daemon what is
        running and rebuild its inventory after a restart; a subprocess parent
        cannot — the children are orphaned, not enumerable. So the local driver's
        inventory is process-local, and the supervisor treats the *worker
        registry* in the store as the durable answer to "what is serving this
        key". That split is not a local-driver quirk to paper over: a driver's
        inventory can always be stale or partial, which is why
        `supervisor.observe` reconciles the two rather than trusting either.
        """
        for hid, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._procs.pop(hid, None)
                self._handles.pop(hid, None)
        out = list(self._handles.values())
        return [h for h in out if key is None or h.key == key]

    def close(self) -> None:
        for handle in self.list():
            self.stop(handle)


# ---- container drivers (D23) ------------------------------------------------

class ContainerDriver(ExecutionDriver):
    """Shared behaviour for `docker` and `kubernetes`: the sandbox's shape.

    Both substrates need the same four things and would otherwise implement them
    twice, which is the three-behaviours outcome D26 exists to avoid one level down:

    * an **explicitly constructed environment** (:meth:`sandbox_env`) rather than an
      inherited one — this is where "the tenant process holds no credentials" is
      actually true rather than scrubbed after the fact,
    * **resource limits**, without which one tenant is the node's problem,
    * a **hardened default** — read-only root, no capabilities, no new privileges,
      non-root user — which is right whether or not gVisor is underneath,
    * and the **isolation probe**, so the declaration is measured.

    **What it launches is a pair, not a container (D32).** Phase 5 found that
    ``sandbox_env`` describes a process with no credentials — exactly right for the
    process that imports tenant code and exactly wrong for the one that has to open the
    database and *be* the broker — and that both were the same container, so the
    untrusted posture had no launchable topology on any driver. Phase 6 builds the
    split: :meth:`claimer_env` configures the credentialed half, :meth:`sandbox_env`
    the credential-free half, :meth:`host_argv` is what the second one runs, and the
    two share :data:`SOCKET_DIR`.

    **Both postures get the pair, and that is a simplification rather than a cost.**
    The alternative — one container for trusted deployments and two for untrusted — is
    a second launch path that only the less-tested posture exercises, and the trusted
    single container was never actually correct either: it was configured by
    ``sandbox_env`` and had no DSN, so it would have claimed nothing. One path, and
    the posture decides what is *in* the environments rather than how many there are.
    """

    # D32. `topology_supported` reads it: a driver that renders both halves can serve
    # untrusted tenants, one that renders only the tenant half cannot.
    launched_unit: str = UNIT_PAIR

    # Set by subclasses from configuration. `runsc` present means gVisor was asked
    # for; whether it was GIVEN is what the probe answers.
    runtime: str = ""

    def __init__(self, *, image: str = "", runtime: str = "",
                 memory: str = DEFAULT_MEMORY, cpus: str = DEFAULT_CPUS,
                 env: Optional[Mapping[str, str]] = None) -> None:
        src = env if env is not None else os.environ
        self.image = image or (src.get(IMAGE_ENV) or "").strip()
        self.runtime = (runtime or src.get(RUNTIME_ENV) or "").strip()
        self.memory = memory or (src.get(MEMORY_ENV) or DEFAULT_MEMORY)
        self.cpus = cpus or (src.get(CPUS_ENV) or DEFAULT_CPUS)
        self.claimer_memory = src.get(CLAIMER_MEMORY_ENV) or DEFAULT_CLAIMER_MEMORY
        self.claimer_cpus = src.get(CLAIMER_CPUS_ENV) or DEFAULT_CLAIMER_CPUS
        self._probe: Optional[IsolationProbe] = None

    @property
    def sandboxed(self) -> bool:
        return self.runtime == GVISOR_RUNTIME

    def sandbox_env(self, spec: WorkerSpec, *, token: str = "") -> Dict[str, str]:
        """The sandbox half's environment, built from nothing.

        The important line in this file. :meth:`ExecutionDriver.worker_env` starts
        from ``os.environ`` because it launches platform code; this starts from ``{}``
        because it launches a container that will import tenant code. The DSN, the
        seal key, the pooled provider key and the bucket credential are not omitted by
        a filter — they were never added, so there is no list to forget to update.

        What DOES cross is what a sandbox needs to be useful, and none of it is a
        standing credential to anything outside the pair: which environment it serves,
        where the two sockets are, and the host token — which authorises "you may ask
        me to import a bundle" and nothing else, and which the template scrubs before
        the tenant's own code runs.
        """
        out: Dict[str, str] = {
            "RYA_ENVIRONMENT": spec.environment or "",
            "RYA_WORKSPACE": spec.workspace,
            "RYA_AGENT": spec.agent,
            # Mediation and network posture are REQUIRED of a sandbox, so they are set
            # here rather than passed in: a container launched by this driver that
            # somehow had them off would be a sandbox with an open network and a
            # database credential, which is the configuration the whole phase exists
            # to make unreachable.
            "RYA_BROKER": "1",
            "RYA_EGRESS": "proxy",
            # D32. The host binds the first and its templates' forks dial the second.
            "RYA_TEMPLATE_HOST": HOST_SOCKET_PATH,
            "RYA_TEMPLATE_HOST_TOKEN": token,
            "RYA_BROKER_SOCKET": BROKER_SOCKET_PATH,
        }
        out.update({k: str(v) for k, v in (spec.env or {}).items()})
        return {k: v for k, v in out.items() if v != ""}

    def claimer_env(self, spec: WorkerSpec, *, token: str = "") -> Dict[str, str]:
        """The credentialed half's environment (D32).

        Inherits, unlike :meth:`sandbox_env`, and the asymmetry *is* the boundary: this
        container is the one that holds the DSN, the seal key and the pooled provider
        key, precisely so that the one beside it holds none of them. Phase 5's finding
        was that these two methods were the same method.

        The three values forced here are forced rather than inherited because they
        describe *this pair*: a claimer that inherited a stale ``RYA_BROKER_SOCKET``
        from the supervisor would bind its broker somewhere the sandbox cannot see, and
        the failure would look like a tenant whose every call times out.
        """
        out = self.worker_env(spec)
        out["RYA_TEMPLATE_HOST"] = HOST_SOCKET_PATH
        out["RYA_TEMPLATE_HOST_TOKEN"] = token
        out["RYA_BROKER_SOCKET"] = BROKER_SOCKET_PATH
        # Mediation is not optional on this topology. The claimer's pool holds
        # `HostedTemplate`s, and a hosted template with no broker refuses to start
        # (`E_TEMPLATE_HOST_UNMEDIATED`) — so an unset `RYA_BROKER` here would not
        # quietly degrade to the weak topology, it would fail to serve. Setting it is
        # how the pair stays the thing the driver said it launched.
        out["RYA_BROKER"] = "1"
        out.setdefault("RYA_EGRESS", "proxy")
        return {k: v for k, v in out.items() if v != ""}

    def host_argv(self, spec: WorkerSpec) -> List[str]:
        """What the sandbox container runs: the template host, and nothing else.

        Note there is no `worker` in it. The sandbox container never claims, never
        registers and never heartbeats — it waits to be told which bundle to import.
        That is the whole content of D32's "the broker is a sibling, never a parent".
        """
        return [*worker_command(spec.env), "template-host",
                "--socket", HOST_SOCKET_PATH]

    def hardening_args(self, *, memory: str = "", cpus: str = "") -> List[str]:
        """Docker/OCI flags that are right at every isolation level.

        ``memory`` and ``cpus`` override the sandbox's, which is how the pair's two
        halves get different limits from one function. Everything else — read-only
        root, no capabilities, no new privileges, non-root, a bounded noexec tmpfs —
        is identical on both, because none of it is about *whose* code is running.

        §7 is explicit that hardened Docker "is materially better than the default
        and is the right configuration for the `docker` driver. It still cannot
        contain a kernel bug, which is why it declares `shared-kernel`". Both halves
        of that sentence are implemented here: the hardening always applies, and it
        does not change what the driver claims.
        """
        return ["--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                "--user", "10001:10001",
                f"--memory={memory or self.memory}", f"--cpus={cpus or self.cpus}",
                f"--pids-limit={DEFAULT_PIDS}",
                # A writable tmpfs, because the bundle cache and the interpreter both
                # need somewhere to write and `--read-only` otherwise makes the
                # sandbox unable to start. Bounded and noexec.
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"]

    def labels(self, spec: WorkerSpec) -> Dict[str, str]:
        return {LABEL_MANAGED: "1", LABEL_KEY: spec.key,
                LABEL_WORKSPACE: spec.workspace, LABEL_AGENT: spec.agent}

    def describe(self) -> dict:
        return {**super().describe(), "image": self.image or None,
                "runtime": self.runtime or "(default)",
                "memory": self.memory, "cpus": self.cpus,
                "probe": (self._probe.describe() if self._probe else None)}

    def _require_image(self) -> str:
        if not self.image:
            raise RyaError(
                "E_WORKER_START_FAILED",
                f"The '{self.name}' driver needs a sandbox image and none is declared.",
                hint=f"Set {IMAGE_ENV} to an image containing this build of rya. It "
                     "must be the SAME build as the supervisor (D5: one deployable, "
                     "nothing to version between the halves).",
            )
        return self.image


def _run(argv: List[str], *, timeout: float = 30.0, check: bool = True) -> str:
    """Run a CLI and return stdout, turning a failure into a Rya error.

    Both container drivers shell out rather than using a client library. That is a
    deliberate dependency choice, the same one ``providers/llm.py`` makes for HTTP:
    the real path works on a base install, and `docker` and `kubectl` are already
    present wherever these drivers make sense.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RyaError(
            "E_WORKER_START_FAILED", f"'{argv[0]}' is not on PATH.",
            hint=f"The {argv[0]} CLI is how this driver talks to its substrate.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RyaError("E_WORKER_START_FAILED",
                       f"'{' '.join(argv[:3])}…' did not finish within {timeout:.0f}s.") from exc
    if check and proc.returncode != 0:
        raise RyaError(
            "E_WORKER_START_FAILED",
            f"'{' '.join(argv[:4])}…' failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
    return proc.stdout


class DockerDriver(ContainerDriver):
    """A worker is a container on this host.

    **Isolation depends on configuration, and the class attribute cannot say so.**
    ``isolation`` is resolved per instance: ``shared-kernel`` for the default runtime,
    ``sandboxed`` with ``--runtime=runsc``. That is the one driver where the honest
    answer is "it depends", and making it an instance attribute rather than a class
    one is what keeps :func:`require_untrusted_posture` reading the truth instead of
    a class-level average.

    Unlike `local`, ``list`` is a real substrate query: the daemon knows what is
    running, so this driver's inventory survives a supervisor restart. That is the gap
    ``LocalDriver.list`` documents it cannot close.
    """

    name = "docker"
    isolation = ISOLATION_SHARED_KERNEL
    # A container start plus bundle materialisation plus import. Higher than local
    # because there is an image to start; far below a Fargate task.
    cold_start_target_ms = 4000

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        # Per-instance, shadowing the class attribute. See the class docstring.
        self.isolation = (ISOLATION_SANDBOXED if self.sandboxed
                          else ISOLATION_SHARED_KERNEL)

    def _name_for(self, spec: WorkerSpec) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in spec.key)
        return f"rya-{safe}-{os.urandom(3).hex()}"

    def sandbox_argv(self, spec: WorkerSpec, *, name: str, volume: str,
                     token: str) -> List[str]:
        """`docker run` for the credential-free half. Rendered, so it is testable dry."""
        argv = ["docker", "run", "--detach", "--name", name]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        # D24: no route out. The sandbox reaches allowlisted hosts through the
        # broker's mediated fetch and nothing else, so a raw urllib request fails at
        # connect() rather than at a Python check that tenant code could skip.
        argv += ["--network", "none"]
        argv += self.hardening_args()
        argv += ["--volume", f"{volume}:{SOCKET_DIR}"]
        for key, value in {**self.labels(spec), LABEL_UNIT: UNIT_SANDBOX}.items():
            argv += ["--label", f"{key}={value}"]
        for key, value in self.sandbox_env(spec, token=token).items():
            argv += ["--env", f"{key}={value}"]
        return argv + [self._require_image(), *self.host_argv(spec)]

    def claimer_argv(self, spec: WorkerSpec, *, name: str, volume: str,
                     token: str) -> List[str]:
        """`docker run` for the credentialed half.

        Three deliberate differences from :meth:`sandbox_argv`, each of which is the
        boundary in one direction:

        * **no ``--runtime``**. gVisor is for the process running tenant code. Paying
          its syscall tax on the claimer — which imports nothing and mostly waits on a
          database — would be cost with no security content.
        * **a network**. This half talks to Postgres, the object store and the model
          providers. It is the *reason* the other half can have ``--network none``.
        * **it inherits its environment**. See :meth:`claimer_env`.

        Still hardened, still non-root, still read-only: platform code has no more
        business writing to its root filesystem than tenant code does.
        """
        argv = ["docker", "run", "--detach", "--name", name]
        argv += self.hardening_args(memory=self.claimer_memory, cpus=self.claimer_cpus)
        argv += ["--volume", f"{volume}:{SOCKET_DIR}"]
        for key, value in {**self.labels(spec), LABEL_UNIT: UNIT_CLAIMER}.items():
            argv += ["--label", f"{key}={value}"]
        for key, value in self.claimer_env(spec, token=token).items():
            argv += ["--env", f"{key}={value}"]
        return argv + [self._require_image(), *self.worker_argv(spec)]

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        """Launch the D32 pair: a named volume, then the sandbox, then the claimer.

        **The order is not arbitrary.** The sandbox goes up first because the claimer
        dials it — a claimer that starts first gets ``E_TEMPLATE_HOST_UNAVAILABLE`` on
        its first warm and would have to retry. And the volume goes up first because
        both mount it; a `docker run` with a missing volume creates one implicitly,
        which would silently give the two halves *different* directories and produce
        the most confusing failure available here (two healthy containers, one socket
        each, no contact).

        If the claimer fails to start, the sandbox is torn down rather than left
        running. A half-started pair is a container holding a tenant's warm
        interpreters that nothing will ever dispatch to.
        """
        self._require_image()
        name = self._name_for(spec)
        sandbox_name, volume = f"{name}-sandbox", f"{name}-sock"
        token = secrets.token_urlsafe(32)
        _run(["docker", "volume", "create", volume])
        sandbox = self.sandbox_argv(spec, name=sandbox_name, volume=volume, token=token)
        claimer = self.claimer_argv(spec, name=name, volume=volume, token=token)
        sandbox_id = _run(sandbox).strip()
        try:
            claimer_id = _run(claimer).strip()
        except RyaError:
            self._teardown(name=sandbox_name, volume=volume)
            raise
        handle = WorkerHandle(
            id=name, driver=self.name, spec=spec,
            native={"container": claimer_id[:12] or name,
                    "sandbox": sandbox_id[:12] or sandbox_name,
                    "sandboxName": sandbox_name, "volume": volume,
                    "argv": claimer, "sandboxArgv": sandbox})
        log.info("started pair %s + %s for %s (runtime=%s, %s)", name, sandbox_name,
                 spec.key, self.runtime or "default", spec.reason)
        return handle

    def _teardown(self, *, name: str, volume: str, timeout: int = 10) -> None:
        _run(["docker", "stop", "--timeout", str(timeout), name], check=False)
        _run(["docker", "rm", "--force", name], check=False)
        if volume:
            _run(["docker", "volume", "rm", "--force", volume], check=False)

    def stop(self, handle: WorkerHandle, *, timeout: float = 10.0) -> None:
        """Ask, then insist — the same discipline as `local`, one layer out.

        ``docker stop`` sends SIGTERM and escalates to SIGKILL after its own timeout,
        which is exactly ``LocalDriver.stop``'s behaviour, so a cooperative worker
        still deregisters with a reason and a wedged one still becomes `lost`.

        **The claimer goes down first**, mirroring `ForkExecutor.close`: it is the one
        holding queue leases, and stopping the templates under a claimer that is still
        dispatching would turn a clean shutdown into a run of `E_TEMPLATE_LOST`. The
        volume goes last, and it is removed rather than left — a named volume per
        launch that nobody deletes is an unbounded leak on a long-lived host, and this
        one holds two sockets and nothing else.
        """
        native = handle.native or {}
        _run(["docker", "stop", "--timeout", str(int(timeout)), handle.id], check=False)
        _run(["docker", "rm", "--force", handle.id], check=False)
        self._teardown(name=str(native.get("sandboxName") or f"{handle.id}-sandbox"),
                       volume=str(native.get("volume") or f"{handle.id}-sock"),
                       timeout=int(timeout))

    def list(self, key: Optional[str] = None) -> List[WorkerHandle]:
        # Filtered to the CLAIMER half. A pair is one worker, and counting both halves
        # would make `supervisor.observe` believe it has twice the fleet it asked for
        # and reap the difference.
        argv = ["docker", "ps", "--filter", f"label={LABEL_MANAGED}=1",
                "--filter", f"label={LABEL_UNIT}={UNIT_CLAIMER}",
                "--format", "{{.Names}}\t{{.Label \"rya.key\"}}\t"
                            "{{.Label \"rya.workspace\"}}\t{{.Label \"rya.agent\"}}"]
        out = _run(argv, check=False)
        handles: List[WorkerHandle] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, container_key, workspace, agent = parts[:4]
            if key is not None and container_key != key:
                continue
            # The spec is REBUILT from labels, not remembered. A supervisor that
            # restarted has no memory of what it launched, and the point of a
            # substrate query is that it does not need one.
            version = container_key.split(":")[-1]
            handles.append(WorkerHandle(
                id=name, driver=self.name,
                spec=WorkerSpec(workspace=workspace, agent=agent,
                                version_id=None if version == "local" else version),
                # Derived from the claimer's name rather than remembered, which is the
                # same discipline as the spec above and matters more: without these,
                # `stop` on a rebuilt handle would kill the claimer and leave the
                # sandbox and its volume behind on every supervisor restart. The names
                # are a pure function of the claimer's, which is why `start` builds
                # them that way instead of using two random ids.
                native={"rebuiltFromLabels": True,
                        "sandboxName": f"{name}-sandbox", "volume": f"{name}-sock"}))
        return handles

    def verify_isolation(self) -> IsolationProbe:
        """Ask a throwaway container what kernel it is on.

        Run against the *same* image and runtime a worker would get, because the
        question is whether this configuration produces gVisor — not whether gVisor
        exists on the host. A `docker` driver with no `--runtime` is refuted rather
        than unknown: it is definitely a shared kernel, and saying so is more useful
        than declining to answer.
        """
        if not self.sandboxed:
            self._probe = IsolationProbe(
                driver=self.name, declared=self.isolation, verified=False,
                detail=f"no {GVISOR_RUNTIME} runtime is configured, so containers "
                       "share the host kernel by construction")
            return self._probe
        image = self._require_image()
        argv = ["docker", "run", "--rm", "--runtime", self.runtime,
                "--network", "none", "--entrypoint", "/bin/sh",
                image, "-c", ISOLATION_PROBE_SCRIPT]
        try:
            out = _run(argv, timeout=60.0)
        except RyaError as exc:
            self._probe = IsolationProbe(
                driver=self.name, declared=self.isolation, verified=None,
                detail=f"the probe container could not be run: {exc.message[:200]}")
            return self._probe
        self._probe = read_isolation_signals(out, driver=self.name,
                                             declared=self.isolation)
        return self._probe

    def close(self) -> None:
        for handle in self.list():
            self.stop(handle)


class KubernetesDriver(ContainerDriver):
    """A worker is a Pod, sandboxed by a gVisor ``RuntimeClass``.

    The `RuntimeClass` indirection is what makes D23 portable: EKS, GKE, AKS and
    on-prem all express "run this pod under gVisor" the same way, so the driver does
    not learn a cloud. What it *cannot* know is whether the cluster's RuntimeClass
    actually maps to `runsc` — that is a node-level configuration — which is precisely
    why :meth:`verify_isolation` asks a pod instead of reading the manifest back.

    Renders a manifest and applies it, rather than driving the API with a client
    library. Two reasons: no new dependency on the base install (the same choice
    ``providers/llm.py`` makes), and the manifest is inspectable — an operator can run
    :meth:`render` and read exactly what the platform would create, which for the
    component that carries the isolation claim is worth more than an object graph.
    """

    name = "kubernetes"
    isolation = ISOLATION_SANDBOXED
    # A pod schedule plus image pull plus bundle plus import. Warm nodes make this
    # achievable; a cold node pool does not, which is what pre-warming is for.
    cold_start_target_ms = 8000

    def __init__(self, *, namespace: str = "", runtime_class: str = "",
                 service_account: str = "", env: Optional[Mapping[str, str]] = None,
                 **kw) -> None:
        super().__init__(env=env, **kw)
        src = env if env is not None else os.environ
        self.namespace = namespace or (src.get(K8S_NAMESPACE_ENV) or "default").strip()
        declared = (runtime_class or src.get(K8S_RUNTIME_CLASS_ENV)
                    or DEFAULT_RUNTIME_CLASS).strip()
        self.runtime_class = "" if declared.lower() == RUNTIME_CLASS_NONE else declared
        self.service_account = (service_account
                                or src.get(K8S_SERVICE_ACCOUNT_ENV) or "").strip()
        # A RuntimeClass is how gVisor is requested here, so `runtime` is set from it
        # rather than from RYA_CONTAINER_RUNTIME — otherwise `sandboxed` on this
        # driver would depend on a variable that means nothing to Kubernetes.
        self.runtime = GVISOR_RUNTIME if self.runtime_class else ""
        # Per-instance, exactly as on `DockerDriver`, and for a reason worth naming:
        # leaving `sandboxed` on the class meant a driver explicitly configured with
        # NO RuntimeClass still claimed it, and the launch gate reads the
        # declaration. A driver's honesty about itself cannot be a class constant when
        # the answer depends on configuration.
        self.isolation = (ISOLATION_SANDBOXED if self.runtime_class
                          else ISOLATION_SHARED_KERNEL)

    def _pod_name(self, spec: WorkerSpec) -> str:
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in spec.key.lower())
        return f"rya-{safe.strip('-')[:40]}-{os.urandom(3).hex()}"

    def render(self, spec: WorkerSpec, *, name: Optional[str] = None,
               token: Optional[str] = None) -> dict:
        """The Pod manifest. Pure, so the interesting parts are testable with no cluster.

        This is the function a security review should read: it is where the network
        policy, the RuntimeClass, the security context and the environment all become
        one declarative object.

        **Two containers, and the pod is the D32 pair** (Phase 6). Kubernetes makes
        this the easy substrate for the arrangement: one pod, one lifecycle, one
        `emptyDir` for the two sockets, and a network policy that already applies to
        both. What the pod does *not* get is a shared PID namespace — the default, left
        alone deliberately — so the claimer's memory is not readable from the sandbox
        even though the two are scheduled together.

        The RuntimeClass covers the whole pod, so the claimer runs under gVisor too.
        That is a cost with no security content (see `DockerDriver.claimer_argv`, which
        can and does avoid it) and it is accepted here rather than worked around: the
        alternative is two pods and a cross-pod socket, which needs a shared PVC or a
        TCP broker, and a TCP broker is a listener a tenant's neighbours can reach.
        """
        image = self._require_image()
        token = token or secrets.token_urlsafe(32)
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {
                "name": name or self._pod_name(spec),
                "namespace": self.namespace,
                "labels": {k.replace(".", "-"): v
                           for k, v in self.labels(spec).items()},
            },
            "spec": {
                # The isolation request. Absent, the pod runs on the node's default
                # runtime and this driver's declaration would be a fiction.
                **({"runtimeClassName": self.runtime_class} if self.runtime_class else {}),
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                **({"serviceAccountName": self.service_account}
                   if self.service_account else {}),
                # D18 again, at the substrate: a mounted service-account token is a
                # credential for the cluster's API, which is a platform credential in
                # a tenant process by any reading.
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                    "runAsGroup": 10001, "fsGroup": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    # The credentialed half. Named `claimer` rather than `worker`
                    # because the pod now has two and "worker" would be ambiguous
                    # exactly where an operator reading `kubectl logs` needs it not
                    # to be.
                    {
                        "name": "claimer",
                        "image": image,
                        "command": self.worker_argv(spec),
                        "env": [{"name": k, "value": v}
                                for k, v in sorted(
                                    self.claimer_env(spec, token=token).items())],
                        "resources": {
                            "requests": {"memory": self.claimer_memory,
                                         "cpu": self.claimer_cpus},
                            "limits": {"memory": self.claimer_memory,
                                       "cpu": self.claimer_cpus},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"},
                                         {"name": "broker", "mountPath": SOCKET_DIR}],
                    },
                    # The credential-free half: the template host, and the only
                    # container in this pod that will ever import tenant code. The
                    # tenant's resource limits are HERE and not on the claimer — one
                    # cgroup around every warm interpreter this tenant owns, which is
                    # the trade §7.1 records as giving up per-agent limits.
                    {
                        "name": "sandbox",
                        "image": image,
                        "command": self.host_argv(spec),
                        "env": [{"name": k, "value": v}
                                for k, v in sorted(
                                    self.sandbox_env(spec, token=token).items())],
                        "resources": {
                            "requests": {"memory": self.memory, "cpu": self.cpus},
                            "limits": {"memory": self.memory, "cpu": self.cpus},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"},
                                         {"name": "broker", "mountPath": SOCKET_DIR}],
                    },
                ],
                "volumes": [
                    {"name": "scratch", "emptyDir": {"medium": "Memory",
                                                     "sizeLimit": "256Mi"}},
                    # The two sockets, on an in-memory volume so neither ever touches a
                    # disk. **This volume is where D32 lands**, and it is the one part
                    # of the arrangement Phase 5 got right before the rest existed: it
                    # used to carry a comment claiming a sidecar this function did not
                    # render. The sidecar is above now, and the volume is unchanged.
                    {"name": "broker", "emptyDir": {"medium": "Memory"}},
                ],
            },
        }

    def network_policy(self, spec: WorkerSpec, *, posture=None) -> dict:
        """The D24 half a manifest can express: deny all egress.

        Separate from the Pod on purpose. A ``NetworkPolicy`` is a namespace-scoped
        object with its own lifecycle, and an operator reviewing "can this pod reach
        the internet" should be able to read one small document rather than find the
        answer inside a pod spec.

        Egress is denied outright rather than allowlisted by CIDR. Allowlisting hosts
        at the network layer means resolving names to addresses and pinning them, and
        a CDN rotating an address then silently breaks an allowed call — so the
        mediated ``egress.fetch`` is the route out, and this object's job is to make
        sure there is no other one.
        """
        return {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": f"rya-deny-egress-{spec.agent or 'all'}",
                         "namespace": self.namespace},
            "spec": {
                "podSelector": {"matchLabels": {LABEL_MANAGED.replace(".", "-"): "1"}},
                "policyTypes": ["Egress"],
                # An empty egress list is deny-all. Written explicitly rather than
                # omitted, because an absent `egress` key with `policyTypes: [Egress]`
                # means the same thing and reads like an oversight.
                "egress": [],
            },
        }

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        import json as _json

        name = self._pod_name(spec)
        manifest = self.render(spec, name=name)
        argv = ["kubectl", "apply", "-n", self.namespace, "-f", "-"]
        try:
            proc = subprocess.run(argv, input=_json.dumps(manifest),
                                  capture_output=True, text=True, timeout=60.0)
        except FileNotFoundError as exc:
            raise RyaError("E_WORKER_START_FAILED", "'kubectl' is not on PATH.",
                           hint="The kubernetes driver applies manifests through "
                                "kubectl so it needs no client library.") from exc
        if proc.returncode != 0:
            raise RyaError("E_WORKER_START_FAILED",
                           f"kubectl apply failed: {(proc.stderr or '').strip()[:400]}")
        return WorkerHandle(id=name, driver=self.name, spec=spec,
                            native={"namespace": self.namespace, "kind": "Pod"})

    def stop(self, handle: WorkerHandle, *, timeout: float = 10.0) -> None:
        _run(["kubectl", "delete", "pod", handle.id, "-n", self.namespace,
              f"--grace-period={int(timeout)}"], check=False, timeout=timeout + 30)

    def list(self, key: Optional[str] = None) -> List[WorkerHandle]:
        out = _run(["kubectl", "get", "pods", "-n", self.namespace,
                    "-l", f"{LABEL_MANAGED.replace('.', '-')}=1",
                    "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\t'}"
                          "{.metadata.labels.rya-key}{'\\t'}"
                          "{.metadata.labels.rya-workspace}{'\\t'}"
                          "{.metadata.labels.rya-agent}{'\\t'}{.status.phase}{'\\n'}{end}"],
                   check=False)
        handles: List[WorkerHandle] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, pod_key, workspace, agent, phase = parts[:5]
            if phase not in ("Running", "Pending"):
                continue
            if key is not None and pod_key != key:
                continue
            version = pod_key.split(":")[-1]
            handles.append(WorkerHandle(
                id=name, driver=self.name,
                spec=WorkerSpec(workspace=workspace, agent=agent,
                                version_id=None if version == "local" else version),
                native={"namespace": self.namespace, "phase": phase}))
        return handles

    def verify_isolation(self) -> IsolationProbe:
        """Run the probe in a pod under the same RuntimeClass.

        The check §9 risk 8 asked for, and the reason it has to be a pod: whether a
        ``RuntimeClass`` named ``gvisor`` maps to `runsc` is a node configuration this
        process cannot read. A cluster with the RuntimeClass defined and no gVisor
        nodes accepts the pod and runs it on `runc`, and the manifest looks perfect.
        """
        if not self.runtime_class:
            self._probe = IsolationProbe(
                driver=self.name, declared=self.isolation, verified=False,
                detail=f"no RuntimeClass is configured, so pods run on the node's "
                       f"default runtime. Set {K8S_RUNTIME_CLASS_ENV}.")
            return self._probe
        image = self._require_image()
        argv = ["kubectl", "run", f"rya-probe-{os.urandom(3).hex()}",
                "-n", self.namespace, "--rm", "--attach", "--restart=Never",
                "--quiet", "--image", image,
                f"--overrides={{\"spec\":{{\"runtimeClassName\":\"{self.runtime_class}\"}}}}",
                "--command", "--", "/bin/sh", "-c", ISOLATION_PROBE_SCRIPT]
        try:
            out = _run(argv, timeout=180.0)
        except RyaError as exc:
            self._probe = IsolationProbe(
                driver=self.name, declared=self.isolation, verified=None,
                detail=f"the probe pod could not be run: {exc.message[:200]}")
            return self._probe
        self._probe = read_isolation_signals(out, driver=self.name,
                                             declared=self.isolation)
        return self._probe


# ---- resolution + the fail-closed check -------------------------------------

DRIVERS: Dict[str, Callable[..., ExecutionDriver]] = {
    "local": LocalDriver,
    "docker": DockerDriver,
    "kubernetes": KubernetesDriver,
}

# Named here rather than left to a KeyError so the refusal can say *when* each one
# arrives. An operator who sets RYA_EXECUTION_DRIVER=kubernetes today has made a
# reasonable request against a roadmap, not a typo, and the two failures deserve
# different messages.
PLANNED_DRIVERS = {
    "ecs": "optional, for shops already on ECS: microvm, provided by Fargate. §7 is "
           "explicit that Fargate is available, not depended on — task launch is tens "
           "of seconds against a 2000 ms target, and it is one cloud",
}


def resolve_driver(env: Optional[Mapping[str, str]] = None, **kwargs) -> ExecutionDriver:
    """The declared execution driver, defaulting to `local`.

    Same shape as ``open_store`` and ``resolve_bundle_store``: read one
    environment variable, fall back to the zero-config local arm, and make the
    default the one that works on a laptop with nothing installed.

    ``env`` is threaded into the driver as well as used to pick it. That sounds
    obvious and it was not: while `local` was the only driver, nothing a driver read
    came from the environment, so passing an explicit ``env`` selected the driver and
    then let it configure itself from ``os.environ``. With `docker` and `kubernetes`
    that gap decides whether a driver claims to be sandboxed — a caller resolving with
    a declared environment (D8) would have got an ambiently-configured driver and no
    indication of it.
    """
    env = env if env is not None else os.environ
    name = (env.get(DRIVER_ENV) or DEFAULT_DRIVER).strip().lower()
    kwargs.setdefault("env", env)
    factory = DRIVERS.get(name)
    if factory is None:
        planned = PLANNED_DRIVERS.get(name)
        raise RyaError(
            "E_DRIVER_UNKNOWN",
            f"No execution driver named '{name}'."
            + (f" That driver {planned}." if planned else ""),
            hint=f"Available now: {', '.join(sorted(DRIVERS))}. "
                 f"Set {DRIVER_ENV} to one of those.",
        )
    return factory(**kwargs)


def require_isolation_for_tenancy(driver: ExecutionDriver, *,
                                  untrusted: Optional[bool] = None,
                                  env: Optional[Mapping[str, str]] = None) -> ExecutionDriver:
    """Refuse to start when the declared posture outruns the driver (D26).

    A **startup** failure, not a warning. §9 risk 8 is specific about the failure
    mode this prevents: isolation becoming a driver property means "is Rya safe
    for untrusted tenants" stops having one answer, and the wrong answer is not
    reached by misreading the documentation — it is reached by not consulting it.
    A log line at boot is not consulted either.

    Returns the driver so a caller can write
    ``driver = require_isolation_for_tenancy(resolve_driver())`` and have no path
    that skips the check by forgetting to look at the result.
    """
    if untrusted is None:
        untrusted = untrusted_tenancy_enabled(env)
    if not untrusted:
        return driver
    if isolation_rank(driver.isolation) >= isolation_rank(UNTRUSTED_MIN_ISOLATION):
        return driver
    raise RyaError(
        "E_ISOLATION_INSUFFICIENT",
        f"{UNTRUSTED_ENV} is set, but the '{driver.name}' execution driver declares "
        f"isolation '{driver.isolation}' and untrusted tenancy requires at least "
        f"'{UNTRUSTED_MIN_ISOLATION}'.",
        hint="Untrusted tenant code needs a boundary that contains a kernel escape "
             "(D17/D23); namespaces and cgroups contain a buggy tenant, not a "
             f"hostile one. Either unset {UNTRUSTED_ENV} — and do not advertise "
             f"hostile-tenant isolation — or run a driver that provides it: "
             f"{DRIVER_ENV}=docker with {RUNTIME_ENV}={GVISOR_RUNTIME}, or "
             f"{DRIVER_ENV}=kubernetes with a gVisor RuntimeClass.",
    )


# ---- the launch gate --------------------------------------------------------

@dataclass(frozen=True)
class PostureReport:
    """The four conditions the untrusted posture requires, and their state.

    Returned rather than raised so an operator tool can *show* the gate — `rya posture`
    prints this — and so the refusal message can name every unmet condition at once.
    Being told about one missing piece at a time, four deploys in a row, is how a
    launch checklist gets abandoned.

    **The fourth condition is Phase 5's, and it exists because of a Phase 4 gap.** D18
    needs the credentials to be somewhere the tenant is not; D32 is the decision about
    *where*, and until a driver can launch that arrangement the other three conditions
    can all be satisfied by a deployment that does not work. See
    :func:`topology_supported`.
    """

    untrusted: bool
    isolation_ok: bool
    isolation_detail: str
    broker_ok: bool
    broker_detail: str
    egress_ok: bool
    egress_detail: str
    probe: Optional[IsolationProbe] = None
    topology_ok: bool = True
    topology_detail: str = ""

    @property
    def unmet(self) -> List[str]:
        out = []
        if not self.isolation_ok:
            out.append(f"isolation (D23): {self.isolation_detail}")
        if not self.broker_ok:
            out.append(f"credential mediation (D18): {self.broker_detail}")
        if not self.egress_ok:
            out.append(f"network egress (D24): {self.egress_detail}")
        if not self.topology_ok:
            out.append(f"broker topology (D32): {self.topology_detail}")
        return out

    @property
    def ok(self) -> bool:
        return not self.unmet

    def describe(self) -> dict:
        return {"untrusted": self.untrusted, "ok": self.ok, "unmet": self.unmet,
                "isolation": {"ok": self.isolation_ok, "detail": self.isolation_detail},
                "broker": {"ok": self.broker_ok, "detail": self.broker_detail},
                "egress": {"ok": self.egress_ok, "detail": self.egress_detail},
                "topology": {"ok": self.topology_ok, "detail": self.topology_detail},
                "probe": self.probe.describe() if self.probe else None}


def topology_supported(driver: "ExecutionDriver") -> tuple:
    """Whether this driver can launch a working D32 arrangement. ``(ok, detail)``.

    Three answers, and the middle one is the reason this function exists.

    * ``pair`` — the driver renders a credentialed claimer container beside a
      credential-free sandbox container sharing :data:`SOCKET_DIR`. This is what D32
      calls *good*: the boundary between the tenant's process and the platform's
      credentials is a container boundary with a different uid and no shared PID
      namespace, rather than a process boundary inside one container.
    * ``claimer`` — the driver launches the credentialed half and lets it spawn
      templates as children. D32's *weak* form. Accepted here because it is a real
      boundary and it is what `local` does, and refused by the *isolation* condition
      instead, which is the honest place for "a process boundary is not a sandbox".
    * ``sandbox`` — the driver launches only the tenant half. Refused: a claimer with
      no DSN claims nothing, so a deployment on it starts, looks healthy and serves
      nothing. This was every container driver until Phase 6.
    """
    unit = getattr(driver, "launched_unit", UNIT_CLAIMER)
    if unit == UNIT_PAIR:
        return True, (f"the '{driver.name}' driver launches the D32 pair — a "
                      "credentialed claimer beside a credential-free sandbox running "
                      f"the template host, sharing {SOCKET_DIR} — so the credential "
                      "boundary is a container boundary")
    if unit == UNIT_CLAIMER:
        return True, (f"the '{driver.name}' driver launches the claimer, so the "
                      "credential boundary is the process boundary between it and the "
                      "template it spawns")
    return False, (
        f"the '{driver.name}' driver launches a credential-free sandbox, which cannot "
        "also be the claimer — a claimer with no DSN claims nothing. Serving untrusted "
        "tenants on it needs the split D32 describes: a claimer container beside a "
        f"sandbox container running `rya template-host`, sharing {SOCKET_DIR}. A driver "
        f"that renders both declares `launched_unit = {UNIT_PAIR!r}`")


def check_untrusted_posture(driver: ExecutionDriver, *,
                            untrusted: Optional[bool] = None,
                            env: Optional[Mapping[str, str]] = None,
                            verify: bool = False) -> PostureReport:
    """Evaluate all three conditions. Never raises; :func:`require_untrusted_posture` does.

    ``verify=False`` by default because probing costs a container start, and the
    supervisor evaluates this on a path where that would be paid per tick. The
    *launch gate* passes ``verify=True``, which is the one place the cost is worth it
    — and `rya doctor posture` is how an operator pays it deliberately.
    """
    env = env if env is not None else os.environ
    if untrusted is None:
        untrusted = untrusted_tenancy_enabled(env)

    probe = driver.verify_isolation() if verify else None
    level = probe.effective if probe is not None else driver.isolation
    isolation_ok = isolation_rank(level) >= isolation_rank(UNTRUSTED_MIN_ISOLATION)
    detail = f"the '{driver.name}' driver provides '{level}'"
    if probe is not None and probe.verified is None:
        # Unverifiable is a REFUSAL here, matching `isolation_rank`'s treatment of an
        # unknown level: the safe direction is the default one. An operator who cannot
        # probe their cluster does not have a verified sandbox, whatever the manifest
        # says.
        isolation_ok = False
        detail = f"the isolation probe was inconclusive — {probe.detail}"
    elif probe is not None and probe.verified is False:
        detail = f"the isolation probe REFUTED the declaration — {probe.detail}"

    from ..broker.protocol import BROKER_ENV, broker_enabled
    from ..egress import MODE_ENV, MODE_NONE

    broker_ok = broker_enabled(env)
    egress_mode = (env.get(MODE_ENV) or MODE_NONE).strip().lower()
    egress_ok = egress_mode != MODE_NONE
    topology_ok, topology_detail = topology_supported(driver)
    return PostureReport(
        topology_ok=topology_ok, topology_detail=topology_detail,
        untrusted=bool(untrusted),
        isolation_ok=isolation_ok, isolation_detail=detail,
        broker_ok=broker_ok,
        broker_detail=("tenant processes are mediated" if broker_ok else
                       f"{BROKER_ENV} is not set, so a tenant process would hold the "
                       "database credential, the seal key and the provider key"),
        egress_ok=egress_ok,
        egress_detail=("the substrate restricts egress" if egress_ok else
                       f"{MODE_ENV} is '{egress_mode}', so nothing at the network "
                       "layer stops tenant code reaching any host it likes"),
        probe=probe)


def require_untrusted_posture(driver: ExecutionDriver, *,
                              untrusted: Optional[bool] = None,
                              env: Optional[Mapping[str, str]] = None,
                              verify: bool = True) -> ExecutionDriver:
    """The launch gate. Refuses unless D18, D23, D24 and D32 are all in force.

    Supersedes :func:`require_isolation_for_tenancy`, which checked the only one of
    the three that existed in Phase 3 and is kept as the narrower question some
    callers legitimately ask.

    **One check over three conditions, on purpose.** MULTITENANT_PLAN §6 opens with
    "half a security boundary is not a security boundary", and three independent
    warnings would be three independent things to miss — the exact shape of §9 risk
    8's failure, where the wrong answer is reached by not consulting the
    documentation rather than by misreading it. Refusing to start is consulted.

    Returns the driver so a caller can write
    ``driver = require_untrusted_posture(resolve_driver())`` and have no path that
    skips the check by forgetting to look at the result.
    """
    report = check_untrusted_posture(driver, untrusted=untrusted, env=env,
                                     verify=verify)
    if not report.untrusted or report.ok:
        return driver
    unmet = "\n  - ".join(report.unmet)
    raise RyaError(
        "E_ISOLATION_INSUFFICIENT",
        f"{UNTRUSTED_ENV} is set and this deployment does not meet the untrusted "
        f"posture. Unmet:\n  - {unmet}",
        hint="Untrusted tenancy requires all four of a sandbox that contains a kernel "
             "escape (D23), a tenant process holding no credentials (D18), egress "
             "enforced by the network (D24), and a driver that can launch the broker "
             "outside the tenant's sandbox (D32). Any one of them missing makes the "
             f"others insufficient. Unset {UNTRUSTED_ENV} to run the trusted posture — "
             "which is supported and is what every self-host is — and do not advertise "
             "hostile-tenant isolation while doing so.",
    )
