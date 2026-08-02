"""What to start, how many, and when to stop — the scheduling policy (D25).

This is the half of §6 that was never built. Registration, handler advertisement,
``preflight``, heartbeat, ``--idle-exit`` and ``COLD_START_TARGET_MS`` all exist;
what did not exist was anything that *decides*. Every worker in the tree was
started by a human, a compose file or an ECS ``DesiredCount``, which made
scale-to-zero **one-way**: a worker could idle out and then that key was simply
unserved until someone noticed.

**One implementation, no substrate branching.** Policy lives here; launching lives
behind :class:`~rya.execution.drivers.ExecutionDriver`. Nothing in this module
knows whether a worker is a subprocess, a container or a Fargate task, which is
the property that makes "the same supervisor passes its tests against `local` and
`docker`" a statement about the code rather than about discipline.

**The input signal.** Claimable depth per key, from the queue, plus the worker
registry. Both deliberately reuse the reasoning already in ``worker.py``:
:func:`claimable_by_key` counts only what a worker for that key would actually
claim (``Worker.queue_depth``'s point — a foreign ``/queue/*`` job must not read as
busy), and liveness comes from ``store.worker_list``, which since Phase 3 demotes a
worker that stopped heartbeating instead of reporting a SIGKILLed process as
`alive` forever. A scheduler on that signal would have replaced nothing and reaped
nothing, which is why the liveness fix was a prerequisite rather than a tidy-up.

**One decision function, then effects.** :meth:`Supervisor.plan` is pure: registry
and depth in, a list of actions out. :meth:`Supervisor.apply` performs them. That
split is what lets the interesting policy questions — does a key with two live
versions get two workers, does `maxWorkers` refuse the third — be tested without
launching a process, and it is the shape §9 risk 7 asks for, since a scheduler is
where distributed-systems complexity re-enters a design D1 removed it from.

**What this deliberately does not do.** It does not schedule across workspaces by
itself: a :class:`Supervisor` holds one workspace's store because that is the
isolation boundary (D29 keeps it at ``workspace_id``), and fanning out is
:func:`supervise_workspaces`, which enumerates with the admin DSN and then reads
each tenant through its own weak-role store. Nor does it decide fairness *within* a
tenant — that lives in the claimer since Phase 5 (D33,
:class:`rya.execution.scope.FairOrder`), because the claimer is the thing with a
dispatch to hand out.

**Two Phase 5 additions, and both are about "how many of a thing".** It plans a
different *shape* of key at the wide claimer scope — one per workspace rather than one
per agent-version (:meth:`Supervisor._tenant_targets`), which is where the N×M×V
collapse becomes an actual number. And it holds a **lease** (D34), because the
`kubernetes` driver made "two supervisors" the default way to deploy one and two
supervisors do not merely duplicate work: :meth:`Supervisor.observe` reconciles the
registry against the *driver's* inventory, and a second supervisor's inventory is
empty, so it sees a fleet it did not launch and starts its own anyway.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..errors import RyaError
from .drivers import ExecutionDriver, WorkerHandle, WorkerSpec
from .scope import (SCOPE_TENANT, Claimable, parse_key, peek, resolve_scope,
                    resolve_version, scope_key)

log = logging.getLogger("rya.execution.supervisor")

# Actions a plan can contain. Strings rather than an enum because they are logged,
# emitted by `rya supervisor --plan --json` and asserted on in tests — three places
# where a stable literal is worth more than a type.
START = "start"
STOP = "stop"
REAP = "reap"

DEFAULT_TICK_SECONDS = 5.0


@dataclass
class Action:
    kind: str
    key: str
    reason: str
    spec: Optional[WorkerSpec] = None
    handle: Optional[WorkerHandle] = None
    worker_id: Optional[str] = None

    def describe(self) -> dict:
        return {"action": self.kind, "key": self.key, "reason": self.reason,
                "workerId": self.worker_id,
                "spec": self.spec.describe() if self.spec else None}


@dataclass
class SupervisorPolicy:
    """The numbers. Every one of them is a policy choice, so none is buried.

    ``backlog_per_worker`` is the only one with a non-obvious default. 5 means a
    key with 40 queued turns asks for 8 workers, capped by ``max_replicas_per_key``
    and then by ``maxWorkers``. It is a *queue-length* heuristic rather than a
    latency target because the platform has no per-item service-time estimate —
    turn durations span a mocked reply and a ten-minute tool loop — and inventing
    one would be a guess with a decimal point on it.
    """

    backlog_per_worker: int = 5
    max_replicas_per_key: int = 4
    idle_exit_seconds: float = 60.0
    poll_seconds: float = 2.0
    concurrency: int = 1
    # Which environments to keep one warm worker for, regardless of depth. Empty by
    # default and that is not timidity: pre-warming every promoted agent would
    # defeat the scale-to-zero this phase just made two-way, and §6 names idle cost
    # as *the* constraint. An operator who wants the first prod request to skip a
    # cold start opts in per environment and pays for it knowingly.
    prewarm_environments: tuple = ()
    # Unpinned (working-tree) keys are only startable where a project is mounted.
    # Without one, a launched worker has nothing to load, so scheduling it would
    # produce a process that fails preflight every few seconds forever.
    allow_unpinned: bool = True
    # Whether this supervisor must hold a lease before it applies a plan. On by
    # default since Phase 5: the failure it prevents (two supervisors, every replica
    # count doubled) is silent, and the cost of the check is one row.
    require_lease: bool = True
    lease_seconds: float = 30.0
    # D35/§9: how often the multi-workspace fan-out refreshes org verdicts. 0 disables
    # it. On by default — an org budget that nothing reconciles caps nothing, and the
    # trigger that named this gap ("Nobody runs `rya orgs reconcile`") is closed by the
    # scheduler existing rather than by documenting that one is needed.
    #
    # A supervisor tick rather than a fourth run mode, which was the third option on
    # the table. The supervisor already runs continuously, already holds the admin DSN
    # in `--all-workspaces` mode, and already has the lease that stops two of them
    # doing everything twice. A `rya reconciler` process would be a fourth thing to
    # deploy and monitor for one query a minute.
    reconcile_orgs_seconds: float = 300.0


def claimable_by_key(store, *, workspace: str = "default",
                     agents: Optional[List[str]] = None) -> List[Claimable]:
    """Claimable depth, grouped by ``(workspace, agent, version)``.

    :func:`rya.execution.scope.peek` is the implementation, and it is shared with the
    *claimer* rather than duplicated for it. Phase 5 made that necessary: at the wide
    claimer scope one claimer serves many groups and has to pick one, which is the
    same ``GROUP BY`` over queue metadata this function does. Two implementations
    would be two answers — and the number the supervisor scales on and the group the
    claimer serves have to agree, or replicas oscillate against a claimer that is
    working on something else.

    Kept as a name here because it is what "the supervisor's input signal" is called
    everywhere in the docs, and because the *question* is the supervisor's even when
    the arithmetic is not.
    """
    return peek(store, workspace=workspace, agents=agents)


class Supervisor:
    """Keeps one workspace's fleet matched to its work.

    ``store`` is that workspace's handle — the same scoping every other execution
    plane component uses, so a supervisor cannot read another tenant's depth even
    by accident. ``driver`` is how it launches. ``project_root`` is only needed for
    working-tree keys and for the local driver's working directory.
    """

    def __init__(self, store, driver: ExecutionDriver, *,
                 workspace: str = "default",
                 environment: Optional[str] = None,
                 project_root: Optional[Path] = None,
                 policy: Optional[SupervisorPolicy] = None,
                 scope: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.driver = driver
        self.workspace = workspace
        self.environment = environment
        self.project_root = Path(project_root) if project_root else None
        self.policy = policy or SupervisorPolicy()
        # D27/#19-8b: which shape of key this supervisor schedules. It is the same
        # decision the claimer makes and it has to be the same answer, because the
        # supervisor launches the claimer — a supervisor planning `ws:agent:vid` keys
        # in front of tenant-scoped claimers would see every one of its keys unserved
        # and start a worker per tick, forever.
        self.scope = resolve_scope(scope)
        self.clock = clock
        self.started: Dict[str, List[WorkerHandle]] = {}
        self.ticks = 0
        self.actions: List[dict] = []
        self.lease: Optional[dict] = None
        self.passive = False
        # The lease holder id. Per *process*, and it includes the host and pid so that
        # "who holds this" is answerable from the row rather than by correlating logs
        # — the whole point of the lease is that the other holder is somewhere else.
        from ..store import _new_id

        self.id = f"{socket.gethostname()}:{os.getpid()}:{_new_id('sup')}"

    # ---- observation ------------------------------------------------------
    def observe(self) -> dict:
        """What is running, what is lost, and what is waiting.

        Reconciles two views that can disagree, and neither is authoritative on its
        own. The **registry** is durable but lags: a worker appears in it only once
        it got far enough to talk to the database, so a process that is starting up
        looks absent. The **driver** knows what it launched but its inventory can be
        process-local (the `local` driver cannot enumerate orphaned children) or
        stale (a container the daemon has since reaped).

        Trusting the registry alone starts a duplicate for every key whose worker is
        still booting. Trusting the driver alone loses the fleet entirely across a
        supervisor restart. So a key counts as served if *either* view has it, which
        errs toward not starting a second worker — the cheaper mistake, since an
        idle worker exits on its own and a stampede does not.
        """
        listing = getattr(self.store, "worker_list", None)
        registered = list(listing() or []) if listing is not None else []
        alive: Dict[str, List[dict]] = {}
        lost: List[dict] = []
        for doc in registered:
            if doc.get("status") == "alive":
                alive.setdefault(doc.get("concurrencyKey") or "", []).append(doc)
            elif doc.get("status") == "lost":
                lost.append(doc)

        launched: Dict[str, List[WorkerHandle]] = {}
        try:
            for handle in self.driver.list():
                launched.setdefault(handle.key, []).append(handle)
        except Exception:  # a driver that cannot answer must not stop the tick
            log.warning("driver inventory failed", exc_info=True)
            launched = {k: list(v) for k, v in self.started.items()}

        return {"alive": alive, "lost": lost, "launched": launched,
                "claimable": claimable_by_key(self.store, workspace=self.workspace)}

    def _live_count(self, key: str, view: dict) -> int:
        return max(len(view["alive"].get(key, [])), len(view["launched"].get(key, [])))

    # ---- policy -----------------------------------------------------------
    def plan(self, view: Optional[dict] = None) -> List[Action]:
        """Decide, without doing. Pure with respect to ``view``.

        Reaps come first in the plan so they are applied first: a replacement that
        registers before the dead row is retired leaves two registrations for one
        key, and the second tick would then see a fleet it did not schedule.

        A lost worker's replacement needs no quota slot freed for it — see
        ``_worker_budget`` — because `lost` is already excluded from both the budget
        and `quotas`. That is a consequence of the liveness fix; on the old signal
        a crash-looping key would have read as an exhausted `maxWorkers` instead.
        """
        view = view if view is not None else self.observe()
        actions: List[Action] = []

        for doc in view["lost"]:
            actions.append(Action(kind=REAP, key=doc.get("concurrencyKey") or "",
                                  worker_id=doc.get("id"),
                                  reason=f"no heartbeat for "
                                         f"{int(doc.get('heartbeatAgeSeconds') or 0)}s"))

        budget = self._worker_budget(view)
        for item in self._targets(view):
            key, want, reason = item
            live = self._live_count(key, view)
            if want <= live:
                continue
            spec = self._spec_for(key, reason)
            if spec is None:
                continue
            for _ in range(want - live):
                if budget is not None and budget <= 0:
                    # Named rather than silently truncated: "the fleet is at its
                    # maxWorkers ceiling" and "there was nothing to start" produce
                    # the same empty plan otherwise, and only one of them is a
                    # reason to go buy capacity.
                    log.info("maxWorkers reached; not starting %s (%s)", key, reason)
                    break
                actions.append(Action(kind=START, key=key, reason=reason, spec=spec))
                if budget is not None:
                    budget -= 1
        return actions

    def _worker_budget(self, view: dict) -> Optional[int]:
        """How many more workers ``maxWorkers`` admits, or None for unlimited.

        §11.12's quota, enforced at **schedule** time. ``Worker.register`` already
        checks it, and that check stays for workers nobody scheduled — but refusing
        at registration means the launch already happened: a container was pulled
        and started so it could exit on a quota error. Deciding here costs nothing
        and is the difference between a cap and a receipt.

        A `lost` worker consumes no budget, and it needs no credit for the reap
        planned alongside it: ``view["alive"]`` holds only genuinely-alive workers
        because ``store.worker_list`` derives that, and ``quotas`` counts the same
        way. Before the liveness fix this arithmetic could not have been written —
        a crashed worker held a slot forever, so a key that crash-looped would have
        exhausted `maxWorkers` and the supervisor would have concluded, correctly
        from the data it had, that the tenant was out of capacity.
        """
        from ..quotas import resolve_quota

        try:
            limit = resolve_quota(self.store).max_workers
        except Exception:
            return None
        if limit is None:
            return None
        # A worker present in both views is one worker, so the total is summed with
        # the same per-key reconciliation `_live_count` uses rather than by adding
        # the two views together.
        keys = set(view["alive"]) | set(view["launched"])
        return max(0, limit - sum(self._live_count(k, view) for k in keys))

    def _targets(self, view: dict) -> List[tuple]:
        """``(key, wanted_replicas, reason)`` for every key worth serving."""
        if self.scope == SCOPE_TENANT:
            return self._tenant_targets(view)
        targets: Dict[str, tuple] = {}
        for item in view["claimable"]:
            if not item.agent:
                # Unattributed work. Real depth for whatever is already running,
                # but it names no agent, so there is no key to start. Logged at
                # debug on purpose: on a pre-Phase-3 database every `jobs` row is
                # in this bucket and a warning per tick would be noise.
                log.debug("%d unattributed item(s) cannot be scheduled to a key",
                          item.depth)
                continue
            key = self._resolve_key(item)
            want = min(self.policy.max_replicas_per_key,
                       max(1, -(-item.depth // max(1, self.policy.backlog_per_worker))))
            targets[key] = (key, want, f"depth {item.depth}")
        for key in self._prewarm_keys():
            targets.setdefault(key, (key, 1, "pre-warm"))
        return [targets[k] for k in sorted(targets)]

    def _tenant_targets(self, view: dict) -> List[tuple]:
        """One key for the whole tenant. The Phase 5 property, in six lines.

        §7.1's table, arithmetically: the narrow scope wants a replica count per
        ``(agent, version)`` and the wide scope wants one per *tenant*, computed from
        the tenant's total depth. Two things fall out of that and both are exit
        criteria rather than side effects:

        * **A rollout no longer doubles anything.** ``support/v7`` and ``support/v8``
          are two groups inside one key, so a promotion changes which templates are
          warm and not how many sandboxes exist.
        * **An approval resuming on a retired version needs no dedicated sandbox.**
          It is depth on the tenant's one key, and the claimer forks v7 for it.

        Unattributed depth counts here even though the claimer will refuse to run it,
        which is the opposite of the narrow scope's treatment. The reason is that at
        the narrow scope unattributed depth named no key to *start*; here the key
        exists regardless, and hiding the rows would leave a tenant with nothing but
        unroutable work looking idle while a queue built up. The claimer logs the
        refusal per tick and the supervisor's `claimable` list still shows the group.
        """
        depth = sum(item.depth for item in view["claimable"])
        key = scope_key(self.workspace, scope=SCOPE_TENANT)
        if depth:
            want = min(self.policy.max_replicas_per_key,
                       max(1, -(-depth // max(1, self.policy.backlog_per_worker))))
            return [(key, want, f"depth {depth}")]
        if self.policy.prewarm_environments and self._prewarm_keys():
            # One claimer, warming several versions inside itself. The narrow scope
            # pre-warmed by keeping a whole extra sandbox alive per key; this keeps
            # interpreters warm inside a sandbox the tenant already needs, which is
            # the same latency win at a fraction of the idle cost §6 names as *the*
            # constraint.
            return [(key, 1, "pre-warm")]
        return []

    def _resolve_key(self, item: Claimable) -> str:
        """Which key should serve this work — resolving an unpinned item's version.

        Turns and resumes carry a pin (D12), so their key is already complete. Two
        things do not: the `jobs` primitive, whose rows record no version, and any
        item enqueued while nothing was promoted. For those,
        :func:`rya.execution.scope.resolve_version` reads the environment pointer,
        and it is shared with the claimer for the same reason the grouping is: the
        version the supervisor schedules for and the version the claimer forks have
        to be the same one.

        With no pointer the key stays unpinned, which is the working-tree mode and
        needs a mounted project (see ``_spec_for``).
        """
        version_id = resolve_version(self.store, item, environment=self.environment)
        return scope_key(item.workspace, item.agent, version_id)

    def _prewarm_keys(self) -> List[str]:
        """One key per (agent, current version) in each pre-warmed environment.

        Reads the environment pointers, which is the same question `GET /agents`
        answers — an agent exists because a version or a pointer says so (D21). A
        pointer at a version this deployment cannot start is skipped rather than
        retried: a retired or deleted version is not a scheduling problem.
        """
        if not self.policy.prewarm_environments:
            return []
        env_list = getattr(self.store, "env_list", None)
        if env_list is None:
            return []
        out = []
        for row in env_list() or []:
            if row.get("name") not in self.policy.prewarm_environments:
                continue
            agent, version_id = row.get("agent"), row.get("currentVersionId")
            if agent and version_id:
                out.append(f"{self.workspace}:{agent}:{version_id}")
        return out

    def _spec_for(self, key: str, reason: str) -> Optional[WorkerSpec]:
        """Turn a key back into something launchable, refusing what cannot run."""
        try:
            parsed = parse_key(key)
        except RyaError:  # pragma: no cover - keys are built by this module
            return None
        workspace = parsed["workspace"]
        if parsed["scope"] == SCOPE_TENANT:
            # Nothing to validate. A tenant claimer names no version, so there is no
            # version record to check for existence or retirement — those checks moved
            # into the claimer, per group, where a retired version with a pinned
            # approval is a *legitimate* thing to fork rather than a reason to refuse.
            # That is the third exit criterion: an approval resuming on a retired
            # version needs no dedicated sandbox, and here it needs no dedicated
            # scheduling decision either.
            return WorkerSpec(workspace=workspace, agent="", version_id=None,
                              bundle_hash=None, environment=self.environment,
                              project_root=self.project_root,
                              idle_exit_seconds=self.policy.idle_exit_seconds,
                              poll_seconds=self.policy.poll_seconds,
                              concurrency=self.policy.concurrency, reason=reason,
                              scope=SCOPE_TENANT,
                              prewarm=tuple(self.policy.prewarm_environments))
        agent = parsed["agent"] or ""
        version_id = parsed["versionId"]
        if version_id is None and not (self.policy.allow_unpinned and self.project_root):
            log.info("not starting unpinned key %s: no project is mounted", key)
            return None
        if version_id is not None:
            record = None
            getter = getattr(self.store, "version_get", None)
            if getter is not None:
                record = getter(version_id)
            if record is None:
                log.warning("not starting %s: version %s does not exist", key, version_id)
                return None
            if record.get("state") == "retired":
                # D12 retains a version while runs are pinned to it, so a retired
                # version with queued work is a real state — and the right response
                # is to refuse rather than to start a worker that would raise
                # E_VERSION_RETIRED on every attempt.
                log.warning("not starting %s: version %s is retired", key, version_id)
                return None
        return WorkerSpec(workspace=workspace, agent=agent, version_id=version_id,
                          bundle_hash=None, environment=self.environment,
                          project_root=self.project_root,
                          idle_exit_seconds=self.policy.idle_exit_seconds,
                          poll_seconds=self.policy.poll_seconds,
                          concurrency=self.policy.concurrency, reason=reason)

    # ---- effects ----------------------------------------------------------
    def apply(self, actions: List[Action]) -> List[dict]:
        """Carry out a plan. One failed action never aborts the rest.

        A driver that cannot start one key must not stop the tick from reaping a
        dead worker or serving a different key — the failure modes here are
        per-key (a missing image, a bad bundle) far more often than global.
        """
        done: List[dict] = []
        for action in actions:
            record = action.describe()
            try:
                if action.kind == REAP:
                    self._reap(action)
                elif action.kind == START:
                    handle = self.driver.start(action.spec)  # type: ignore[arg-type]
                    self.started.setdefault(action.key, []).append(handle)
                    record["handleId"] = handle.id
                elif action.kind == STOP and action.handle is not None:
                    self.driver.stop(action.handle)
                record["ok"] = True
            except RyaError as exc:
                record["ok"] = False
                record["error"] = exc.message
                log.warning("%s %s failed: %s", action.kind, action.key, exc.message)
            except Exception as exc:
                record["ok"] = False
                record["error"] = str(exc)
                log.warning("%s %s failed", action.kind, action.key, exc_info=True)
            done.append(record)
        self.actions.extend(done)
        return done

    def _reap(self, action: Action) -> None:
        """Retire a lost worker's registration.

        Deliberately ``worker_deregister``, the same call a clean exit makes, with
        ``lost`` as the reason. A crash then ends up in the same *shape* as a
        graceful shutdown and is told apart by ``stopReason`` — which keeps
        `GET /workers`, the console badge and the `maxWorkers` count on one code
        path instead of two. Note the row is not deleted: "this key kept dying" is
        the history an operator needs most.
        """
        dereg = getattr(self.store, "worker_deregister", None)
        if dereg is not None and action.worker_id:
            dereg(action.worker_id, "lost")

    # ---- the singleton guard ----------------------------------------------
    @property
    def lease_name(self) -> str:
        return f"supervisor:{self.workspace}"

    def hold_lease(self) -> bool:
        """Whether this supervisor may act. Renews on every tick.

        **Why a supervisor needs one at all.** Open question 7 arrived with the
        `kubernetes` driver: the obvious way to run a supervisor there is a Deployment,
        a Deployment is scalable by default, and a second replica reads the same queue
        depth, computes the same target, sees the *other* replica's workers as the
        fleet — and then starts its own too, because ``observe`` reconciles the
        registry with the *driver's* inventory and a second supervisor's driver
        inventory is empty. So the fleet does not converge on 2N by accident; it
        doubles because each replica believes it is the only one.

        Per **workspace**, not per process, and that is deliberately useful: two
        supervisors over a hundred tenants split the fleet rather than one idling.

        Losing the lease is not an error. A supervisor that cannot take it keeps
        observing and planning and logs what it *would* have done — which is the
        useful behaviour for a standby, and makes a lease handover visible in the
        losing replica's logs rather than only in the winning one's.
        """
        if not self.policy.require_lease:
            return True
        acquire = getattr(self.store, "lease_acquire", None)
        if acquire is None:
            # A duck-typed store from before this existed. Acting is the compatible
            # answer: the alternative is that a third-party store silently stops
            # scheduling anything, which is a worse failure than the one being
            # prevented.
            return True
        try:
            self.lease = acquire(self.lease_name, self.id,
                                 self.policy.lease_seconds)
        except Exception:  # noqa: BLE001 - a lease read must not stop a fleet
            log.warning("lease acquisition failed; acting anyway", exc_info=True)
            return True
        return self.lease is not None

    def release_lease(self) -> None:
        """Hand the lease back on a clean exit, so a standby takes over immediately
        rather than after the TTL."""
        release = getattr(self.store, "lease_release", None)
        if release is None or not self.lease:
            return
        try:
            release(self.lease_name, self.id)
        except Exception:  # noqa: BLE001
            log.debug("lease release failed", exc_info=True)
        self.lease = None

    def tick(self) -> dict:
        """One observe → plan → apply cycle. The whole loop is this.

        The lease is taken *between* plan and apply rather than before observe, so a
        standby still produces a plan and still logs it. That is the whole value of
        the passive mode: an operator debugging "why is nothing scaling" can read the
        standby's log and see a correct plan going unapplied, which points at the
        lease instead of at the policy.
        """
        view = self.observe()
        actions = self.plan(view)
        held = self.hold_lease()
        self.passive = not held
        if not held:
            log.warning("another supervisor holds %s; not applying %d action(s)",
                        self.lease_name, len(actions))
            done: List[dict] = []
        else:
            done = self.apply(actions)
        self.ticks += 1
        out = {"tick": self.ticks, "actions": done,
               "claimable": [c.__dict__ for c in view["claimable"]],
               "alive": sum(len(v) for v in view["alive"].values()),
               "lost": len(view["lost"]), "scope": self.scope}
        if not held:
            out["passive"] = True
            out["withheld"] = [a.describe() for a in actions]
        return out

    def run(self, *, max_ticks: Optional[int] = None,
            tick_seconds: float = DEFAULT_TICK_SECONDS,
            on_tick: Optional[Callable[[dict], Any]] = None) -> dict:
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                out = self.tick()
                if on_tick is not None:
                    on_tick(out)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                time.sleep(tick_seconds)
        finally:
            # Released on the way out so a standby takes over on its next tick rather
            # than after the TTL. A crash skips this, which is what the TTL is for.
            self.release_lease()
            log.info("supervisor stopping after %d tick(s)", ticks)
        return {"ticks": ticks, "actions": self.actions, "passive": self.passive}


def supervise_workspaces(*, admin_dsn: str, driver: ExecutionDriver,
                         project_root: Optional[Path] = None,
                         environment: Optional[str] = None,
                         policy: Optional[SupervisorPolicy] = None,
                         workspaces: Optional[List[str]] = None,
                         scope: Optional[str] = None) -> List[dict]:
    """One tick across every workspace. The multi-tenant fan-out.

    The privilege split is the point: workspaces are *enumerated* with the admin
    DSN (``rya_workspaces`` carries no RLS policy — it is the table that decides
    what a tenant is), and each one's depth is *read* through its own
    ``open_worker_store`` handle as the weaker ``rya_worker`` role. A supervisor
    that read tenant work on the admin connection would be one bug away from
    scheduling across the boundary D29 keeps at ``workspace_id``.
    """
    from ..store import open_worker_store
    from ..tenancy import Tenancy

    names = workspaces
    if names is None:
        tenancy = Tenancy(admin_dsn)
        try:
            names = [w["id"] for w in tenancy.list_workspaces()]
        finally:
            tenancy.close()

    out = []
    for name in names:
        store = open_worker_store(Path(project_root or "."), name)
        sup = Supervisor(store, driver, workspace=name, environment=environment,
                         project_root=project_root, policy=policy, scope=scope)
        try:
            out.append({"workspace": name, **sup.tick()})
        finally:
            closer = getattr(store, "close", None)
            if closer is not None:
                closer()
    reconciled = reconcile_orgs(admin_dsn, policy=policy)
    if reconciled is not None:
        out.append({"workspace": None, "orgs": reconciled})
    return out


# The last time THIS process reconciled. Process-local rather than durable, and the
# throttle is safe without durability for one reason: `orgs.reconcile` is idempotent,
# so the cost of two supervisors both running it is a duplicated query, not a wrong
# answer. Making it durable would mean a lease and a row for something whose worst
# failure is doing correct work twice.
_last_reconcile: Dict[str, float] = {}


def reconcile_orgs(admin_dsn: str, *,
                   policy: Optional[SupervisorPolicy] = None,
                   now: Optional[float] = None) -> Optional[List[dict]]:
    """Refresh every org's verdict, at most once per interval. ``None`` if skipped.

    **This is §9's "Nobody runs `rya orgs reconcile`", closed.** D35 enforces an org
    budget through a derived per-workspace verdict computed by a privileged
    reconciler, and until now nothing in the platform refreshed it — so a deployment
    that set a budget and never arranged a cron had a budget that capped nothing.

    It is deliberately *not* inside `Supervisor.tick`. A `Supervisor` is scoped to one
    workspace and holds a per-workspace handle; an org rollup spans workspaces and
    needs the admin connection, which is exactly the privilege split
    `supervise_workspaces` documents. Putting it on the fan-out keeps the tenant-scoped
    object unable to read across the boundary D29 keeps at `workspace_id`.

    A failure here is logged and swallowed. The supervisor's job is keeping the fleet
    matched to the work, and a billing rollup that cannot be computed must not stop it
    — the same direction `orgs.read_verdict` fails in, for the same reason.
    """
    policy = policy or SupervisorPolicy()
    interval = float(policy.reconcile_orgs_seconds or 0.0)
    if interval <= 0:
        return None
    stamp = time.monotonic() if now is None else now
    last = _last_reconcile.get(admin_dsn)
    if last is not None and (stamp - last) < interval:
        return None
    _last_reconcile[admin_dsn] = stamp
    from ..orgs import reconcile

    try:
        return reconcile(admin_dsn)
    except Exception as exc:  # noqa: BLE001 - billing must not stop scheduling
        log.warning("org reconcile failed: %s", exc)
        return None
