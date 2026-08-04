"""Claimer scope — how much one claimer serves, and in what order (D27, #19-8b).

D27 decided the *mechanism* (fork per run from a hash-keyed warm pool) and shipped
the *scope* narrow: one claimer per ``(workspace, agent, version)``, exactly what
``rya worker`` has always been. It also said, in as many words, that widening the
scope to per-tenant would then be **configuration** rather than a rewrite. This
module is that configuration, and writing it is what tests the claim.

Two scopes::

    version   one claimer per (workspace, agent, version)   key `ws:agent:vid`
    tenant    one claimer per workspace                     key `ws:*:*`

**What actually changes at the wide scope.** Not the execution model — a run still
executes in a fork of an interpreter that imported exactly one bundle (D3 holds
verbatim). What changes is that the *claimer* no longer knows at startup which
bundle that will be, so three things it used to get for free have to be built:

1. **Preflight before claiming.** The narrow scope imported the bundle at startup,
   so a handler-set hole was a startup failure. The wide scope cannot, so the
   claimer *peeks* — a read-only look at the queue — resolves the version of the
   work it is about to take, warms that version's template (which is the import,
   and therefore the preflight), and only then forks a child to claim. The
   guarantee is preserved by ordering, not by luck. See :func:`peek`.
2. **Fairness inside a tenant.** ``concurrency_key`` was designed so one workspace
   cannot starve another; inside one claimer the same question reappears one level
   down, between the agents of a single tenant. See :class:`FairOrder`.
3. **Routing by group.** An unattributed item — one whose metadata names no agent —
   is claimable by *any* worker at the narrow scope and by *no* fork at the wide
   one, because at the wide scope there is no single handler set to run it against.
   Refusing it is the same call :meth:`Supervisor.plan` already makes for the same
   reason (D22: running one agent's item against another's handler is a cross-agent
   execution path, not a mix-up).

**One grouping implementation, two readers.** The supervisor asks "how deep is each
key, so how many workers do I want" and the claimer asks "which group do I serve
next"; both are the same ``GROUP BY`` over queue metadata, and the version and the
agent live in that metadata rather than in columns (D12/D22 put them there so
D14's SDK-free surface stayed unchanged). Two implementations of it would be two
answers, and the one the supervisor scales on and the one the claimer serves have
to agree or the fleet oscillates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..errors import RyaError

log = logging.getLogger("rya.execution.scope")

# One claimer per (workspace, agent, version) — D27's "narrow first", and still the
# default. Widening is opt-in because it gives up two things (see the module
# docstring) and a deployment should say so on purpose.
SCOPE_VERSION = "version"
# One claimer per workspace. Sandbox count becomes proportional to active tenants
# instead of to the N x M x V product, which is Phase 5's whole property.
SCOPE_TENANT = "tenant"
SCOPES = (SCOPE_VERSION, SCOPE_TENANT)

SCOPE_ENV = "RYA_CLAIMER_SCOPE"

# The wildcard a tenant-scoped key uses in the agent and version positions. A
# literal rather than an empty segment because `ws::` is unreadable in a log line
# and indistinguishable from a key built from empty strings by accident.
ANY = "*"

# The version segment for a working-tree (unpinned) claimer. Unchanged from
# `WorkerKey.concurrency_key`, which is the point: a version-scoped key must be
# spelled byte-identically before and after this module existed, or every
# registration in a running fleet stops matching what the supervisor launched.
LOCAL = "local"


def resolve_scope(value: Optional[str] = None, env=None) -> str:
    """The configured scope, refusing anything unrecognised.

    An unknown scope is an error rather than a fallback to the narrow default, for
    the reason ``quotas.py`` gives about a mistyped limit: a deployment that set
    ``RYA_CLAIMER_SCOPE=per-tenant`` and silently got the narrow scope would be
    paying the N x M x V sandbox bill while believing it had stopped.
    """
    import os

    if value is None:
        source = env if env is not None else os.environ
        value = (source.get(SCOPE_ENV) or "").strip() or SCOPE_VERSION
    value = value.strip().lower()
    if value not in SCOPES:
        raise RyaError(
            "E_SCOPE_UNKNOWN",
            f"Unknown claimer scope '{value}'.",
            hint=f"Valid scopes: {', '.join(SCOPES)}. '{SCOPE_VERSION}' is one claimer "
                 f"per (workspace, agent, version) — what a worker has always been. "
                 f"'{SCOPE_TENANT}' is one claimer per workspace, forking whichever "
                 f"version each item is pinned to (D27/#19-8b).",
        )
    return value


def scope_key(workspace: str, agent: Optional[str] = None,
              version_id: Optional[str] = None, *, scope: str = SCOPE_VERSION) -> str:
    """The concurrency key for one claimer at ``scope``.

    The single source of this string. ``WorkerKey.concurrency_key`` (what a running
    worker registers) and ``WorkerSpec.key`` (what the supervisor launched) both
    call it, because the supervisor compares those two and a drift between them
    makes the fleet read as permanently understaffed — it then starts a worker
    every tick, forever. That failure mode is already recorded as a gotcha in this
    package's AGENTS.md; routing both through one function is how it stops being a
    thing to remember.
    """
    if scope == SCOPE_TENANT:
        return f"{workspace}:{ANY}:{ANY}"
    return f"{workspace}:{agent or ''}:{version_id or LOCAL}"


def parse_key(key: str) -> dict:
    """``scope_key``'s inverse: ``{scope, workspace, agent, versionId}``.

    ``agent`` and ``versionId`` come back ``None`` at tenant scope — genuinely
    absent rather than empty, because "this claimer serves every agent" and "this
    claimer serves the agent named by the empty string" are different statements
    and the second one is a bug.
    """
    parts = key.split(":", 2)
    if len(parts) != 3:
        raise RyaError("E_VALIDATION", f"'{key}' is not a claimer key.",
                       hint="Keys are `workspace:agent:version` or `workspace:*:*`.")
    workspace, agent, version = parts
    if agent == ANY and version == ANY:
        return {"scope": SCOPE_TENANT, "workspace": workspace, "agent": None,
                "versionId": None}
    return {"scope": SCOPE_VERSION, "workspace": workspace, "agent": agent or None,
            "versionId": None if version == LOCAL else version}


@dataclass(frozen=True)
class Claimable:
    """Pending work for one ``(workspace, agent, version)`` group.

    The supervisor's scheduling unit and the claimer's dispatch unit are the same
    tuple at both scopes, which is what lets one grouping serve both. At the narrow
    scope a claimer *is* one of these; at the wide scope it serves many.
    """

    workspace: str
    agent: str
    version_id: Optional[str] = None
    depth: int = 0

    @property
    def key(self) -> str:
        return scope_key(self.workspace, self.agent, self.version_id)

    @property
    def attributed(self) -> bool:
        """Whether this group names an agent, and is therefore dispatchable."""
        return bool(self.agent)


def peek(store, *, workspace: str = "default",
         agents: Optional[List[str]] = None) -> List[Claimable]:
    """Claimable depth per group. A read, never a claim.

    Reads ``chat-turn`` and ``approval-resume`` from the queue plus the `jobs`
    primitive's due rows — exactly what a Rya worker would claim, for the reason
    ``Worker.queue_depth`` gives at length: the queue table serves two product
    surfaces (D14), so counting a foreign ``/queue/*`` job here would make every
    group permanently busy, nothing would ever scale to zero, and every start
    decision would be for a group that already has a worker.

    Grouped in Python rather than in SQL. The agent and the version live in each
    job's ``metadata``, so grouping on them means reading the JSON either way, and
    doing it here keeps ``FileStore`` and ``PostgresStore`` on one implementation.
    This is the per-tick cost #19 predicted for the narrow scope; at the wide scope
    the *supervisor* collapses to one number per tenant and this cost moves into the
    claimer, where it replaces a startup import.

    An **unattributed** item (no ``agent`` in its metadata) is reported under
    ``agent=""`` rather than dropped, so a caller can see it. Both callers then
    refuse to route it, for different but agreeing reasons — the supervisor has no
    key to start, the claimer has no handler set to fork.
    """
    from ..queue import agent_of, version_of
    from ..turns import RESUME_JOB

    counts: Dict[tuple, int] = {}

    def bump(agent: Optional[str], version_id: Optional[str]) -> None:
        counts[(agent or "", version_id)] = counts.get((agent or "", version_id), 0) + 1

    listing = getattr(store, "queue_list", None)
    if listing is not None:
        for kind in ("chat-turn", RESUME_JOB):
            for job in listing("pending", kind) or []:
                bump(agent_of(job), version_of(job))

    # The `jobs` primitive. Its rows carry an agent since Phase 3 (D22 reaching the
    # second surface); rows written before that have none and land in the
    # unattributed bucket, which is the same thing `claim_due_job` does with them.
    jobs = getattr(store, "list_jobs", None)
    if jobs is not None:
        from ..store import now_iso

        now = now_iso()
        for job in jobs("pending") or []:
            if (job.get("runAt") or "") <= now:
                bump(job.get("agent"), None)

    out = [Claimable(workspace=workspace, agent=agent, version_id=version_id, depth=n)
           for (agent, version_id), n in sorted(counts.items(), key=lambda kv: kv[0])]
    if agents is not None:
        out = [c for c in out if not c.agent or c.agent in agents]
    return out


def resolve_version(store, group: Claimable, *, environment: Optional[str]) -> Optional[str]:
    """The version that should serve ``group``, resolving an unpinned one.

    Turns and resumes carry a pin (D12), so most groups arrive complete. Two things
    do not: the `jobs` primitive, whose rows record no version, and anything
    enqueued while nothing was promoted. For those, "new work goes to the promoted
    version" (§9) is the answer, so this reads the environment pointer — the same
    question the api asks to pin a run in the first place.

    Not pinning a background job at enqueue time is deliberate and not an oversight:
    unlike an approval resume, a job has no journal to replay, so there is no version
    it *must* run on, and holding it back for a version that has since been
    superseded would be the strange choice.

    With no pointer the answer stays ``None``, which is the working-tree mode — a
    claimer needs a mounted project to serve it.
    """
    if group.version_id is not None or not environment or not group.agent:
        return group.version_id
    from .. import deployments

    try:
        current = deployments.current_version(store, environment, group.agent)
    except Exception:  # noqa: BLE001 - a missing pointer is not a scheduling error
        return None
    return (current or {}).get("id")


class FairOrder:
    """Round-robin across groups, so depth cannot buy priority.

    The problem the wide scope creates. ``concurrency_key`` exists because "one
    workspace must not starve another" (§6), and it is applied *between* claimers.
    Put five of a tenant's agents behind one claimer and the same question appears
    inside it: ``support`` with 40 queued turns and ``billing`` with 1 share a
    dispatch budget, and serving strictly by depth means ``billing`` waits for all
    40. At the narrow scope this could not happen — one claimer, one agent — which
    is why it is new work rather than a bug that was always there.

    Deliberately **equal dispatches, not weighted by depth.** A deficit or
    weighted-fair scheme needs a per-item service-time estimate, and the platform
    does not have one — turn durations span a mocked reply and a ten-minute tool
    loop, which is the same reason ``SupervisorPolicy.backlog_per_worker`` is a
    queue-length heuristic. Equal turns is the strongest thing that can be said
    honestly, and it is enough for the property being defended: a sibling with one
    item waits for one dispatch, not for the backlog.

    Throughput is not lost, only redistributed: a deep group still gets every
    dispatch nobody else wants, because a group with nothing pending is not in the
    candidate list at all. And the supervisor's answer to a genuine capacity
    shortfall is unchanged — it starts another claimer.
    """

    def __init__(self) -> None:
        # group key -> dispatches served. Monotonic rather than reset per tick: a
        # tick is a few forks, and resetting would make the order restart from the
        # same place every time, which is round-robin in the small and
        # first-group-wins in the large.
        self._served: Dict[str, int] = {}

    def order(self, groups: List[Claimable]) -> List[Claimable]:
        """``groups``, least-served first. Ties keep ``peek``'s stable order.

        The index is in the sort key rather than relying on sort stability alone,
        because "stable" here has to survive someone rewriting this as a heap later.
        """
        ranked = sorted(enumerate(groups),
                        key=lambda pair: (self._served.get(pair[1].key, 0), pair[0]))
        return [group for _, group in ranked]

    def served(self, group: Claimable, n: int = 1) -> None:
        self._served[group.key] = self._served.get(group.key, 0) + n

    def describe(self) -> dict:
        return dict(self._served)
