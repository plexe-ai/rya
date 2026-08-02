"""The execution plane's scheduling half (D25/D26/D27).

Three pieces, and the split between them is the point:

- ``drivers`` — **how** a worker gets launched. Pluggable per substrate (`local`
  now; `docker`, `kubernetes`, `ecs` with #14), and each driver declares what it
  isolates so untrusted tenancy on a driver that cannot back it fails at startup.
- ``supervisor`` — **what** to launch and how many. One implementation, platform
  code, no branching on substrate. This is the half that was entirely missing:
  every worker in the tree until now was started by a human, a compose file or an
  ECS ``DesiredCount``, so scale-to-zero was one-way.
- ``pool`` — **where the run happens** once a worker is up: a fork of a warm
  interpreter keyed by bundle hash, so the claimer holds no tenant import.
- ``host`` — **which container that fork happens in** (D32): a credential-free
  template host inside the sandbox, so the claimer no longer has to be the tenant
  process's parent. ``pool`` is unchanged by it, which was the test of whether D27
  keyed the right thing.

``worker.py`` is the other half of the plane and is deliberately not in here: it
is what gets scheduled, not what schedules.
"""

from .drivers import (  # noqa: F401
    BROKER_SOCKET_PATH,
    DRIVERS,
    HOST_SOCKET_PATH,
    ISOLATION_MICROVM,
    ISOLATION_NONE,
    ISOLATION_SANDBOXED,
    ISOLATION_SHARED_KERNEL,
    SOCKET_DIR,
    UNIT_CLAIMER,
    UNIT_PAIR,
    UNIT_SANDBOX,
    UNTRUSTED_ENV,
    UNTRUSTED_MIN_ISOLATION,
    DockerDriver,
    ExecutionDriver,
    IsolationProbe,
    KubernetesDriver,
    LocalDriver,
    PostureReport,
    WorkerHandle,
    WorkerSpec,
    check_untrusted_posture,
    isolation_rank,
    read_isolation_signals,
    require_isolation_for_tenancy,
    require_untrusted_posture,
    resolve_driver,
    topology_supported,
    untrusted_tenancy_enabled,
)
from .host import (  # noqa: F401
    HOST_SOCKET_ENV,
    HOST_TOKEN_ENV,
    HostedTemplate,
    HostedTemplateProbe,
    TemplateHost,
    hosted_enabled,
)
from .supervisor import (  # noqa: F401
    Supervisor,
    SupervisorPolicy,
    claimable_by_key,
)

__all__ = [
    "DRIVERS", "ExecutionDriver", "LocalDriver", "DockerDriver", "KubernetesDriver",
    "WorkerHandle", "WorkerSpec", "IsolationProbe", "PostureReport",
    "ISOLATION_NONE", "ISOLATION_SHARED_KERNEL", "ISOLATION_SANDBOXED",
    "ISOLATION_MICROVM", "UNTRUSTED_ENV", "UNTRUSTED_MIN_ISOLATION",
    "isolation_rank", "read_isolation_signals", "resolve_driver",
    # D32: the topology vocabulary. `topology_supported` is the fourth launch-gate
    # condition and `UNIT_PAIR` is what satisfies it.
    "topology_supported", "UNIT_CLAIMER", "UNIT_SANDBOX", "UNIT_PAIR",
    "SOCKET_DIR", "BROKER_SOCKET_PATH", "HOST_SOCKET_PATH",
    "TemplateHost", "HostedTemplate", "HostedTemplateProbe", "hosted_enabled",
    "HOST_SOCKET_ENV", "HOST_TOKEN_ENV",
    # Two checks, and the difference matters: `require_isolation_for_tenancy` asks the
    # narrow question (does the driver isolate?), `require_untrusted_posture` is the
    # LAUNCH GATE and asks all three of D18/D23/D24. New callers want the second.
    "require_isolation_for_tenancy", "require_untrusted_posture",
    "check_untrusted_posture", "untrusted_tenancy_enabled",
    "Supervisor", "SupervisorPolicy", "claimable_by_key",
]
