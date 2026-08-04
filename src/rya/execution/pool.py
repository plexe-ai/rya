"""Fork per run, from a warm interpreter keyed by bundle hash (D27).

The shape, and why it is this one::

    claimer  (rya worker --fork)   imports NO tenant code, ever
       │
       ├─ WarmPool[bundle_hash] ──► template process   imports the bundle ONCE
       │                               │
       │                               └─ fork() ──► child claims ONE item,
       │                                             executes it, exits
       └─ counts claimable depth, registers, heartbeats, idles out

**What is actually being decoupled.** D3 says a version-pinned run needs a process
that loaded only that version, because ``load_agent`` mutates ``sys.path`` and
never unwinds it. That is still true and nothing here changes it: a template holds
one bundle and a child of that template holds the same one. What D27 decouples is
the *container* from the *process* — the sandbox stops having to exist per
(workspace, agent, version) because a single sandbox can fork whichever version an
item is pinned to. Build import-at-startup instead and widening the claimer scope
later is a rewrite of ``worker.py``; that lock-in, not tenant-vs-triple, is what
this is for (#19).

**The child claims, rather than being handed a claim.** The tempting split is
"parent claims, child executes", and it is the wrong one: it would fork the
claim/execute pair apart, and every durability guarantee in ``turns.py`` — the
lease, the reclaim, the memoized replay — lives in that pair. So a child runs the
existing ``execute_pending``/``execute_resumes``/``work_once`` path verbatim with
``limit=1``. Claims are atomic (``SKIP LOCKED``), so N children racing on one key
is the case the design already covers, which means fork-per-run needed no new
concurrency reasoning at all.

**The pool is keyed by content, never by version id.** A pool is a cache, and a
cache that hands a run an interpreter which loaded different code is the same
hazard D9's content-keyed journal exists to catch, one layer down (§9 risk 9).
Content addressing is already the identity everywhere else — ``idx_versions_hash``,
``bundles.verify``, the unpacked-tree cache — so a version id here would be the
only place it was not.

**Every child builds its own store.** ``Engine._clone`` already says why: "a
psycopg connection must not be shared across threads." Sharing one across a *fork*
is worse than across a thread — two processes interleaving writes on one socket
corrupt the protocol stream rather than merely racing — so the template is
spawned rather than forked from the claimer (no inherited handles at all) and each
child opens a fresh worker-scoped store.

Not available everywhere: ``os.fork`` does not exist on Windows and is unsafe from
a threaded process. :data:`FORK_AVAILABLE` is the check, and the claimer falls
back to in-process execution when it is false — which is why D27 makes claimer
mode *configuration* rather than the only way to run.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import RyaError

log = logging.getLogger("rya.execution.pool")

FORK_AVAILABLE = hasattr(os, "fork")

# A child that never finishes wedges the claimer, because the parent waits for it.
# 0 disables the timeout, which is today's in-process behaviour and therefore the
# default: an unbounded handler is a pre-existing property of the platform, not
# something this module should start refusing. What the fork buys is that the
# timeout becomes *possible* — an in-process worker had no way to abandon a
# handler mid-call. When it fires, the item's queue lease expires and the ordinary
# reclaim path picks it up, so a killed child loses no work.
DEFAULT_RUN_TIMEOUT_SECONDS = 0.0

_STOP = {"op": "stop"}

# How many warm interpreters one claimer holds. See `WarmPool` for why the default
# depends on the claimer scope, and `default_pool_size` for the numbers.
POOL_MAX_ENTRIES_ENV = "RYA_POOL_MAX_ENTRIES"
DEFAULT_POOL_ENTRIES = 4
DEFAULT_TENANT_POOL_ENTRIES = 12


def default_pool_size(scope: str = "version", env=None) -> int:
    """The pool ceiling for ``scope``, overridable by the environment.

    An explicit env var wins at either scope, because an operator who has measured
    their own tenants knows something this function does not.
    """
    from .scope import SCOPE_TENANT

    source = env if env is not None else os.environ
    raw = (source.get(POOL_MAX_ENTRIES_ENV) or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("%s=%r is not a number; using the default", POOL_MAX_ENTRIES_ENV,
                        raw)
    return (DEFAULT_TENANT_POOL_ENTRIES if scope == SCOPE_TENANT
            else DEFAULT_POOL_ENTRIES)


@dataclass
class ForkOutcome:
    """What one forked child did. Shaped like ``Worker.drain_once``'s return."""

    turns: List[str] = field(default_factory=list)
    jobs: List[dict] = field(default_factory=list)
    resumes: List[str] = field(default_factory=list)
    count: int = 0
    exit_code: int = 0
    error: Optional[str] = None
    duration_ms: int = 0


