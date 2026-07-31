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
    """The scheduling unit. One process serves exactly one of these."""

    workspace: str
    agent: str
    version_id: Optional[str] = None    # None = legacy working-tree mode
    bundle_hash: Optional[str] = None

    def concurrency_key(self) -> str:
        """The queue's fairness primitive (§6): one workspace must not starve
        another, so caps are applied per key rather than globally."""
        return f"{self.workspace}:{self.agent}:{self.version_id or 'local'}"

    def describe(self) -> dict:
        return {"workspace": self.workspace, "agent": self.agent,
                "versionId": self.version_id, "bundleHash": self.bundle_hash}


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
    store = bundles.resolve_bundle_store(project_root, root=cache_root)
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


class Worker:
    """One execution-plane instance.

    Owns ``ctx``, the journal and replay for the runs it executes, holds a
    tenant-scoped store handle, and claims from the queue. It is platform code,
    so ``ctx`` stays a LOCAL API — all 36 journaled operations remain in-process
    function calls (§5.2).
    """

    def __init__(self, engine, key: WorkerKey, *, version: Optional[dict] = None,
                 worker_id: Optional[str] = None,
                 idle_exit_seconds: float = DEFAULT_IDLE_EXIT_SECONDS,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 concurrency: int = 1,
                 turn_limit: int = 10):
        self.engine = engine
        self.key = key
        self.version = version or {}
        self.stats = WorkerStats()
        self.idle_exit_seconds = idle_exit_seconds
        self.poll_seconds = poll_seconds
        self.concurrency = max(1, concurrency)
        self.turn_limit = turn_limit
        self.id = worker_id or self._mint_id()
        self.handlers: dict = {}
        self._registered = False
        self._stop = False

    def _mint_id(self) -> str:
        from .store import _new_id
        return _new_id("wrk")

    # ---- startup -------------------------------------------------------
    def advertise(self) -> dict:
        """The handler set this process can actually serve."""
        agent = self.engine.agent
        return {
            "event": agent.event_handler() is not None,
            "jobs": sorted(getattr(agent, "_job_handlers", {})),
            "crons": sorted(getattr(agent, "_cron_handlers", {})),
            "tools": sorted(getattr(agent, "_tool_handlers", {})),
        }

    def preflight(self) -> dict:
        """Fail closed BEFORE claiming anything.

        §5.2: a worker "refuses to start if its registered handler set does not
        cover the tools its manifest version declares, so 'the image is missing a
        handler' surfaces at startup rather than mid-run."
        """
        self.handlers = self.advertise()
        missing = check_handler_set(self.engine.manifest, self.engine.agent)
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
        if declared and declared != self.engine.manifest.name:
            raise RyaError(
                "E_BUNDLE_MISMATCH",
                f"Version {self.key.version_id} is for agent '{declared}' but the "
                f"loaded bundle's manifest says '{self.engine.manifest.name}'.",
                hint="The version record and the bundle disagree; re-deploy rather than patching either.",
            )
        return self.handlers

    def register(self) -> dict:
        # §11.12: maxWorkers caps how much of the fleet one workspace can occupy.
        # Checked at registration — the only point where refusing costs nothing,
        # since the process has not claimed work yet and exiting loses no state.
        from .quotas import require_admission
        require_admission(self.engine.store, kind="worker")

        record = {
            "id": self.id,
            "workspaceId": self.key.workspace,
            "agent": self.key.agent,
            "versionId": self.key.version_id,
            "bundleHash": self.key.bundle_hash,
            "concurrencyKey": self.key.concurrency_key(),
            "handlers": self.handlers,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "coldStartMs": self.stats.coldStartMs,
            "sdkVersion": self.version.get("sdkVersion"),
        }
        reg = getattr(self.engine.store, "worker_register", None)
        if reg is not None:
            self._registered = True
            return reg(record)
        return record

    def heartbeat(self, **fields) -> None:
        beat = getattr(self.engine.store, "worker_heartbeat", None)
        if beat is None or not self._registered:
            return
        try:
            beat(self.id, stats=self.stats.__dict__, **fields)
        except Exception:  # a bookkeeping failure must not stop the work
            log.debug("worker heartbeat failed", exc_info=True)

    def deregister(self, reason: str) -> None:
        dereg = getattr(self.engine.store, "worker_deregister", None)
        if dereg is not None and self._registered:
            try:
                dereg(self.id, reason)
            except Exception:
                log.debug("worker deregister failed", exc_info=True)

    # ---- the loop ------------------------------------------------------
    def drain_once(self) -> dict:
        """One tick: reclaim + run due chat turns, then due jobs.

        Both are queue claims with leases, so N workers on the same key are safe
        and a process that dies mid-item has its work reclaimed (§6).
        """
        from . import turns as _turns

        ran_turns: list = []
        try:
            ran_turns = _turns.execute_pending(self.engine, worker_id=self.id,
                                               limit=self.turn_limit)
        except Exception:
            log.warning("turn drain failed", exc_info=True)
        ran_jobs: list = []
        try:
            ran_jobs = self.engine.work_once(concurrency=self.concurrency)
        except Exception:
            log.warning("job drain failed", exc_info=True)

        done = len(ran_turns) + len(ran_jobs)
        if done:
            self.stats.claimed += done
            self.stats.turns += len(ran_turns)
            self.stats.completed += sum(
                1 for j in ran_jobs if j.get("status") in ("completed", "waiting_approval"))
            self.stats.failed += sum(1 for j in ran_jobs if j.get("status") == "failed")
            self.stats.lastClaimAt = now_iso()
        return {"turns": ran_turns, "jobs": ran_jobs, "count": done}

    def queue_depth(self) -> int:
        """Pending work **this worker can actually claim** — what §6 scales on.

        Deliberately not ``queue_counts()["pending"]``. That counts every row in
        the queue table, and the table serves two product surfaces (D14): chat
        turns dispatched to `rya worker`, and the SDK-free ``/queue/*`` jobs that
        foreign consumers (a TypeScript DAG worker) claim for themselves. This
        worker only ever claims ``chat-turn`` (``turns.execute_pending``) plus the
        `jobs` primitive's due handlers.

        Counting foreign jobs here means one pending ``/queue/*`` job — work this
        process will never touch — reads as "busy" forever, so the idle window
        never elapses and scale-to-zero never fires. §6 opens by naming idle cost
        as *the* constraint for a fleet of N workspaces x M agents x V versions, so
        a worker that cannot go idle is the expensive failure, not a cosmetic one.

        Version pinning applies for the same reason it applies to claiming (D12): a
        turn pinned to another version is not this worker's work.
        """
        depth = 0
        listing = getattr(self.engine.store, "queue_list", None)
        if listing is not None:
            mine = self.key.version_id
            for job in listing("pending", "chat-turn") or []:
                pinned = queue.version_of(job)
                if mine is None or pinned is None or pinned == mine:
                    depth += 1
        try:
            depth += len(self.engine.due_jobs())
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
            log.info("worker %s down (%s): %s", self.id, reason, self.stats.__dict__)
        return {"workerId": self.id, "reason": reason, "iterations": iterations,
                "stats": self.stats.__dict__, **self.key.describe()}


def start_worker(*, project_root: Path, store, workspace: str = "default",
                 version_id: Optional[str] = None, environment: Optional[str] = None,
                 agent_name: Optional[str] = None,
                 cache_root: Optional[Path] = None, **worker_kwargs) -> Worker:
    """Build a ready-to-run :class:`Worker` for one (workspace, agent, version).

    ``version_id`` pins explicitly. ``environment`` resolves the current pointer
    for that environment instead (§9: new runs go to the promoted version). Both
    absent = legacy working-tree mode.
    """
    from .runtime import Engine, load_agent

    t0 = time.monotonic()

    if version_id is None and environment is not None:
        from rya import deployments
        name = agent_name or load_manifest(project_root / "rya.agent.yaml").name
        version_id = deployments.resolve_for_run(store, environment, name)["id"]

    key = WorkerKey(workspace=workspace, agent=agent_name or "", version_id=version_id)
    root, version = resolve_bundle_root(store, key, project_root=project_root,
                                        cache_root=cache_root)
    manifest = load_manifest(root / "rya.agent.yaml")
    key.agent = manifest.name
    key.bundle_hash = version.get("bundleHash")

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
