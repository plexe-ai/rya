"""The execution plane: one worker process per (workspace, agent, version).

PLATFORM_DESIGN §5.2 and §6. This is platform code — the same version as the
`api` process, against the same Postgres — that loads a *client-versioned* bundle
and executes its handlers. The api process enqueues; a worker claims (D4). There
is no api→worker interface, no wire protocol, and nothing to version between
them (D5).

Why per (workspace, agent, **version**) rather than a homogeneous fleet::

    a version-pinned run needs a process on that version, and one process
    cannot hold two — `load_agent` mutates `sys.path` and never unloads
    (runtime/engine.py). D3.

What a worker does that `rya worker` never did before:

- loads a **pinned bundle version** and reports its content hash,
- **advertises its handler set** and refuses to start when the manifest declares
  a tool it cannot serve, so "the image is missing a handler" is a startup
  failure rather than a mid-run one,
- **registers itself** — `queue.claim` took a bare `worker_id` string that was
  never registered or validated,
- heartbeats, honours `cancelRequested`, and **scales to zero** after an idle
  window, tracking cold start as a number with a target (§6).

The legacy mode — no version, load the working tree — is retained and is what
`rya dev` and single-tenant `rya serve` use. It is the same loop; only where the
code comes from differs.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import queue
from .errors import RyaError
from .execution.scope import (SCOPE_TENANT, SCOPE_VERSION, FairOrder, peek,
                              resolve_scope, resolve_version, scope_key)
from .manifest import load_manifest
from .store import now_iso

log = logging.getLogger("rya.worker")

# §6: "cold-start time is a tracked number with a target". A worker that takes
# longer than this to load its bundle and reach the claim loop logs a warning —
# scale-to-zero puts this on the critical path of the next run.
COLD_START_TARGET_MS = 2000

# How long a worker with no claimed work and an empty queue for its key stays
# alive before exiting. 0 disables scale-to-zero (a long-lived `rya worker`).
DEFAULT_IDLE_EXIT_SECONDS = 0

DEFAULT_POLL_SECONDS = 2.0


@dataclass
class WorkerKey:
    """The scheduling unit. One process serves exactly one of these.

    ``scope`` is D27's configurable claimer scope (#19-8b). At ``version`` scope —
    the default, and what a worker has always been — ``agent`` and ``version_id``
    name the one thing this process serves. At ``tenant`` scope they are empty and
    the process serves every agent and version the workspace owns, resolving each
    per item; see :mod:`rya.execution.scope`.
    """

    workspace: str
    agent: str
    version_id: Optional[str] = None    # None = legacy working-tree mode
    bundle_hash: Optional[str] = None
    scope: str = SCOPE_VERSION

    def concurrency_key(self) -> str:
        """The queue's fairness primitive (§6): one workspace must not starve
        another, so caps are applied per key rather than globally."""
        return scope_key(self.workspace, self.agent, self.version_id, scope=self.scope)

    @property
    def tenant_scoped(self) -> bool:
        return self.scope == SCOPE_TENANT

    def describe(self) -> dict:
        return {"workspace": self.workspace, "agent": self.agent,
                "versionId": self.version_id, "bundleHash": self.bundle_hash,
                "scope": self.scope}


@dataclass
class WorkerStats:
    coldStartMs: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    turns: int = 0
    lastClaimAt: Optional[str] = None
    startedAt: str = field(default_factory=now_iso)


def resolve_bundle_root(store, key: WorkerKey, *, project_root: Path,
                        cache_root: Optional[Path] = None) -> tuple[Path, dict]:
    """Materialize the code this worker will run and return ``(root, version)``.

    Legacy mode (no ``version_id``) returns ``project_root`` unchanged with an
    empty version record — that is `rya dev` and single-tenant `rya serve`,
    where the working tree IS the bundle (§10).

    Pinned mode fetches the immutable archive, unpacks it into a
    content-addressed cache and **verifies the hash before importing anything**.
    D12: replay is only sound against the code that wrote the journal, so a
    worker that cannot prove it has that code must not start.

    The archive may come from the local directory or from the object store
    (§5.3) — ``bundles.resolve_bundle_store`` decides, and ``cache_root`` still
    names an explicit local archive root when one is given. The *unpacked* tree
    is always local: it is this process's working copy, not the artifact.
    """
    if key.version_id is None:
        return project_root, {}

    from rya import bundles

    version = store.version_get(key.version_id)
    if version is None:
        raise RyaError(
            "E_VERSION_NOT_FOUND",
            f"Version '{key.version_id}' does not exist in workspace '{key.workspace}'.",
            hint="List versions with `rya versions list`; a retained version is "
                 "required for every run still pinned to it (D12).",
        )
    if version.get("state") == "retired":
        raise RyaError(
            "E_VERSION_RETIRED",
            f"Version '{key.version_id}' is retired and will not be started.",
            hint="Promote an active version, or un-retire this one if runs are still pinned to it.",
        )

    bundle_hash = version["bundleHash"]
    # D20: resolve the archive inside this worker's own tenant namespace. The
    # WorkerKey already carries the workspace, so a worker cannot reach for
    # another tenant's archive even if it somehow learned the hash — which is
    # the point, since a bundle hash is content-addressed and therefore
    # guessable by anyone who has the same bytes.
    store = bundles.resolve_bundle_store(project_root, root=cache_root,
                                         workspace=key.workspace)
    cache = Path(cache_root) if cache_root else bundles.default_archive_root(project_root)
    dest = cache / "unpacked" / bundle_hash
    if not (dest / "rya.agent.yaml").is_file():
        bundles.load_bundle(bundle_hash, store, dest)
    # Re-verified even on a cache hit: the archive was verified when it was
    # unpacked, but the unpacked tree is a mutable directory on this box.
    bundles.verify(dest, bundle_hash)
    return dest, version


def check_handler_set(manifest, agent) -> list[str]:
    """Tools the manifest declares that this bundle cannot serve.

    A declared tool is servable by an ``@agent.tool`` handler, a ``url:`` in the
    manifest (the platform makes the call), or a registered mock. Anything else
    is a hole that would surface as a mid-run ``E_TOOL_NOT_FOUND`` on whichever
    unlucky request first reached it.
    """
    from .tools.registry import default_registry

    registry = default_registry()
    missing = []
    for decl in manifest.tools:
        if getattr(decl, "url", None):
            continue
        if agent is not None and agent.tool_handler(decl.id) is not None:
            continue
        if registry.get(decl.id) is not None:
            continue
        missing.append(decl.id)
    return missing


class InlineExecutor:
    """The claimer *is* the interpreter — one import at startup, bound to one version.

    What ``rya worker`` has always been, and still the right answer for `rya dev`,
    single-tenant `rya serve` and any platform without ``os.fork``. The trade is
    the one D27 names: this process holds a tenant import for its whole life, so
    the sandbox around it has to exist per (workspace, agent, version).
    """

    mode = "inline"

    def __init__(self, engine) -> None:
        self.engine = engine

    @property
    def agent_name(self) -> Optional[str]:
        return getattr(getattr(self.engine, "manifest", None), "name", None)

    def advertise(self) -> dict:
        agent = self.engine.agent
        return {
            "event": agent.event_handler() is not None,
            "jobs": sorted(getattr(agent, "_job_handlers", {})),
            "crons": sorted(getattr(agent, "_cron_handlers", {})),
            "tools": sorted(getattr(agent, "_tool_handlers", {})),
        }

    def missing_handlers(self) -> list:
        return check_handler_set(self.engine.manifest, self.engine.agent)

    def due_jobs(self) -> list:
        return self.engine.due_jobs()

    def warm(self) -> list:
        """Nothing to pre-warm: this process *is* the warm interpreter.

        Present so the two executors answer the same calls. It is also the shape of
        why inline mode cannot serve the tenant scope at all — pre-warming several
        versions means several interpreters, and this one holds exactly the import it
        was constructed with.
        """
        return []

    def drain(self, *, limit: int, worker_id: str, concurrency: int = 1) -> dict:
        from . import turns as _turns

        ran_resumes: list = []
        try:
            ran_resumes = _turns.execute_resumes(self.engine, worker_id=worker_id, limit=limit)
        except Exception:
            log.warning("resume drain failed", exc_info=True)
        ran_turns: list = []
        try:
            ran_turns = _turns.execute_pending(self.engine, worker_id=worker_id, limit=limit)
        except Exception:
            log.warning("turn drain failed", exc_info=True)
        ran_jobs: list = []
        try:
            ran_jobs = self.engine.work_once(concurrency=concurrency)
        except Exception:
            log.warning("job drain failed", exc_info=True)
        return {"turns": ran_turns, "jobs": ran_jobs, "resumes": ran_resumes}

    def close(self) -> None:
        pass


class ForkExecutor:
    """Fork per run from a warm interpreter (D27). The claimer imports nothing.

    ``preflight`` still fails closed before anything is claimed — it just fails in
    the process that can answer. Starting the warm template *is* the preflight: the
    template imports the bundle and reports its handler set, so a hole or an
    import error surfaces at claimer startup exactly as it did when the claimer
    itself held the import. That relocation is what makes D27 compatible with the
    guarantee ``worker.preflight`` exists for; see ``execution/pool.py``.
    """

    mode = "fork"

    def __init__(self, *, store, root: Optional[Path] = None,
                 version: Optional[dict] = None,
                 workspace: str = "default", environment: Optional[str] = None,
                 agent_name: Optional[str] = None, pool=None,
                 state_root: Optional[Path] = None,
                 run_timeout_seconds: float = 0.0,
                 broker=None, config=None,
                 scope: str = SCOPE_VERSION,
                 project_root: Optional[Path] = None,
                 cache_root: Optional[Path] = None,
                 prewarm: tuple = ()) -> None:
        from .execution.pool import WarmPool, default_pool_size

        # D18: the running `BrokerServer`, or None for the trusted posture. This
        # executor is the natural owner because it is the only one that starts a
        # process holding tenant code — the inline executor has nothing to mediate,
        # since the "tenant process" and the claimer are the same process there, which
        # is exactly why the inline mode cannot serve untrusted tenants.
        self.broker = broker
        self.config = config
        self.store = store
        self.scope = scope
        self.root = Path(root) if root else None
        # Where bundles are materialised FROM and cached, at tenant scope: the
        # deployment root, not a bundle root. At version scope `start_worker` has
        # already resolved one bundle and `root` is it.
        self.project_root = Path(project_root) if project_root else None
        self.cache_root = Path(cache_root) if cache_root else None
        self.prewarm = tuple(prewarm)
        # A forked child cannot inherit this process's store handle — a psycopg
        # connection must not be shared across a fork — so it opens its own, and it
        # needs to be told where from. `store.root` is the FileStore arm; Postgres
        # resolves from the DSN and ignores it.
        self.state_root = Path(state_root or getattr(store, "root", None)
                               or self.project_root or self.root or ".")
        self.version = version or {}
        self.workspace = workspace
        self.environment = environment
        self._agent_name = agent_name
        # D32: if a template host is configured, every entry in this pool is an
        # interpreter in the *sandbox container beside this one* rather than a child of
        # this process. Read from the environment rather than passed down because the
        # thing that knows is the driver that rendered the pair, and it configures both
        # halves the same way — see `ContainerDriver.host_argv`.
        from .execution.host import host_socket, host_token

        self.pool = pool if pool is not None else WarmPool(
            max_entries=default_pool_size(scope),
            run_timeout_seconds=run_timeout_seconds,
            host_socket=host_socket(), host_token=host_token())
        self._template = None
        # Wide scope only: one bundle root per version this claimer has been asked
        # to serve, and a round-robin cursor over the groups it serves them for.
        self._roots: dict = {}
        self.fair = FairOrder()
        self.unroutable = 0
        if scope == SCOPE_TENANT:
            # Recorded on the registration, so `GET /workers` and the console can
            # tell a tenant claimer from a version one. They fail differently: a
            # version claimer that cannot import its bundle never starts, and a
            # tenant claimer with one broken agent keeps serving the others.
            self.mode = "fork-tenant"
        if getattr(self.pool, "hosted", False):
            # D32's topology, on the registration for the same reason the scope is: an
            # operator looking at `GET /workers` should be able to tell which
            # arrangement is actually running, and "the templates are in another
            # container" is the fact that decides where to look when one dies. Suffixed
            # rather than replacing, because scope and topology are independent — a
            # hosted claimer can be either width.
            self.mode = f"{self.mode}-hosted"

    @property
    def tenant_scoped(self) -> bool:
        return self.scope == SCOPE_TENANT

    @property
    def agent_name(self) -> Optional[str]:
        if self.tenant_scoped:
            # Deliberately None: a tenant claimer serves many, and answering with
            # whichever one happened to warm first would be read as *the* agent by
            # `preflight`'s E_BUNDLE_MISMATCH check.
            return None
        tmpl = self._template
        return (tmpl.agent_name if tmpl is not None else None) or self._agent_name

    # ---- bundles, per version ---------------------------------------------
    def _resolve(self, version_id: Optional[str]) -> tuple:
        """``(root, version)`` for one version id, materialising it once.

        The wide scope's central move: at version scope ``start_worker`` resolves
        exactly one bundle before the process reaches its loop, and at tenant scope
        this does it lazily, per version, from the same function — so a bundle is
        still hash-verified before anything imports it (D12) and still resolved
        inside this workspace's own object-store namespace (D20).
        """
        if version_id is None:
            if self.root is None and self.project_root is None:
                raise RyaError(
                    "E_VERSION_NOT_FOUND",
                    "Unpinned work needs a mounted project and this claimer has none.",
                    hint="A tenant-scoped claimer materialises each item's pinned "
                         "version from the object store. Work with no pin — a `jobs` "
                         "row, or anything enqueued while nothing was promoted — has "
                         "no bundle to fetch, so it needs either a promoted version in "
                         "this environment or a working tree.")
            return (self.root or self.project_root), {}
        hit = self._roots.get(version_id)
        if hit is not None:
            return hit
        key = WorkerKey(workspace=self.workspace, agent="", version_id=version_id)
        resolved = resolve_bundle_root(
            self.store, key,
            project_root=self.project_root or self.root or Path("."),
            cache_root=self.cache_root)
        self._roots[version_id] = resolved
        return resolved

    def template(self):
        """The warm interpreter, at version scope. One bundle, for this process's life."""
        if self._template is None or not self._template.alive:
            self._template = self._acquire(root=self.root, version=self.version,
                                           agent=self._agent_name)
        return self._template

    def template_for(self, *, version_id: Optional[str], agent: str):
        """The warm interpreter for one group, warming it if this is the first item.

        **This call is the preflight**, and its position in ``drain`` is what
        preserves the guarantee the wide scope looked like it gave up. Starting a
        template imports the bundle and reports its handler set, so a hole or an
        import error raises *here* — before the fork that claims. §7.1 said the
        guarantee would degrade from "before claiming" to "after claiming, in the
        fork"; it does not, because the claimer peeks at the queue rather than
        claiming from it and therefore knows which version to warm first.
        """
        root, version = self._resolve(version_id)
        template = self._acquire(root=root, version=version,
                                 agent=agent or version.get("agent"))
        if template.missing:
            # The hole, refused before the claim. At version scope this same fact is
            # raised by `Worker.preflight` and stops the process from starting; here
            # it stops *this group* from being dispatched to and leaves the tenant's
            # other agents running, which is the only difference the wide scope makes
            # to it. `_drain_tenant` records the refusal against the group.
            raise RyaError(
                "E_HANDLER_SET_INCOMPLETE",
                f"Bundle {version.get('bundleHash') or '(working tree)'} for agent "
                f"'{template.agent_name or agent or '?'}' declares "
                f"{len(template.missing)} tool(s) it cannot serve: "
                f"{', '.join(sorted(template.missing))}.",
                hint="Implement them with @agent.tool, give them a `url:` in "
                     "rya.agent.yaml, or remove them from `tools:`. Nothing is claimed "
                     "for this agent until it can be served, so the failure is not a "
                     "half-executed run.")
        return template

    def _acquire(self, *, root, version: dict, agent: Optional[str]):
        return self.pool.acquire(
            bundle_hash=version.get("bundleHash"), root=root, version=version,
            workspace=self.workspace, environment=self.environment,
            state_root=self.state_root, broker=self.broker,
            agent_name=agent, routes=self._routes_for(agent, version))

    def _routes_for(self, agent: Optional[str], version: dict) -> dict:
        """Model routes with the credentials taken out, from the broker that holds them.

        A handler legitimately knows which model it is calling — ``ctx.llm``'s journal
        label is built from ``route.model``, and D9 content-keys on it, so withholding
        the name would make every mediated replay look like drift. What it must not
        know is the key.

        Resolved by the **broker** rather than here, and that is not tidying: at
        tenant scope the routes differ per agent, and the thing that knows how to
        resolve one agent's config is the thing holding the credential it resolves to.
        Two projections of a ``RunConfig`` — one that strips the key and one that does
        not — is how a key ends up in the wrong one.
        """
        if self.broker is None:
            # The trusted posture. `WarmTemplate._cfg` sends no routes at all in
            # unmediated mode, because the child resolves its own config from the
            # environment exactly as an inline worker does.
            return {}
        return self.broker.public_routes(agent=agent or "",
                                         version_id=str(version.get("id") or ""))

    # ---- what this claimer can serve --------------------------------------
    def advertise(self) -> dict:
        """The handler set behind this claimer.

        At tenant scope this is the union over the templates that are warm, plus the
        (agent, version) pairs they hold. A union is the honest shape for a claimer
        that fronts several bundles: it answers "can something behind this process
        serve `send-email`" and deliberately not "can *the* agent serve it", which is
        a question a tenant claimer has no single answer to. ``agents`` is what an
        operator actually reads.
        """
        if not self.tenant_scoped:
            return dict(self.template().handlers)
        union = {"event": False, "jobs": set(), "crons": set(), "tools": set()}
        agents: dict = {}
        for tmpl in self.pool.templates:
            union["event"] = union["event"] or bool(tmpl.handlers.get("event"))
            for field_name in ("jobs", "crons", "tools"):
                union[field_name] |= set(tmpl.handlers.get(field_name) or [])
            agents[tmpl.agent_name or "?"] = (tmpl.version or {}).get("id")
        return {"event": union["event"], "jobs": sorted(union["jobs"]),
                "crons": sorted(union["crons"]), "tools": sorted(union["tools"]),
                "agents": agents, "scope": SCOPE_TENANT}

    def missing_handlers(self) -> list:
        """Declared tools nothing warm can serve.

        At tenant scope this covers the pre-warmed versions only, which is the whole
        set at startup and a subset later. That is not a weaker guarantee, it is the
        same one applied per version: every bundle is checked when its template warms,
        and its template warms before its first item is claimed.
        """
        if not self.tenant_scoped:
            return list(self.template().missing)
        missing: list = []
        for tmpl in self.pool.templates:
            missing += [f"{tmpl.agent_name or '?'}:{tool}" for tool in tmpl.missing]
        return missing

    def warm(self) -> list:
        """Pre-warm the current version of each named environment. Returns what warmed.

        Opt-in, and empty by default for the reason ``SupervisorPolicy`` gives: warming
        every promoted agent would defeat the scale-to-zero the execution plane just
        made two-way, and §6 names idle cost as *the* constraint. What it buys at
        tenant scope is different from the narrow scope, though, and better: the
        narrow scope pre-warmed by keeping a whole extra *sandbox* alive per key, and
        this keeps an interpreter alive inside a sandbox the tenant already has.

        A version that cannot be warmed is logged and skipped, not raised. At startup
        this runs inside ``preflight``, where a raise is correct for the *claimer's own*
        agent — but a tenant claimer has no own agent, and refusing to serve four
        healthy agents because a fifth has a broken bundle would be the wrong trade.
        """
        if not self.prewarm:
            return []
        env_list = getattr(self.store, "env_list", None)
        if env_list is None:
            return []
        warmed: list = []
        for row in env_list() or []:
            if row.get("name") not in self.prewarm:
                continue
            agent, version_id = row.get("agent"), row.get("currentVersionId")
            if not (agent and version_id):
                continue
            try:
                self.template_for(version_id=version_id, agent=agent)
            except Exception as exc:  # noqa: BLE001 - one bad bundle is not a fleet
                log.warning("pre-warm of %s/%s failed: %s", agent, version_id, exc)
                continue
            warmed.append({"agent": agent, "versionId": version_id})
        log.info("pre-warmed %d version(s) for %s", len(warmed), ", ".join(self.prewarm))
        return warmed

    def due_jobs(self) -> list:
        """A store query, so the claimer answers it without importing anything.

        Duplicated from ``Engine.due_jobs`` rather than shared, because sharing it
        would mean the claimer holding an ``Engine`` — and an ``Engine`` holds an
        agent. The whole property being defended is that this process has no
        handle on tenant code.
        """
        listing = getattr(self.store, "list_jobs", None)
        if listing is None:
            return []
        now = now_iso()
        return [j for j in listing("pending") if (j.get("runAt") or "") <= now]

    def drain(self, *, limit: int, worker_id: str, concurrency: int = 1) -> dict:
        """Fork once **per item**, up to ``limit``, stopping when a fork finds nothing.

        A loop rather than one fork with ``limit=N``, and the difference is the whole
        claim: handing a single child N items would make this fork-per-*tick*, so ten
        queued turns would share one address space and a handler that corrupted its
        interpreter would take the other nine with it.

        ``concurrency`` is accepted and ignored. Inline it means "run due jobs in N
        threads on cloned engines"; here every item already has its own process, and
        running several at once is a decision the supervisor makes by launching more
        claimers — which it can do because it scales on depth. Doing it here as well
        would multiply the two.
        """
        if self.tenant_scoped:
            return self._drain_tenant(limit=limit, worker_id=worker_id)
        turns: list = []
        jobs: list = []
        resumes: list = []
        error = None
        fork_ms = 0
        for _ in range(max(1, limit)):
            outcome = self.template().drain(limit=1, worker_id=worker_id)
            fork_ms += outcome.duration_ms
            if outcome.error:
                # Reported, and the loop stops: a child that died is a reason to let
                # the tick end and the supervisor look, not to fork again immediately.
                log.warning("forked run reported an error: %s", outcome.error)
                error = outcome.error
                break
            if not outcome.count:
                break
            turns += outcome.turns
            jobs += outcome.jobs
            resumes += outcome.resumes
        return {"turns": turns, "jobs": jobs, "resumes": resumes,
                "forkMs": fork_ms, "error": error}

    def _drain_tenant(self, *, limit: int, worker_id: str) -> dict:
        """Peek, warm, fork — round-robin across this tenant's groups.

        The order of the three verbs is the whole design of the wide scope:

        1. **Peek** (:func:`rya.execution.scope.peek`) is a read. Nothing is claimed,
           so nothing is held, so a claimer that dies between the peek and the fork
           has cost nothing and reclaimed nothing.
        2. **Warm** is the preflight. The template imports the bundle and reports its
           handler set, which is where ``E_HANDLER_SET_INCOMPLETE`` and an import
           error surface — *before* the claim, which is the guarantee §7.1 predicted
           this scope would lose.
        3. **Fork** claims. One item, in a child, filtered to the group's own agent
           and version — by the capability when mediated, and by the engine's own
           manifest and version record when not.

        The peek can be stale by the time the fork claims: a sibling claimer may have
        taken the item. That is harmless and is why the ordering is safe — the fork's
        claim is still atomic, its filter is still its own group's, and an empty
        result simply retires that group for this tick.

        **One failing group does not stop the tick.** At the narrow scope a fork error
        ends the loop, on purpose: there was one bundle and one agent, so "the
        interpreter holding this tenant's code keeps dying" was the whole worker's
        story. Here it is one agent of several, and letting it end the tick would let
        one broken bundle starve every sibling — the exact property this scope has to
        defend.
        """
        groups = peek(self.store, workspace=self.workspace)
        self.unroutable = sum(g.depth for g in groups if not g.attributed)
        if self.unroutable:
            # Nothing can run these: an item whose metadata names no agent has no
            # handler set to be executed against, and picking one would be D22's
            # cross-agent execution path chosen deliberately. The supervisor refuses
            # to invent a key from the same rows for the same reason.
            log.warning("%d item(s) name no agent and cannot be routed at tenant scope",
                        self.unroutable)
        candidates = [g for g in groups if g.attributed]

        turns: list = []
        jobs: list = []
        resumes: list = []
        errors: list = []
        fork_ms = 0
        exhausted: set = set()
        served: list = []
        # ``limit`` counts ITEMS, matching the narrow scope's "fork once per item, up to
        # limit". A group that turns out to have nothing — the peek is a read, so it can
        # be stale by the time the fork claims — is retired and the slot goes to the next
        # group instead of being spent. Otherwise a tick with N groups and N-1 of them
        # already drained by a sibling claimer would do almost no work while reporting a
        # full budget.
        budget = max(1, limit)
        # Bounded independently of the budget, because each group can consume at most
        # one attempt before it is either productive or retired.
        for _ in range(budget + len(candidates) + 1):
            if budget <= 0:
                break
            ready = [g for g in self.fair.order(candidates) if g.key not in exhausted]
            if not ready:
                break
            group = ready[0]
            self.fair.served(group)
            version_id = resolve_version(self.store, group, environment=self.environment)
            try:
                template = self.template_for(version_id=version_id, agent=group.agent)
                outcome = template.drain(limit=1, worker_id=worker_id)
            except RyaError as exc:
                log.warning("group %s cannot be served: %s", group.key, exc.message)
                errors.append({"key": group.key, "error": exc.message})
                exhausted.add(group.key)
                continue
            fork_ms += outcome.duration_ms
            if outcome.error:
                log.warning("forked run for %s reported an error: %s", group.key,
                            outcome.error)
                errors.append({"key": group.key, "error": outcome.error})
                exhausted.add(group.key)
                continue
            if not outcome.count:
                exhausted.add(group.key)
                continue
            served.append(group.key)
            turns += outcome.turns
            jobs += outcome.jobs
            resumes += outcome.resumes
            budget -= outcome.count
        out = {"turns": turns, "jobs": jobs, "resumes": resumes, "forkMs": fork_ms,
               "groups": [g.key for g in candidates], "served": served,
               "unroutable": self.unroutable, "error": None}
        if errors:
            # Reported as a single line so `Worker.drain_once` keeps one `error` key,
            # and in full under `errors` so an operator can see which agent it was.
            out["errors"] = errors
            out["error"] = "; ".join(f"{e['key']}: {e['error']}" for e in errors)
        return out

    def close(self) -> None:
        """Stop the templates, then the broker. In that order.

        The order is not cosmetic: a template's child may be mid-call, and closing the
        socket first turns an orderly shutdown into ``E_BROKER_UNAVAILABLE`` inside a
        handler that was about to finish.

        **Closing the broker at all is a Phase 5 fix.** This method stopped the pool and
        left the `BrokerServer` running, so every mediated claimer that exited leaked its
        0700 temp directory and a stale socket — invisible on a laptop and one directory
        per restart on a box that recycles claimers. The executor is the right owner
        because it is the only thing that holds the server: `_start_broker` builds it and
        hands it straight here.
        """
        self.pool.close()
        self._template = None
        closer = getattr(self.broker, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 - a shutdown must not raise
                log.debug("broker close failed", exc_info=True)


class Worker:
    """One execution-plane instance.

    Owns ``ctx``, the journal and replay for the runs it executes, holds a
    tenant-scoped store handle, and claims from the queue. It is platform code,
    so ``ctx`` stays a LOCAL API — all 36 journaled operations remain in-process
    function calls (§5.2).

    Since D27 the *claiming* and the *executing* are separable: this class does the
    first and delegates the second to an executor. ``InlineExecutor`` keeps them in
    one process (what a worker always was); ``ForkExecutor`` puts the run in a fork
    of a warm interpreter and leaves this process with no tenant import at all.
    Everything else here — registration, preflight, heartbeat, claimable depth,
    idle exit — is identical in both modes, which is the point: the supervisor
    (D25) schedules the same shape either way.
    """

    def __init__(self, engine=None, key: Optional[WorkerKey] = None, *,
                 version: Optional[dict] = None,
                 worker_id: Optional[str] = None,
                 idle_exit_seconds: float = DEFAULT_IDLE_EXIT_SECONDS,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 concurrency: int = 1,
                 turn_limit: int = 10,
                 store=None,
                 executor=None):
        if key is None:  # pragma: no cover - programmer error
            raise TypeError("Worker requires a WorkerKey")
        if executor is None and engine is None:
            raise TypeError("Worker needs either an engine (inline) or an executor")
        self.engine = engine
        self.executor = executor if executor is not None else InlineExecutor(engine)
        self.store = store if store is not None else engine.store
        self.key = key
        self.version = version or {}
        self.stats = WorkerStats()
        self.idle_exit_seconds = idle_exit_seconds
        self.poll_seconds = poll_seconds
        self.concurrency = max(1, concurrency)
        self.turn_limit = turn_limit
        self.id = worker_id or self._mint_id()
        self.handlers: dict = {}
        self.prewarmed: list = []
        self._registered = False
        self._stop = False

    def _mint_id(self) -> str:
        from .store import _new_id
        return _new_id("wrk")

    # ---- startup -------------------------------------------------------
    def advertise(self) -> dict:
        """The handler set this process can actually serve."""
        return self.executor.advertise()

    def preflight(self) -> dict:
        """Fail closed BEFORE claiming anything.

        §5.2: a worker "refuses to start if its registered handler set does not
        cover the tools its manifest version declares, so 'the image is missing a
        handler' surfaces at startup rather than mid-run."
        """
        # #5/D19: the key and the STORE must agree about the tenant. Checked
        # FIRST, before anything that inspects the bundle: everything this process
        # writes is stamped with `key.workspace` (`pin_run`, `register`) while
        # every row it reads comes back through the store's own scope, so a
        # disagreement means it writes rows attributed to one tenant into
        # another's namespace — silently, because each half is individually
        # consistent. That is a more fundamental misconfiguration than a missing
        # handler, and it is cheaper to detect.
        store_ws = getattr(self.store, "workspace_id", None)
        if store_ws is not None and store_ws != self.key.workspace:
            raise RyaError(
                "E_WORKSPACE_MISMATCH",
                f"Worker key is scoped to workspace '{self.key.workspace}' but its "
                f"store is scoped to '{store_ws}'.",
                hint="Usually this means `--workspace` names a tenant the "
                     "deployment cannot scope to: workspace isolation needs "
                     "RYA_MULTITENANT=1 plus Postgres plus `tenancy.setup()`. "
                     "Without those, `open_worker_store` falls back to the default "
                     "workspace — and running anyway is what made --workspace "
                     "decorative, so this refuses instead.",
            )
        if self.key.tenant_scoped:
            # The wide scope has no single bundle to import, so there is nothing here
            # that could refuse to start — and that is the honest answer rather than a
            # gap. The handler-set check moved to `ForkExecutor.template_for`, which
            # runs per group and still runs *before* that group's claim. What is left
            # to do at startup is pre-warming, which is where a promoted version's
            # import error surfaces at boot exactly as it used to.
            self.prewarmed = self.executor.warm()
            self.handlers = self.advertise()
            holes = self.executor.missing_handlers()
            if holes:
                # Logged, not raised. Refusing to serve four healthy agents because a
                # fifth declares a tool it cannot serve would make one tenant's bad
                # deploy an outage for its siblings, and the per-group refusal already
                # stops the broken one from claiming.
                log.warning("pre-warmed bundles declare %d unservable tool(s): %s",
                            len(holes), ", ".join(sorted(holes)))
            return self.handlers
        # Both of these ask the EXECUTOR, so the answer comes from whatever holds
        # the import. Inline that is this process; under D27 it is the warm
        # template, and starting it is what makes this call still "before claiming
        # anything" rather than "on the first request that needed a handler".
        self.handlers = self.advertise()
        missing = self.executor.missing_handlers()
        if missing:
            raise RyaError(
                "E_HANDLER_SET_INCOMPLETE",
                f"Bundle {self.key.bundle_hash or '(working tree)'} declares "
                f"{len(missing)} tool(s) it cannot serve: {', '.join(sorted(missing))}.",
                hint="Implement them with @agent.tool, give them a `url:` in "
                     "rya.agent.yaml, or remove them from `tools:`. A worker will "
                     "not start with a hole in its handler set.",
            )
        # D12: the version record and the loaded manifest must describe the same
        # agent, or this process would claim work under a name it does not serve.
        declared = self.version.get("agent")
        loaded = self.executor.agent_name
        if declared and loaded and declared != loaded:
            raise RyaError(
                "E_BUNDLE_MISMATCH",
                f"Version {self.key.version_id} is for agent '{declared}' but the "
                f"loaded bundle's manifest says '{loaded}'.",
                hint="The version record and the bundle disagree; re-deploy rather than patching either.",
            )
        return self.handlers

    def register(self) -> dict:
        # §11.12: maxWorkers caps how much of the fleet one workspace can occupy.
        # Checked at registration — the only point where refusing costs nothing,
        # since the process has not claimed work yet and exiting loses no state.
        # The supervisor (D25) checks the same quota again at SCHEDULE time, which
        # is earlier and cheaper still; this stays because a worker started by hand
        # or by a compose file never passes through a supervisor.
        from .quotas import require_admission
        require_admission(self.store, kind="worker")

        record = {
            "id": self.id,
            "workspaceId": self.key.workspace,
            "agent": self.key.agent,
            "versionId": self.key.version_id,
            "bundleHash": self.key.bundle_hash,
            "concurrencyKey": self.key.concurrency_key(),
            # D27/#19-8b. Recorded because the same registration means different
            # things at the two scopes: at `version` the agent and versionId columns
            # above describe what this process serves, and at `tenant` they are empty
            # and `handlers.agents` is where the answer lives.
            "scope": self.key.scope,
            "handlers": self.handlers,
            # D27: whether this process holds the tenant import or forks for it.
            # Worth recording because the two have different failure modes and the
            # registration is the only place an operator can tell them apart — a
            # fork claimer with a wedged template looks exactly like a busy worker
            # otherwise.
            "mode": getattr(self.executor, "mode", "inline"),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "coldStartMs": self.stats.coldStartMs,
            "sdkVersion": self.version.get("sdkVersion"),
        }
        reg = getattr(self.store, "worker_register", None)
        if reg is not None:
            self._registered = True
            return reg(record)
        return record

    def heartbeat(self, **fields) -> None:
        beat = getattr(self.store, "worker_heartbeat", None)
        if beat is None or not self._registered:
            return
        try:
            beat(self.id, stats=self.stats.__dict__, **fields)
        except Exception:  # a bookkeeping failure must not stop the work
            log.debug("worker heartbeat failed", exc_info=True)

    def deregister(self, reason: str) -> None:
        dereg = getattr(self.store, "worker_deregister", None)
        if dereg is not None and self._registered:
            try:
                dereg(self.id, reason)
            except Exception:
                log.debug("worker deregister failed", exc_info=True)

    # ---- the loop ------------------------------------------------------
    def drain_once(self) -> dict:
        """One tick: reclaim + run due chat turns, approval resumes, then due jobs.

        All three are queue claims with leases, so N workers on the same key are
        safe and a process that dies mid-item has its work reclaimed (§6).

        Resumes are drained BEFORE new turns on purpose. A resume continues a run
        that is already holding an approval a human has acted on, and starting
        fresh work ahead of it would add latency to the one item someone is
        actually waiting for.

        The executor decides *where* this happens (D27): in this process, or in a
        fork of a warm interpreter. The bookkeeping below is identical either way,
        which is deliberate — a supervisor scaling on `stats.claimed` must not have
        to know which mode a worker is in.
        """
        try:
            tick = self.executor.drain(limit=self.turn_limit, worker_id=self.id,
                                       concurrency=self.concurrency)
        except RyaError as exc:
            # A fork mode failure (the template died, the bundle stopped importing)
            # is not a tick that ran nothing — it is a broken worker, and saying so
            # is what lets the supervisor replace it instead of watching it poll
            # forever. Still not raised: the item's lease reclaims it elsewhere.
            log.warning("drain failed: %s", exc.message)
            tick = {"turns": [], "jobs": [], "resumes": [], "error": exc.message}
        ran_turns = tick.get("turns") or []
        ran_jobs = tick.get("jobs") or []
        ran_resumes = tick.get("resumes") or []

        done = len(ran_turns) + len(ran_jobs) + len(ran_resumes)
        if done:
            self.stats.claimed += done
            self.stats.turns += len(ran_turns)
            self.stats.completed += sum(
                1 for j in ran_jobs if j.get("status") in ("completed", "waiting_approval"))
            self.stats.failed += sum(1 for j in ran_jobs if j.get("status") == "failed")
            self.stats.lastClaimAt = now_iso()
        out = {"turns": ran_turns, "jobs": ran_jobs, "resumes": ran_resumes,
               "count": done}
        if tick.get("error"):
            out["error"] = tick["error"]
        return out

    def queue_depth(self) -> int:
        """Pending work **this worker can actually claim** — what §6 scales on.

        Deliberately not ``queue_counts()["pending"]``. That counts every row in
        the queue table, and the table serves two product surfaces (D14): chat
        turns dispatched to `rya worker`, and the SDK-free ``/queue/*`` jobs that
        foreign consumers (a TypeScript DAG worker) claim for themselves. This
        worker only ever claims ``chat-turn`` (``turns.execute_pending``) and
        ``approval-resume`` (``turns.execute_resumes``), plus the `jobs`
        primitive's due handlers.

        Counting foreign jobs here means one pending ``/queue/*`` job — work this
        process will never touch — reads as "busy" forever, so the idle window
        never elapses and scale-to-zero never fires. §6 opens by naming idle cost
        as *the* constraint for a fleet of N workspaces x M agents x V versions, so
        a worker that cannot go idle is the expensive failure, not a cosmetic one.

        Version pinning applies for the same reason it applies to claiming (D12): a
        turn pinned to another version is not this worker's work. D22 adds the
        agent for the same reason again, and it matters more here than the version
        does: an unpinned worker has no version to filter on, so before D22 a
        sibling agent's pending turn counted as depth forever. This worker would
        never claim it and never go idle — exactly the scale-to-zero failure the
        rest of this docstring is about, reached by a different route.
        """
        from .turns import RESUME_JOB

        if self.key.tenant_scoped:
            # The same peek the drain routes on, so "am I idle" and "is there anything
            # I can serve" are one question. Unattributed items are excluded on
            # purpose: a tenant claimer will not run them (it has no handler set to
            # run them against), and counting work this process will never touch is
            # exactly the scale-to-zero failure the rest of this docstring is about.
            return sum(g.depth for g in peek(self.store, workspace=self.key.workspace)
                       if g.attributed)
        depth = 0
        listing = getattr(self.store, "queue_list", None)
        if listing is not None:
            mine = self.key.version_id
            my_agent = self.key.agent or None
            for kind in ("chat-turn", RESUME_JOB):
                for job in listing("pending", kind) or []:
                    pinned = queue.version_of(job)
                    if mine is not None and pinned is not None and pinned != mine:
                        continue
                    owner = queue.agent_of(job)
                    if my_agent is not None and owner is not None and owner != my_agent:
                        continue
                    depth += 1
        try:
            depth += len(self.executor.due_jobs())
        except Exception:
            pass
        return depth

    def stop(self) -> None:
        """Cooperative shutdown, bounded by the poll interval — the same shape as
        the queue's `cancelRequested` flag, and deliberately not a new mechanism."""
        self._stop = True

    def run(self, *, max_iterations: Optional[int] = None,
            on_tick: Optional[Callable[[dict], Any]] = None) -> dict:
        """Claim and execute until stopped, iteration-capped, or idle-exited."""
        self.preflight()
        self.register()
        log.info("worker %s up: %s handlers=%s coldStart=%dms", self.id,
                 self.key.concurrency_key(), self.handlers, self.stats.coldStartMs)

        idle_since: Optional[float] = None
        iterations = 0
        reason = "stopped"
        try:
            while not self._stop:
                tick = self.drain_once()
                self.heartbeat()
                if on_tick is not None:
                    on_tick(tick)
                iterations += 1

                if tick["count"]:
                    idle_since = None
                elif self.idle_exit_seconds > 0 and self.queue_depth() == 0:
                    # §6 scale to zero. Cold start then sits on the critical path
                    # of the next run for this key, which is why COLD_START_TARGET_MS
                    # exists and why production environments are pre-warmed.
                    now = time.monotonic()
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since >= self.idle_exit_seconds:
                        reason = "idle"
                        break

                if max_iterations is not None and iterations >= max_iterations:
                    reason = "max-iterations"
                    break
                if not self._stop:
                    time.sleep(self.poll_seconds)
        finally:
            self.deregister(reason)
            # Under D27 the warm interpreters are child PROCESSES, so an exit that
            # skipped this would leave a template holding a tenant import behind
            # after the claimer that owns it is gone — and nothing would ever
            # collect it, because the pool is the only thing that knows it exists.
            self.executor.close()
            log.info("worker %s down (%s): %s", self.id, reason, self.stats.__dict__)
        return {"workerId": self.id, "reason": reason, "iterations": iterations,
                "stats": self.stats.__dict__, **self.key.describe()}


def _start_broker(*, store, project_root: Path, workspace: str, agent: str,
                  manifest, environment: Optional[str]):
    """Stand up the broker for this claimer. Returns ``(server, config)``.

    Everything the tenant must not hold is gathered here, in one function, so the
    answer to "what is on the platform's side of the boundary" is a readable list:
    the store (and therefore the DSN), the resolved ``RunConfig`` (and therefore the
    provider credential and the tenant's declared secrets), the key ring (and
    therefore the seal keys), and the egress service (and therefore the only route
    out).

    The quota check is wired to :func:`quotas.require_admission` with ``kind="model"``
    so #21's refusal is the same code path, the same policy row and the same error
    code an admission refusal already uses. A second implementation of "is this
    workspace over budget" would be a second answer.

    ``manifest`` may be ``None``, which is the tenant scope: this claimer does not
    know at startup which agents it will serve, so the config is resolved per
    ``(agent, version)`` on demand instead — see :meth:`BrokerServer.config_for` and
    the ``resolve`` closure below. The key ring, the store and the quota check are
    workspace-wide and need no such treatment, which is itself worth noticing: what
    is per-agent here is exactly the *declared* configuration (routes and secrets
    come out of a manifest), and what is per-tenant is everything that came out of
    the deployment.
    """
    from .broker.server import BrokerServer
    from .config import current_environment, resolve_run_config
    from .sdk.context import load_env

    env = load_env(project_root)
    resolved_env = environment or current_environment(env)
    config = (resolve_run_config(manifest, env=env, environment=resolved_env)
              if manifest is not None else None)
    keyring = None
    try:
        from .keys import resolve_keyring

        keyring = resolve_keyring(root=project_root, env=env)
    except Exception as exc:  # noqa: BLE001 - reported by readiness, not fatal here
        log.warning("broker started without a key ring: %s", exc)

    def quota_check(ws: str, ctx: dict) -> None:
        from .quotas import require_admission

        require_admission(store, kind="model")

    from .egress import resolve_egress

    def config_for(agent_name: str, version_id: str = ""):
        """One agent's ``RunConfig``, from the manifest its version shipped with.

        Read from the **version record** rather than from a mounted working tree,
        because at tenant scope there is no tree and there may be several live
        versions of the same agent at once. D21 persists the manifest on the version
        for exactly this reason ("a manifest-free `api` learns what agents exist"),
        and this is the second reader of that decision.

        A record written before D21 carries no manifest and resolves to ``None``: the
        broker can then still mediate state for that version but not inference, which
        is a truthful degradation rather than a silent fallback to a *different*
        agent's routes.
        """
        from . import deployments

        record = None
        if version_id:
            getter = getattr(store, "version_get", None)
            if getter is not None:
                record = getter(version_id)
        if record is None and agent_name and resolved_env:
            try:
                record = deployments.current_version(store, resolved_env, agent_name)
            except Exception:  # noqa: BLE001 - no pointer is not a config error
                record = None
        shipped = deployments.manifest_of(record or {})
        if shipped is None:
            return None
        from .manifest import parse_manifest

        return resolve_run_config(parse_manifest(shipped), env=env,
                                  environment=resolved_env)

    def egress_for(agent_name: str):
        return resolve_egress(store=store, agent=agent_name, env=env)

    # D32: on the split topology the broker must bind where the *other container* can
    # reach it, so the driver names the path and both halves are given the same value.
    # Unset — the weak topology and every test — and the broker picks its own 0700 temp
    # directory exactly as before, which is the better default when there is nobody
    # else to share with.
    from .broker.protocol import SOCKET_ENV

    bind_at = (os.environ.get(SOCKET_ENV) or "").strip()
    server = BrokerServer(store=store, project_root=project_root,
                          workspace=workspace, agent=agent, config=config,
                          socket_path=Path(bind_at) if bind_at else None,
                          keyring=keyring, quota_check=quota_check,
                          config_for=None if manifest is not None else config_for,
                          egress_for=None if manifest is not None else egress_for,
                          egress=resolve_egress(store=store, agent=agent, env=env)
                          if manifest is not None else None)
    server.start()
    log.info("broker mediation on for %s/%s (socket %s)", workspace,
             agent or "(every agent)", server.socket_path)
    return server, config


def _start_tenant_claimer(*, project_root: Path, store, workspace: str,
                          environment: Optional[str], cache_root: Optional[Path],
                          fork: bool, mediated: bool, prewarm: tuple, t0: float,
                          run_timeout_seconds: float, **worker_kwargs) -> Worker:
    """One claimer for a whole tenant (D27's wide scope, #19-8b).

    What is *absent* here is the point. There is no ``resolve_bundle_root``, no
    ``load_manifest`` and no ``load_agent``: this process is a generic claimer, and
    the version, the agent and the bundle are properties of each item rather than of
    the process. §7.1 called this "a generic claimer scoped to one tenant: claim any
    job for `acme`, read the job's pinned versionId, materialise that bundle from a
    local cache, fork an interpreter, run, discard" — with the one correction Phase 5
    found necessary, which is that it *peeks* before claiming so that materialising
    and importing still happen before the claim.

    **Fork is mandatory, and not for the same reason mediation is.** Inline mode holds
    one import for the life of the process, so a tenant-scoped inline claimer would
    have to pick one of its tenant's agents at startup and would then be a
    version-scoped claimer with a misleading key. That is a contradiction rather than
    a missing feature, so it is refused rather than downgraded.
    """
    if not fork:
        raise RyaError(
            "E_FORK_UNAVAILABLE",
            "The tenant claimer scope requires fork-per-run.",
            hint="Inline mode binds one import to the process for its whole life "
                 "(`load_agent` mutates sys.path and never unwinds it, D3), so it can "
                 "serve one agent-version and not a tenant. Add --fork, or use "
                 "--scope version.",
        )
    from .execution.pool import FORK_AVAILABLE

    if not FORK_AVAILABLE:
        raise RyaError(
            "E_FORK_UNAVAILABLE",
            "The tenant claimer scope requires os.fork, which this platform lacks.",
            hint="Use --scope version: one claimer per (workspace, agent, version), "
                 "with the import held in the claimer itself.")

    broker = None
    if mediated:
        # `manifest=None` is what makes this broker serve every agent: it resolves a
        # RunConfig per (agent, version) from the version record instead of holding
        # one. `agent=""` likewise — the capability names the agent per dispatch, and
        # a default would be a route out of that.
        broker, _ = _start_broker(store=store, project_root=project_root,
                                  workspace=workspace, agent="", manifest=None,
                                  environment=environment)
    key = WorkerKey(workspace=workspace, agent="", version_id=None,
                    scope=SCOPE_TENANT)
    executor = ForkExecutor(store=store, workspace=workspace,
                            environment=environment, scope=SCOPE_TENANT,
                            project_root=project_root, cache_root=cache_root,
                            state_root=getattr(store, "root", None) or project_root,
                            run_timeout_seconds=run_timeout_seconds,
                            broker=broker, prewarm=prewarm)
    worker = Worker(key=key, store=store, executor=executor, **worker_kwargs)
    worker.stats.coldStartMs = int((time.monotonic() - t0) * 1000)
    log.info("tenant claimer up for %s (prewarm=%s, mediated=%s)", workspace,
             ",".join(prewarm) or "-", bool(broker))
    return worker


def start_worker(*, project_root: Path, store, workspace: str = "default",
                 version_id: Optional[str] = None, environment: Optional[str] = None,
                 agent_name: Optional[str] = None,
                 cache_root: Optional[Path] = None, fork: Optional[bool] = None,
                 run_timeout_seconds: float = 0.0,
                 mediated: Optional[bool] = None,
                 scope: Optional[str] = None,
                 prewarm: tuple = (),
                 **worker_kwargs) -> Worker:
    """Build a ready-to-run :class:`Worker`.

    ``version_id`` pins explicitly. ``environment`` resolves the current pointer
    for that environment instead (§9: new runs go to the promoted version). Both
    absent = legacy working-tree mode.

    ``fork`` selects D27's execution mode: the returned worker claims but does not
    import, and each item runs in a fork of a warm interpreter. It is a parameter
    rather than the only behaviour because ``os.fork`` does not exist everywhere,
    and because the in-process mode is still the right one for `rya dev`, where the
    bundle IS the working tree and a second process buys nothing.

    It is **tri-state**, and the third state is the point: ``None`` means "the scope
    decides", ``True`` and ``False`` are the caller stating a preference. The tenant
    scope requires forking, so it fills in an omitted answer and refuses a
    contradictory one — which a plain ``bool`` cannot express, because the default and
    an explicit ``False`` are the same value.

    ``scope`` is D27's claimer scope, and #19-8b's whole content is that it is a
    parameter here rather than a rewrite of this function. At ``version`` scope
    (default) everything below resolves one bundle, one manifest and one template
    before the claim loop starts — what a worker has always done. At ``tenant``
    scope it resolves **none** of them: there is no agent to name, no version to
    pin and no manifest to read at startup, because the claimer discovers all three
    per item. Both modes then run the identical loop, which is the property that
    made building fork-per-run first worth it.

    ``mediated`` turns on the D18 broker: this process keeps the store, the keys and
    the provider credential, and the template and its forks get a socket instead.
    It **requires** ``fork``, and the reason is structural rather than a missing
    feature — in inline mode the claimer *is* the process that imports tenant code, so
    there is no boundary for a broker to sit on. Defaults to
    ``RYA_BROKER=1``, and is forced on by the untrusted posture.
    """
    t0 = time.monotonic()
    # The launch gate, and this call site is the one that was missing.
    #
    # Phase 3 built `require_isolation_for_tenancy` and wired it into `rya supervisor`
    # only — so a *hand-started* worker walked straight past it, and
    # `RYA_UNTRUSTED_TENANTS=1` on the `local` driver started happily and served
    # untrusted tenants on a shared kernel with a database credential in the process.
    # A gate the supervisor honours and the worker ignores is not a gate; it is a
    # convention with a nice error message, which is precisely §9 risk 8's failure
    # mode. Found by the e2e asserting the refusal rather than the mechanism.
    #
    # Here rather than in the CLI because `start_worker` is what *every* route to a
    # running worker goes through — the CLI, the supervisor's launched process, and a
    # test. A check in one caller is a check one caller can be added around.
    from .execution.drivers import require_untrusted_posture, resolve_driver

    require_untrusted_posture(resolve_driver(), verify=False)

    if mediated is None:
        from .broker.protocol import broker_enabled

        mediated = broker_enabled()

    # Resolved BEFORE the mediation check, because the tenant scope decides `fork` and
    # the mediation check reads it. With the two the other way round, `--scope tenant`
    # with mediation on refused with "mediation was requested without --fork" — which
    # names the wrong flag: the operator asked for a scope that mandates forking, so
    # the diagnosis pointed at something they had no reason to type. Found by the e2e
    # in Phase 6, and it is the same class as every other ordering bug in this file:
    # a check that runs before the thing it checks has been decided.
    scope = resolve_scope(scope)
    if scope == SCOPE_TENANT and fork is None:
        # `--scope tenant` *implies* `--fork`, which is what the CLI help has always
        # said and what `drivers.worker_argv` has always rendered. The tri-state on
        # `fork` is what makes that possible without losing the refusal below it: an
        # omitted argument is a question the scope can answer, and `fork=False` passed
        # deliberately is a statement that contradicts the scope. Those deserve
        # opposite treatment, and a plain `bool` cannot tell them apart.
        fork = True
    fork = bool(fork)
    if mediated and not fork:
        raise RyaError(
            "E_BROKER_UNAVAILABLE",
            "Broker mediation was requested without --fork.",
            hint="Mediation puts a boundary between the claimer and the process that "
                 "imports the bundle. Inline mode has no such split — the claimer IS "
                 "that process — so there is nothing for a broker to mediate. This is "
                 "also why the untrusted posture cannot use inline mode.",
        )

    if scope == SCOPE_TENANT:
        return _start_tenant_claimer(
            project_root=project_root, store=store, workspace=workspace,
            environment=environment, cache_root=cache_root, fork=fork,
            mediated=mediated, prewarm=prewarm, t0=t0,
            run_timeout_seconds=run_timeout_seconds, **worker_kwargs)

    if version_id is None and environment is not None:
        from rya import deployments
        name = agent_name or load_manifest(project_root / "rya.agent.yaml").name
        version_id = deployments.resolve_for_run(store, environment, name)["id"]

    key = WorkerKey(workspace=workspace, agent=agent_name or "", version_id=version_id)
    root, version = resolve_bundle_root(store, key, project_root=project_root,
                                        cache_root=cache_root)
    # The manifest is read from the materialised tree, which is a YAML parse and
    # not an import — so even the fork claimer may do it. It is what names the key.
    manifest = load_manifest(root / "rya.agent.yaml")
    key.agent = manifest.name
    key.bundle_hash = version.get("bundleHash")

    if fork:
        from .execution.pool import FORK_AVAILABLE

        if not FORK_AVAILABLE:
            raise RyaError(
                "E_FORK_UNAVAILABLE",
                "Fork-per-run was requested but os.fork is not available here.",
                hint="Drop --fork: the in-process claimer is the same loop with the "
                     "import held in this process instead of a warm template.",
            )
        broker = config = None
        if mediated:
            broker, config = _start_broker(
                store=store, project_root=project_root, workspace=workspace,
                agent=manifest.name, manifest=manifest, environment=environment)
        executor = ForkExecutor(store=store, root=root, version=version,
                                workspace=workspace, environment=environment,
                                agent_name=manifest.name,
                                state_root=getattr(store, "root", None) or project_root,
                                run_timeout_seconds=run_timeout_seconds,
                                broker=broker, config=config)
        # Warm the template HERE, not lazily on the first drain, so that
        # `coldStartMs` measures the same span in both modes — materialise the
        # bundle, then import it — and so a bundle that will not import fails at
        # `start_worker` exactly as `load_agent` makes it fail inline.
        executor.template()
        worker = Worker(key=key, store=store, executor=executor, version=version,
                        **worker_kwargs)
    else:
        from .runtime import Engine, load_agent

        agent = load_agent(manifest, root)
        # The engine carries the version, so every run it creates is pinned to the
        # code that will execute it (D12) — the process IS the version (D3), so this
        # is resolved once here rather than re-resolved per run.
        engine = Engine(manifest, agent, store, root, version=version,
                        environment=environment)
        worker = Worker(engine, key, version=version, **worker_kwargs)

    worker.stats.coldStartMs = int((time.monotonic() - t0) * 1000)
    if worker.stats.coldStartMs > COLD_START_TARGET_MS:
        log.warning("cold start %dms exceeds the %dms target for %s — pre-warm this "
                    "key or raise the target deliberately",
                    worker.stats.coldStartMs, COLD_START_TARGET_MS, key.concurrency_key())
    return worker


def pin_run(run: dict, key: WorkerKey) -> dict:
    """Stamp the code identity onto a run (D12).

    ``agentVersion`` was only ever the author-typed `manifest.version` string —
    no hash, no immutability, no uniqueness. These three fields are what make a
    replay provably against the code that wrote the journal.
    """
    run["workspaceId"] = key.workspace
    if key.version_id:
        run["versionId"] = key.version_id
    if key.bundle_hash:
        run["bundleHash"] = key.bundle_hash
    return run