# ---- the template process ---------------------------------------------------

def _open_store(cfg: dict, capability: str = ""):
    """The store a CHILD uses. Never the claimer's, never the template's.

    ``stateRoot`` is deliberately a separate config key from ``root``. They are
    different things and conflating them was a real bug in this module's first cut:
    ``root`` is the *unpacked bundle* — a content-addressed cache directory — while
    the state store lives beside the deployment. A child that built a ``FileStore``
    on the bundle root got a private, empty database, claimed nothing, and reported
    an idle tick that looked exactly like "no work to do".

    **Under D18 there is no state root and no DSN.** A mediated child gets a
    ``BrokerStore`` over the socket in ``cfg["brokerSocket"]``, authorised by the
    per-dispatch capability the claimer minted for *this* fork. The capability is a
    parameter rather than part of ``cfg`` because ``cfg`` is what the template holds,
    and the template is tenant-trust: a token sitting in it would be a token in the
    tenant's hands for every dispatch, not just its own.
    """
    socket_path = cfg.get("brokerSocket")
    if socket_path:
        from ..broker.client import BrokerClient, BrokerStore

        if not capability:
            raise RyaError(
                "E_CAPABILITY_INVALID",
                "A mediated child was started without a dispatch capability.",
                hint="The claimer mints one per fork. A child with none can reach the "
                     "broker's socket and authorise nothing, which is the intended "
                     "failure rather than a fallback to direct database access.",
            )
        client = BrokerClient(socket_path, capability)
        return BrokerStore(client, root=Path(cfg["root"]),
                           workspace=cfg.get("workspace") or "",
                           agent=cfg.get("agent") or "")
    from ..store import open_worker_store

    return open_worker_store(Path(cfg.get("stateRoot") or cfg["root"]),
                             cfg.get("workspace") or "default")


def _build_engine(cfg: dict, manifest, agent, capability: str = ""):
    from ..runtime import Engine

    store = _open_store(cfg, capability)
    return Engine(manifest, agent, store, Path(cfg["root"]),
                  version=cfg.get("version") or {}, environment=cfg.get("environment"),
                  broker=getattr(store, "_client", None) if cfg.get("brokerSocket") else None,
                  config=_child_config(cfg))


def _child_config(cfg: dict):
    """The stripped ``RunConfig`` a mediated child runs under.

    Only built in mediated mode; otherwise ``None`` lets ``RuntimeContext`` resolve
    its own from the environment exactly as before. Stripped means what it says: the
    routes keep their provider and model — a handler still sees which model it is
    talking to, and ``_params`` reads ``route.model`` to build the journal label — and
    lose ``api_key`` and ``base_url``. ``secrets`` is emptied because the broker hands
    those over one name at a time on request.

    Rebuilt here rather than shipped over from the claimer because a resolved
    ``RunConfig`` *contains* the keys, and serialising it into the template's ``cfg``
    would put them in the tenant's address space — which is the thing being prevented.
    """
    routes = cfg.get("routes") or {}
    if not cfg.get("brokerSocket"):
        return None
    from ..config import ModelRoute, RunConfig

    return RunConfig(
        values=dict(cfg.get("values") or {}),
        secrets={},
        routes={name: ModelRoute(provider=str(r.get("provider") or "mock"),
                                 model=str(r.get("model") or "mock-llm"),
                                 temperature=r.get("temperature"),
                                 max_tokens=r.get("maxTokens"),
                                 source="broker")
                for name, r in routes.items()},
        environment=cfg.get("environment") or "dev",
        source="broker",
    )


def _child_drain(cfg: dict, manifest, agent, req: dict) -> dict:
    """One item, in the forked child. Deliberately the ordinary drain path.

    Resumes before turns, for the same reason ``Worker.drain_once`` does it: a
    resume continues a run a human is already waiting on.
    """
    from .. import turns as _turns

    engine = _build_engine(cfg, manifest, agent, req.get("capability") or "")
    worker_id = req.get("workerId") or "fork"
    limit = int(req.get("limit") or 1)
    out: Dict[str, Any] = {"turns": [], "jobs": [], "resumes": []}
    out["resumes"] = _turns.execute_resumes(engine, worker_id=worker_id, limit=limit)
    if len(out["resumes"]) < limit:
        out["turns"] = _turns.execute_pending(engine, worker_id=worker_id,
                                              limit=limit - len(out["resumes"]))
    if not out["resumes"] and not out["turns"]:
        out["jobs"] = engine.work_once(concurrency=1)
    out["count"] = len(out["turns"]) + len(out["jobs"]) + len(out["resumes"])
    return out


def _template_main(conn, cfg: dict) -> None:  # pragma: no cover - runs in a child process
    """Import one bundle, then fork a child per request. Never claims anything.

    The template is the process that *has* the tenant's code and does nothing with
    it. Keeping it claim-free is what makes ``preflight`` still fail closed before
    anything is claimed: the claimer asks the template to advertise its handler
    set at boot, and a hole raises there — the same guarantee ``worker.py`` gives
    today, relocated to the process that can actually answer.
    """
    import faulthandler

    faulthandler.disable()
    # D18, and the ORDER is the point: scrub before importing the bundle. `spawn`
    # gives the child a copy of the parent's `os.environ`, and the parent is the
    # claimer, which holds the DSN and the keys. Nothing tenant-written has executed
    # yet at this line — `load_agent` below is the first — so removing them here
    # means the tenant's own code never sees them.
    #
    # This is defence in depth and it is honest about being second-best: the
    # credentials transited this process's memory, and CPython does not let a string
    # be unwritten. The place the exit criterion is genuinely met is the `docker` and
    # `kubernetes` drivers, which construct the sandbox's environment explicitly so
    # the values never arrive at all — which is why the untrusted posture requires
    # one of those (see `drivers.require_untrusted_posture`).
    scrubbed: List[str] = []
    if cfg.get("brokerSocket"):
        from ..broker.inventory import scrub_environment

        scrubbed = scrub_environment(os.environ)
    try:
        from ..manifest import load_manifest
        from ..runtime import load_agent
        from ..worker import check_handler_set

        t0 = time.monotonic()
        root = Path(cfg["root"])
        # The hash of the tree this process is ACTUALLY about to import, recomputed
        # from the bytes rather than echoed back from the request. Echoing would
        # make the pool's content check vacuous — it would only ever confirm that
        # the caller remembered what it asked for. `resolve_bundle_root` already
        # verified the unpacked tree, so this is not the primary defence; it is the
        # one that catches a POOL-KEYING bug specifically (§9 risk 9), which is the
        # failure the claimer's own verification cannot see.
        loaded_hash = None
        if cfg.get("bundleHash"):
            from ..bundles import build_bundle

            loaded_hash = build_bundle(root).hash
        manifest = load_manifest(root / "rya.agent.yaml")
        agent = load_agent(manifest, root)
        import_ms = int((time.monotonic() - t0) * 1000)
        conn.send({
            "ok": True,
            "importMs": import_ms,
            "bundleHash": loaded_hash,
            "agent": manifest.name,
            # Reported so the claimer can assert it rather than trust it — the
            # credential-inventory check reads this, and a template that scrubbed
            # nothing in mediated mode is a finding, not a detail.
            "scrubbed": scrubbed,
            "mediated": bool(cfg.get("brokerSocket")),
            "handlers": {
                "event": agent.event_handler() is not None,
                "jobs": sorted(getattr(agent, "_job_handlers", {})),
                "crons": sorted(getattr(agent, "_cron_handlers", {})),
                "tools": sorted(getattr(agent, "_tool_handlers", {})),
            },
            "missing": check_handler_set(manifest, agent),
        })
    except BaseException as exc:  # the claimer must learn WHY, not just time out
        try:
            conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            return

    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return
        if not req or req.get("op") == "stop":
            return
        if req.get("op") == "drain":
            # The dispatch capability rides on the REQUEST, not on `cfg`. It is
            # therefore reachable by tenant code only for the duration of a dispatch
            # that tenant legitimately received — which is what stops the template
            # from being a place a long-lived credential sits (D18).
            conn.send(_fork_and_wait(conn, cfg, manifest, agent, req))


def _fork_and_wait(conn, cfg, manifest, agent, req: dict) -> dict:  # pragma: no cover
    """Fork, run one item in the child, and report what it did.

    The result comes back over a per-request pipe rather than the template's
    ``conn``: the child must not write to a channel the template also owns, or a
    child that dies mid-write leaves a half-message that desynchronises every
    later request on that connection.
    """
    read_fd, write_fd = os.pipe()
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        # ---- child ----
        os.close(read_fd)
        code = 0
        try:
            conn.close()
        except Exception:
            pass
        try:
            payload = json.dumps(_child_drain(cfg, manifest, agent, req))
        except BaseException as exc:
            payload = json.dumps({"error": f"{type(exc).__name__}: {exc}", "count": 0})
            code = 1
        try:
            os.write(write_fd, payload.encode())
            os.close(write_fd)
        except Exception:
            code = 1
        # `os._exit`, never `sys.exit`: this process shares the template's buffers
        # and atexit handlers, and running them would flush the TEMPLATE's state
        # from a child that has no business owning it.
        os._exit(code)

    # ---- template (parent) ----
    os.close(write_fd)
    timeout = float(cfg.get("runTimeoutSeconds") or 0.0)
    chunks: List[bytes] = []
    # Read to EOF BEFORE reaping: a payload larger than the pipe buffer blocks the
    # child until it is drained, so waiting on the pid first would deadlock.
    with os.fdopen(read_fd, "rb") as fh:
        if timeout > 0:
            deadline = time.monotonic() + timeout
            os.set_blocking(fh.fileno(), False)
            while True:
                try:
                    chunk = fh.read()
                except BlockingIOError:
                    chunk = None
                if chunk:
                    chunks.append(chunk)
                    continue
                if chunk == b"":
                    break
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    break
                time.sleep(0.02)
        else:
            while True:
                chunk = fh.read()
                if not chunk:
                    break
                chunks.append(chunk)
    _, status = os.waitpid(pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    raw = b"".join(chunks)
    out: Dict[str, Any] = {"turns": [], "jobs": [], "resumes": [], "count": 0}
    if raw:
        try:
            out.update(json.loads(raw.decode()))
        except ValueError:
            out["error"] = f"child produced unreadable output: {raw[:200]!r}"
    elif exit_code != 0:
        # A child that died without writing anything is the case worth naming
        # precisely: -9 is the timeout above or an OOM kill, and an operator needs
        # to know which of those they are looking at.
        out["error"] = (f"the forked run died with exit code {exit_code} and wrote "
                        "no result" + (" (run timeout)" if exit_code == -9 and timeout else ""))
    out["exitCode"] = exit_code
    out["durationMs"] = int((time.monotonic() - t0) * 1000)
    return out


# ---- the claimer's side -----------------------------------------------------

class WarmTemplate:
    """A warm interpreter holding exactly one bundle hash.

    Spawned, not forked from the claimer. ``spawn`` costs an interpreter start
    (Phase 0 measured the whole path at 6.6–13.4 ms of *fork* for real agents,
    with import dominating) and buys a child with no inherited file descriptors,
    database connections or partially-initialised platform state. Forking the
    claimer would inherit its store connection into a process that has tenant code
    in it, which is the opposite of what the execution plane is for.
    """

    def __init__(self, *, bundle_hash: Optional[str], root: Path,
                 version: Optional[dict] = None, workspace: str = "default",
                 environment: Optional[str] = None,
                 state_root: Optional[Path] = None,
                 run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
                 start_timeout: float = 60.0,
                 broker=None, agent_name: Optional[str] = None,
                 routes: Optional[dict] = None,
                 broker_socket: str = "") -> None:
        # D18: the `BrokerServer` this template's children talk to. Held by the
        # CLAIMER — this object lives on the claimer's side; only the socket path
        # crosses into the template, and only a per-dispatch capability crosses into a
        # fork.
        self.broker = broker
        # D32: the same socket, named rather than held. On the split topology this
        # object lives inside the `TemplateHost`, which is in the sandbox and has no
        # `BrokerServer` to hold — it was told where one is. Keeping the two as
        # separate attributes rather than accepting "a broker or a path" is what stops
        # a host from accidentally acquiring the minting side: `self.broker` stays
        # None there, and `drain` refuses to mint without one.
        self.broker_socket = str(broker_socket or "")
        self.routes = routes or {}
        self.bundle_hash = bundle_hash
        self.root = Path(root)
        # Where `.rya`/the DSN is resolved from, which is NOT the bundle root. See
        # `_open_store`.
        self.state_root = Path(state_root) if state_root else Path(root)
        self.version = version or {}
        self.workspace = workspace
        self.environment = environment
        self.run_timeout_seconds = run_timeout_seconds
        self.start_timeout = start_timeout
        self.handlers: Dict[str, Any] = {}
        self.missing: List[str] = []
        self.import_ms = 0
        # Set from the manifest at handshake; seeded from the caller so a mediated
        # template can be told which agent it serves BEFORE it imports anything — the
        # broker forces that name onto every claim, so it cannot wait for the import
        # to discover it.
        self.agent_name: Optional[str] = agent_name
        self.scrubbed: List[str] = []
        self.mediated = broker is not None or bool(self.broker_socket)
        self._proc = None
        self._conn = None
        self.runs = 0
        self.dispatches = 0

    # ---- lifecycle -------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def _cfg(self) -> dict:
        """What the template process is told. Read this as the D18 manifest.

        In mediated mode ``stateRoot`` is dropped entirely rather than left in place
        and ignored: it is how ``_open_store`` finds a DSN, and a value that would
        work if the broker key were absent is a fallback nobody meant to build.
        ``routes`` carries provider and model names only — the shape a handler needs
        to know which model it is talking to, with the credential removed.
        """
        cfg: Dict[str, Any] = {"root": str(self.root),
                               "bundleHash": self.bundle_hash,
                               "version": self.version, "workspace": self.workspace,
                               "environment": self.environment,
                               "runTimeoutSeconds": self.run_timeout_seconds}
        socket_path = (str(self.broker.socket_path) if self.broker is not None
                       else self.broker_socket)
        if not socket_path:
            cfg["stateRoot"] = str(self.state_root)
            return cfg
        cfg["brokerSocket"] = socket_path
        cfg["agent"] = self.agent_name or ""
        cfg["routes"] = self.routes
        return cfg

    def start(self) -> dict:
        if self.alive:
            return {"handlers": self.handlers, "importMs": self.import_ms}
        if not FORK_AVAILABLE:
            raise RyaError(
                "E_FORK_UNAVAILABLE",
                "Fork-per-run needs os.fork, which this platform does not provide.",
                hint="Run the claimer in its in-process mode (`rya worker` without "
                     "--fork): one long-lived import bound to one version, which is "
                     "what the worker did before D27.",
            )
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        proc = ctx.Process(target=_template_main, args=(child_conn, self._cfg()),
                           daemon=True, name=f"rya-template-{(self.bundle_hash or 'local')[:12]}")
        proc.start()
        child_conn.close()
        self._proc, self._conn = proc, parent_conn
        if not parent_conn.poll(self.start_timeout):
            self.stop()
            raise RyaError(
                "E_TEMPLATE_START_FAILED",
                f"The warm interpreter for bundle {self.bundle_hash or '(working tree)'} "
                f"did not report ready within {self.start_timeout:.0f}s.",
                hint="That window covers materialising the bundle and importing the "
                     "agent. A tenant module that blocks at import time (a network "
                     "call, an input prompt) is the usual cause.",
            )
        try:
            ready = parent_conn.recv()
        except EOFError as exc:
            # `poll()` returns True on EOF too, so a template that died before
            # sending anything looks readable. Without this the caller gets a bare
            # EOFError from deep inside multiprocessing instead of a Rya error that
            # says which bundle and why.
            self.stop()
            raise RyaError(
                "E_TEMPLATE_START_FAILED",
                f"The warm interpreter for bundle {self.bundle_hash or '(working tree)'} "
                "exited before it reported ready.",
                hint="Usually the spawn itself failed: `multiprocessing` with the "
                     "spawn method re-imports the parent's __main__, so a caller "
                     "whose __main__ is not importable (a `python -c` script, a "
                     "REPL) cannot start one. `rya worker` and `python -m rya.cli` "
                     "both can.",
            ) from exc
        if not ready.get("ok"):
            self.stop()
            raise RyaError(
                "E_BUNDLE_IMPORT_FAILED",
                f"The bundle {self.bundle_hash or '(working tree)'} could not be "
                f"imported: {ready.get('error')}",
                hint="This is the tenant's entrypoint failing, not the platform. It "
                     "fails here — at claimer startup — rather than on whichever "
                     "request first reached it, which is the point of preflight.",
            )
        # The exit criterion, asserted rather than assumed: a template reports the
        # hash it actually loaded, and a pool entry that does not match what was
        # asked for is refused. Keyed by content means CHECKED against content.
        loaded = ready.get("bundleHash")
        if self.bundle_hash and loaded != self.bundle_hash:
            self.stop()
            raise RyaError(
                "E_POOL_HASH_MISMATCH",
                f"A warm interpreter asked for bundle {self.bundle_hash} loaded "
                f"{loaded} instead.",
                hint="A pool is a cache and this is the skew it must never serve: "
                     "handing a run an interpreter on different code is the hazard "
                     "D9's content-keyed journal exists to catch, one layer down.",
            )
        self.handlers = ready.get("handlers") or {}
        self.missing = list(ready.get("missing") or [])
        self.import_ms = int(ready.get("importMs") or 0)
        self.agent_name = ready.get("agent") or self.agent_name
        self.scrubbed = list(ready.get("scrubbed") or [])
        if self.mediated and not ready.get("mediated"):
            # The template did not take the mediated path, which means it built a
            # direct store from `stateRoot` — a credential in the tenant's process and
            # a silent downgrade of the whole posture. Refuse rather than serve.
            self.stop()
            raise RyaError(
                "E_BROKER_UNAVAILABLE",
                "A mediated warm interpreter reported that it is not mediated.",
                hint="The claimer passed a broker socket and the template did not use "
                     "it, so it opened a direct store instead. That is a downgrade of "
                     "the D18 boundary, not a degraded mode.",
            )
        log.info("warm interpreter up for %s (import %dms, handlers=%s, mediated=%s)",
                 self.bundle_hash or "(working tree)", self.import_ms, self.handlers,
                 self.mediated)
        return {"handlers": self.handlers, "importMs": self.import_ms,
                "missing": self.missing, "agent": self.agent_name,
                "mediated": self.mediated, "scrubbed": self.scrubbed}

    def stop(self, *, timeout: float = 5.0) -> None:
        conn, proc = self._conn, self._proc
        self._conn = self._proc = None
        if conn is not None:
            try:
                conn.send(_STOP)
            except (OSError, BrokenPipeError, ValueError):
                pass
            try:
                conn.close()
            except Exception:
                pass
        if proc is not None:
            proc.join(timeout)
            if proc.is_alive():
                proc.terminate()
                proc.join(1.0)

    # ---- work ------------------------------------------------------------
    def drain(self, *, limit: int = 1, worker_id: str = "fork",
              capability: str = "") -> ForkOutcome:
        """Fork one child, run up to ``limit`` items in it, and report.

        A dead template is not silently restarted here. The caller decides, because
        "the interpreter holding this tenant's code keeps dying" is a fact a
        supervisor should see rather than a retry loop should absorb.

        **``capability`` is D32's seam.** Passed in, this object forwards a token
        somebody else minted and does not release it; omitted, it mints from its own
        broker and releases when the dispatch is spent. The two callers are the two
        topologies: the claimer's own pool holds the broker and mints, and the
        :class:`~rya.execution.host.TemplateHost` — which is inside the sandbox and
        holds no signing secret — is handed one per drain. Making this a parameter
        rather than a second method is deliberate: the fork path either side of it is
        identical, and two methods would invite it not to stay that way.
        """
        if not self.alive or self._conn is None:
            raise RyaError(
                "E_TEMPLATE_NOT_RUNNING",
                f"The warm interpreter for {self.bundle_hash or '(working tree)'} is "
                "not running.",
                hint="Call start() first; if it keeps exiting, the bundle's import "
                     "is failing after having once succeeded.",
            )
        self.dispatches += 1
        dispatch = ""
        req = {"op": "drain", "limit": limit, "workerId": worker_id}
        if capability:
            # Forwarded, not minted. Nothing to release either: the claimer that minted
            # it is the only thing that knows when the dispatch is spent, and it
            # releases on its own side of the socket.
            req["capability"] = capability
        elif self.broker is not None:
            # One capability per dispatch. Minted here, in the claimer, and valid for
            # this fork only: the template holds it for the duration of the request
            # and the fork inherits it, so the longest a tenant-reachable token lives
            # is one item's execution.
            dispatch = f"{self.dispatches}:{worker_id}"
            req["capability"] = self.broker.mint(
                dispatch=dispatch,
                agent=self.agent_name or "",
                version_id=str((self.version or {}).get("id") or ""))
        self._conn.send(req)
        try:
            raw = self._conn.recv()
        except (EOFError, OSError) as exc:
            raise RyaError(
                "E_TEMPLATE_LOST",
                f"The warm interpreter for {self.bundle_hash or '(working tree)'} died "
                f"while a run was in flight: {exc}",
                hint="The item's queue lease expires and the ordinary reclaim path "
                     "re-runs it, so no work is lost — but a template that dies "
                     "under load is a resource limit, usually memory.",
            ) from exc
        finally:
            if dispatch:
                # The dispatch is over, so the authority it accumulated is too. The
                # broker cannot know this by itself — a fork can open and close several
                # connections during one item (a streaming model call needs a second),
                # so "the last socket closed" is not "the work finished". The claimer
                # minted the id and is the only thing that knows when it is spent.
                self.broker.release(dispatch)
        self.runs += 1
        return ForkOutcome(turns=list(raw.get("turns") or []),
                           jobs=list(raw.get("jobs") or []),
                           resumes=list(raw.get("resumes") or []),
                           count=int(raw.get("count") or 0),
                           exit_code=int(raw.get("exitCode") or 0),
                           error=raw.get("error"),
                           duration_ms=int(raw.get("durationMs") or 0))


class WarmPool:
    """``bundle_hash -> WarmTemplate``. The cache that keeps import off the path.

    Small on purpose. At the narrow claimer scope one claimer serves one
    (workspace, agent, version), so the pool holds **one** entry and its value is
    the import it does not repeat per run. The map exists because the wide scope
    (#19-8b) is a config change away, and at that scope one claimer holds an entry
    per hot version of every agent a tenant owns — the case where keying by hash
    stops being a formality and starts being what makes two live versions of one
    agent cost two entries instead of colliding on one.

    ``max_entries`` bounds it, evicting the least recently used. Unbounded, a
    tenant that promotes repeatedly accumulates one warm interpreter per historical
    hash and the memory is never reclaimed.

    **Sizing is a scope question, which is why the default moved in Phase 5.** At
    version scope one entry is used and the rest are headroom for a rollout. At
    tenant scope the working set is one entry per *hot version of every agent the
    tenant owns* — §7.1's worked example is 5 agents with 2 live versions, so a
    ceiling of 4 would evict and re-import continuously while every eviction looked
    like a cold start on somebody's next turn. :data:`POOL_MAX_ENTRIES_ENV` sets it;
    :func:`default_pool_size` picks the default from the scope.

    Note what the bound is *not*: a resource limit. Ten warm interpreters holding one
    tenant's code is real memory, and what stops that from being unbounded is the
    sandbox's own cgroup — which is per tenant at this scope, which is the trade §7.1
    records as "per-agent resource limits" being given up.
    """

    def __init__(self, *, max_entries: int = 4,
                 run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
                 host_socket: str = "", host_token: str = "") -> None:
        self.max_entries = max(1, max_entries)
        self.run_timeout_seconds = run_timeout_seconds
        # D32. Set, and every entry is a `HostedTemplate` in another container instead
        # of a `WarmTemplate` in this process. The pool itself is unchanged — same
        # keying, same LRU, same eviction — which is the point: the topology decides
        # *where* an interpreter is, and nothing about *which* interpreter serves what.
        self.host_socket = str(host_socket or "")
        self.host_token = str(host_token or "")
        self._entries: Dict[str, Any] = {}
        self._used: List[str] = []

    @property
    def hosted(self) -> bool:
        return bool(self.host_socket)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def hashes(self) -> List[str]:
        return list(self._entries)

    @property
    def templates(self) -> List[WarmTemplate]:
        """Every warm interpreter, in no particular order.

        What a tenant-scoped claimer reads to answer "what can I serve" — see
        ``ForkExecutor.advertise``. Deliberately the live objects rather than a
        snapshot of their handler sets: a template that has died since it warmed
        should read as dead, not as a stale advertisement.
        """
        return list(self._entries.values())

    def acquire(self, *, bundle_hash: Optional[str], root: Path,
                version: Optional[dict] = None, workspace: str = "default",
                environment: Optional[str] = None,
                state_root: Optional[Path] = None,
                broker=None, agent_name: Optional[str] = None,
                routes: Optional[dict] = None,
                broker_socket: str = "") -> Any:
        """The warm interpreter for this content, starting one if there is none.

        ``bundle_hash`` may be ``None`` — the working-tree mode `rya dev` uses,
        where there is no immutable artifact to address. That entry is keyed
        ``"local"`` and is the one case where the pool cannot promise the code has
        not changed underneath it, because the whole point of that mode is that it
        does. Anything with a hash is content-checked (see ``WarmTemplate.start``).
        """
        key = bundle_hash or "local"
        entry = self._entries.get(key)
        if entry is not None and entry.alive:
            self._touch(key)
            return entry
        if entry is not None:
            entry.stop()
        entry = self._build(bundle_hash=bundle_hash, root=root, version=version,
                            workspace=workspace, environment=environment,
                            state_root=state_root, broker=broker,
                            agent_name=agent_name, routes=routes,
                            broker_socket=broker_socket)
        entry.start()
        self._entries[key] = entry
        self._touch(key)
        self._evict()
        return entry

    def _build(self, **kw) -> Any:
        """One place the topology is chosen, so `acquire` never has to know it."""
        if not self.hosted:
            return WarmTemplate(run_timeout_seconds=self.run_timeout_seconds, **kw)
        from .host import HostedTemplate

        return HostedTemplate(socket_path=self.host_socket, token=self.host_token,
                              run_timeout_seconds=self.run_timeout_seconds, **kw)

    def get(self, key: str) -> Optional[Any]:
        """The entry under ``key``, touched. What a :class:`TemplateHost` drains through.

        Touching on a *read* is right here and would be wrong on an inspection helper:
        the host's only reason to look an entry up is to dispatch to it, so a lookup is
        a use. Without it the LRU would order by warm time and evict the tenant's
        busiest agent first.
        """
        entry = self._entries.get(key)
        if entry is not None:
            self._touch(key)
        return entry

    def drop(self, key: str) -> bool:
        """Stop and forget one entry. ``True`` if there was one."""
        entry = self._entries.pop(key, None)
        if key in self._used:
            self._used.remove(key)
        if entry is None:
            return False
        entry.stop()
        return True

    def _touch(self, key: str) -> None:
        if key in self._used:
            self._used.remove(key)
        self._used.append(key)

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            oldest = self._used.pop(0)
            victim = self._entries.pop(oldest, None)
            if victim is not None:
                log.info("evicting warm interpreter %s (pool holds %d)", oldest,
                         len(self._entries))
                victim.stop()

    def close(self) -> None:
        for entry in list(self._entries.values()):
            entry.stop()
        self._entries.clear()
        self._used.clear()
