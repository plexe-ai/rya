"""The template host: the sandbox's own side of the D32 split.

D32 decided the broker is a *sibling* of the tenant process, never its parent and
never inside the tenant's container. §7.2 then named the one thing that blocked it::

    the claimer *spawns* the template with `multiprocessing`, so the template is
    necessarily its child, so the two are necessarily in the same container.

This module removes that "necessarily". A template host is a small, credential-free,
platform-trust process that runs **inside** the sandbox and accepts *"import this
bundle and serve forks of it"* over a Unix socket. With it, the launched unit for the
untrusted posture is the pair D32 describes::

    ┌─ claimer container ────────────┐   ┌─ sandbox container (gVisor) ──────────┐
    │ rya worker --fork              │   │ rya template-host                     │
    │  · holds the DSN, the seal key │   │  · holds NOTHING                      │
    │  · runs the BrokerServer  ─────┼──▶│  · spawns templates, forks per run    │
    │  · mints one capability/fork   │◀──┼──── the forks call back to the broker │
    └────────────────────────────────┘   └───────────────────────────────────────┘
      broker.sock and host.sock both live on the shared in-memory volume

**Two sockets, pointing opposite ways, and that is the whole design.** The claimer
drives the host (control: start a template, fork it, stop it) and the forks call the
broker (data: claim, journal, model). Neither socket lets its caller become the other
side. A tenant fork that reaches the *host* socket can ask for imports of bundles
already in its own sandbox — no escalation — and a token stops it driving the control
surface at all. A tenant fork that reaches the *broker* socket gets exactly the D18
allowlist it already had.

**The host is credential-free by wire format, not by filtering.** :class:`StartRequest`
has no field for a state root and no field for a DSN, so there is no value the claimer
*could* send that would give the host a database — which is a stronger statement than
"we remember not to send one". ``WarmTemplate._cfg`` already drops ``stateRoot`` in
mediated mode; here the drop is structural. :func:`_refuse_credentials` is the
belt-and-braces pass over what does arrive, and it exists for the case the schema grows
a field somebody did not think about.

**The host does not mint capabilities.** It forwards the one the claimer minted for
that fork. The HMAC secret never leaves the broker process, so a compromised host can
replay a live capability into the dispatch it was already given and can forge nothing —
which is the same bound a compromised *template* already has, and templates run tenant
code. That is the point: putting platform code with no secrets inside the sandbox costs
nothing, and it is what makes the arrangement work at all (§7.2).

**Why not one container of templates per bundle.** §7.2 answered this and it is worth
keeping next to the code: launching a sandbox container per bundle reintroduces exactly
what D27 collapsed, because the warm pool holds one interpreter per hot version and
those would become one *container* per hot version. One host, many templates, one
sandbox — the N×M×V product stays collapsed.
"""

from __future__ import annotations

import hmac
import logging
import os
import socket
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..broker import protocol as proto
from ..errors import RyaError

log = logging.getLogger("rya.execution.host")

# Where the host listens. Set on the CLAIMER, and its presence is what switches the
# pool from spawning templates itself to driving a host — so the weak topology (the
# claimer as the template's parent) stays the default and the split is opted into by
# the driver that renders it.
HOST_SOCKET_ENV = "RYA_TEMPLATE_HOST"
# The shared secret for the control surface. Not a credential in the D18 sense: it
# authorises "ask this host to import a bundle", which grants no platform access at
# all. It is nonetheless scrubbed from a template's environment before the bundle is
# imported (see `broker/inventory.PLATFORM_GROUPS`), because a tenant that can drive
# the control surface can stop its own sandbox serving, and availability is a property
# too.
HOST_TOKEN_ENV = "RYA_TEMPLATE_HOST_TOKEN"

E_HOST_UNAVAILABLE = "E_TEMPLATE_HOST_UNAVAILABLE"
E_HOST_DENIED = "E_TEMPLATE_HOST_DENIED"
E_HOST_CREDENTIALED = "E_TEMPLATE_HOST_CREDENTIALED"
E_HOST_UNMEDIATED = "E_TEMPLATE_HOST_UNMEDIATED"

PROTOCOL_VERSION = 1

OP_HELLO = "hello"
OP_START = "start"
OP_DRAIN = "drain"
OP_STOP = "stop"
OP_STATUS = "status"

# How long a control op may take. A `start` covers materialising nothing (the bundle is
# already on the shared volume) and importing the tenant's module, so it shares
# `WarmTemplate.start_timeout`'s budget. A `drain` is deliberately NOT bounded here —
# it is one item's execution, which is bounded by the queue lease and by
# `runTimeoutSeconds`, and a second unrelated deadline in the middle would turn a slow
# handler into a lost run.
CONTROL_TIMEOUT = 90.0


def host_socket(env: Optional[Dict[str, str]] = None) -> str:
    src = env if env is not None else os.environ
    return (src.get(HOST_SOCKET_ENV) or "").strip()


def host_token(env: Optional[Dict[str, str]] = None) -> str:
    src = env if env is not None else os.environ
    return (src.get(HOST_TOKEN_ENV) or "").strip()


def hosted_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Whether this claimer should drive a host instead of spawning templates itself."""
    return bool(host_socket(env))


# ---- the request the claimer may make ---------------------------------------

# Everything the host is allowed to be told. Read this as the credential-free contract:
# a field absent from here cannot reach a `WarmTemplate` the host builds, and the two
# fields most conspicuously absent are `stateRoot` (how `_open_store` finds a DSN) and
# anything carrying a provider key.
START_FIELDS = frozenset({
    "bundleHash", "root", "version", "workspace", "environment",
    "agent", "routes", "brokerSocket", "runTimeoutSeconds",
})


# The two fields a `ModelRoute` carries that `public_routes` exists to strip. Named
# exactly, because this is the leak the walk below is actually for: a route is a nested
# object, so a flat check over `START_FIELDS` would not see them.
_ROUTE_SECRETS = frozenset({"api_key", "apiKey", "base_url", "baseUrl", "secrets",
                            "headers", "authorization"})


def _refuse_credentials(payload: Any, *, where: str = "request") -> None:
    """Walk a start request and refuse anything credential-shaped.

    Defence in depth over :data:`START_FIELDS`, aimed at one specific regression: a
    future field added to the start request that happens to carry a key.

    **Only the deterministic rules apply here, and that is the correction.** The first
    cut ran `broker/inventory.classify` and refused its `ambiguous` bucket too, which
    rejected a perfectly ordinary route because ``maxTokens`` contains the substring
    "TOKEN". That bucket exists for the opposite situation — inventory.py's own words
    are "Ambiguous names are **not** removed… a shape-based heuristic is not a good
    enough reason to break a handler" — and it exists because an environment is an open
    set of unknown names where a human should decide. A wire schema is a closed set
    this module defines, so the heuristic has nothing to add and a false positive here
    is not a warning a human reads, it is a tenant whose agent will not warm.

    What is refused: an exact platform variable name, a DSN-shaped value under any key
    at all (inventory's rule that "a connection string is a credential whatever it is
    called"), and the named route fields above.
    """
    from ..broker.inventory import CLASS_PLATFORM, classify

    if isinstance(payload, dict):
        for key, value in payload.items():
            name = str(key)
            reason = ""
            if name in _ROUTE_SECRETS:
                reason = "a model route's credential, which `public_routes` strips"
            else:
                cls, group = classify(name, value if isinstance(value, str) else None)
                if cls == CLASS_PLATFORM:
                    reason = f"a platform credential ({group or 'unclassified'})"
            if reason:
                raise RyaError(
                    E_HOST_CREDENTIALED,
                    f"A template host was sent '{name}' in its {where}: {reason}.",
                    hint="The host runs inside the sandbox and holds nothing. A "
                         "credential arriving here would put a platform secret one "
                         "process away from tenant code, which is the arrangement D32 "
                         "exists to prevent. Send provider and model names only — "
                         "`BrokerServer.public_routes` is the projection that strips "
                         "the key.")
            _refuse_credentials(value, where=where)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _refuse_credentials(item, where=where)


@dataclass
class StartRequest:
    """What the claimer tells the host about one bundle. No secrets, by construction."""

    bundle_hash: Optional[str] = None
    root: str = ""
    version: dict = field(default_factory=dict)
    workspace: str = "default"
    environment: Optional[str] = None
    agent: str = ""
    routes: dict = field(default_factory=dict)
    broker_socket: str = ""
    run_timeout_seconds: float = 0.0

    def wire(self) -> dict:
        return {"bundleHash": self.bundle_hash, "root": self.root,
                "version": self.version, "workspace": self.workspace,
                "environment": self.environment, "agent": self.agent,
                "routes": self.routes, "brokerSocket": self.broker_socket,
                "runTimeoutSeconds": self.run_timeout_seconds}

    @classmethod
    def read(cls, payload: dict) -> "StartRequest":
        """Parse, refusing unknown fields rather than ignoring them.

        An ignored field is how a claimer and a host end up disagreeing about what was
        asked for — the same class of skew ``E_POOL_HASH_MISMATCH`` refuses one layer
        down. Here it is worse than skew: the unknown field a newer claimer sends might
        be the one that turns mediation off.
        """
        unknown = sorted(set(payload) - START_FIELDS)
        if unknown:
            raise RyaError(
                E_HOST_DENIED,
                f"A template host was sent field(s) it does not understand: "
                f"{', '.join(unknown)}.",
                hint="The host and the claimer are the same build (D5), so this is "
                     "either a version skew between the two containers or a caller "
                     "that is not the claimer. Both are refused rather than partly "
                     "honoured.")
        _refuse_credentials(payload, where="start request")
        if not payload.get("brokerSocket"):
            raise RyaError(
                E_HOST_UNMEDIATED,
                "A template host was asked to start an unmediated template.",
                hint="The host only exists to serve the untrusted posture, where the "
                     "template reaches the platform through the broker. Without a "
                     "broker socket the template would need a database credential — "
                     "which the host does not have and must not be given. Run the "
                     "claimer's own in-process pool for the trusted posture.")
        return cls(bundle_hash=payload.get("bundleHash"),
                   root=str(payload.get("root") or ""),
                   version=dict(payload.get("version") or {}),
                   workspace=str(payload.get("workspace") or "default"),
                   environment=payload.get("environment"),
                   agent=str(payload.get("agent") or ""),
                   routes=dict(payload.get("routes") or {}),
                   broker_socket=str(payload.get("brokerSocket") or ""),
                   run_timeout_seconds=float(payload.get("runTimeoutSeconds") or 0.0))


# ---- the host ---------------------------------------------------------------

class TemplateHost:
    """Serves templates over a socket. Runs in the sandbox. Holds nothing.

    Owns a :class:`~rya.execution.pool.WarmPool` and exposes it — which is the reason
    this class is small. The pool already knows how to spawn a template, verify the
    hash it loaded, report its handler set and fork per run; putting a socket in front
    of it is the entire delta between the weak topology and the good one. Nothing about
    D27's fork-per-run changes, and that is the payoff from having built the pool
    keyed by content rather than by container.

    **One thread per connection, and one connection per template.** The claimer's
    :class:`HostedTemplate` keeps its own connection for the life of the template, so a
    drain on one group does not serialise behind a drain on another — matching the
    per-template pipe the in-process pool gives it. The alternative, multiplexing every
    template over one connection, would need request ids and would make a slow handler
    on one agent a latency problem for the tenant's other agents.
    """

    def __init__(self, *, socket_path: Path, token: str = "",
                 max_entries: int = 12,
                 run_timeout_seconds: float = 0.0) -> None:
        from .pool import WarmPool

        self.path = Path(socket_path)
        self.token = token or host_token()
        self.pool = WarmPool(max_entries=max_entries,
                             run_timeout_seconds=run_timeout_seconds)
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conns: List[socket.socket] = []
        self._lock = threading.Lock()
        self.started_at = 0.0
        self.serves = 0

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> Path:
        """Bind, listen and serve in a background thread.

        The socket's *directory* is not hardened here, unlike ``BrokerServer.start``,
        and the difference is deliberate: the broker creates its own 0700 temp dir
        because it chooses where to listen, while the host is told where by the
        operator — it is a shared volume between two containers, and a host that
        chmod'd it would be changing a mount the claimer also uses. The socket file
        itself is 0600, and the volume's mode is the substrate's business (an
        ``emptyDir`` is already private to the pod).
        """
        import time as _time

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self._sock.listen(16)
        self._sock.settimeout(0.25)
        self._stop.clear()
        self.started_at = _time.time()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="rya-template-host", daemon=True)
        self._thread.start()
        log.info("template host listening on %s (pool max %d)", self.path,
                 self.pool.max_entries)
        return self.path

    def close(self) -> None:
        """Stop serving, then stop the templates. Order matters, same as the claimer's.

        `ForkExecutor.close` learned this in Phase 5 — stop the pool before the thing
        the pool's children talk to — and the host is the mirror image: stop
        *accepting* first so no new template is started while the pool is being torn
        down, then stop the templates, then drop the socket.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            conns, self._conns = list(self._conns), []
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        self.pool.close()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:  # pragma: no cover
                pass

    def __enter__(self) -> "TemplateHost":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def serve_forever(self) -> None:  # pragma: no cover - the CLI's blocking call
        """What `rya template-host` runs. Blocks until interrupted."""
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    # ---- serving ---------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    req = proto.recv_frame(conn)
                except (EOFError, OSError, RyaError):
                    return
                self.serves += 1
                seq = int(req.get("seq") or 0)
                try:
                    result = self._dispatch(req)
                    proto.send_frame(conn, proto.ok(seq, result))
                except BaseException as exc:  # noqa: BLE001 - errors cross as data
                    log.warning("template host refused %s: %s", req.get("op"), exc)
                    try:
                        proto.send_frame(conn, proto.err(seq, exc))
                    except OSError:
                        return
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _authorize(self, req: dict) -> None:
        """Constant-time token check on every op, including ``hello``.

        Checked on ``hello`` too so a caller learns immediately rather than after it
        has described a bundle — and checked on every *subsequent* op rather than once
        per connection because "authenticated the connection" is a state a bug can
        reach by accident, and re-checking a token the caller already holds costs
        nothing.
        """
        if not self.token:
            raise RyaError(
                E_HOST_DENIED,
                "This template host has no token, so it refuses every request.",
                hint=f"Set {HOST_TOKEN_ENV} on the host and on the claimer. A host "
                     "with no token would accept control requests from any process "
                     "that can reach its socket, and the processes that can reach it "
                     "include the tenant's own forks.")
        if not hmac.compare_digest(str(req.get("token") or ""), self.token):
            raise RyaError(
                E_HOST_DENIED,
                "Template host token does not match.",
                hint="The claimer and the host are configured from the same secret. A "
                     "mismatch is either a misconfigured pair or a process that is not "
                     "the claimer asking the host to import something.")

    def _dispatch(self, req: dict) -> Any:
        op = str(req.get("op") or "")
        self._authorize(req)
        if op == OP_HELLO:
            return {"ok": True, "version": PROTOCOL_VERSION, "pid": os.getpid(),
                    "maxEntries": self.pool.max_entries}
        if op == OP_STATUS:
            return self.status()
        if op == OP_START:
            return self._start_template(StartRequest.read(dict(req.get("start") or {})))
        if op == OP_DRAIN:
            return self._drain(req)
        if op == OP_STOP:
            return self._stop_template(str(req.get("key") or ""))
        raise RyaError(
            E_HOST_DENIED, f"A template host does not serve the op '{op}'.",
            hint=f"It serves {OP_HELLO}, {OP_START}, {OP_DRAIN}, {OP_STOP} and "
                 f"{OP_STATUS}. The data surface is the broker's, on a different "
                 "socket and in the other direction.")

    def _start_template(self, req: StartRequest) -> dict:
        """Warm one bundle. The reply is the preflight the claimer would have done itself.

        Note what crosses back: the handler set, the missing tools, the import time and
        the *scrubbed variable names*. That last one is why `WarmTemplate.start`
        reports it at all — the claimer asserts the template scrubbed rather than
        trusting it — and the assertion has to survive the extra hop, or the split
        topology would be the one arrangement where nobody checks.
        """
        template = self.pool.acquire(
            bundle_hash=req.bundle_hash, root=Path(req.root), version=req.version,
            workspace=req.workspace, environment=req.environment,
            broker_socket=req.broker_socket, agent_name=req.agent,
            routes=req.routes)
        return {"key": req.bundle_hash or "local",
                "handlers": template.handlers, "missing": template.missing,
                "importMs": template.import_ms, "agent": template.agent_name,
                "bundleHash": template.bundle_hash,
                "scrubbed": template.scrubbed, "mediated": template.mediated}

    def _drain(self, req: dict) -> dict:
        key = str(req.get("key") or "")
        template = self.pool.get(key)
        if template is None or not template.alive:
            raise RyaError(
                "E_TEMPLATE_NOT_RUNNING",
                f"The template host holds no live template for '{key}'.",
                hint="It was evicted (the pool is bounded) or it died. The claimer "
                     "starts it again on the next dispatch; the item's queue lease "
                     "expires and the ordinary reclaim path re-runs it, so nothing is "
                     "lost.")
        outcome = template.drain(limit=int(req.get("limit") or 1),
                                 worker_id=str(req.get("workerId") or "fork"),
                                 capability=str(req.get("capability") or ""))
        return {"turns": outcome.turns, "jobs": outcome.jobs,
                "resumes": outcome.resumes, "count": outcome.count,
                "exitCode": outcome.exit_code, "error": outcome.error,
                "durationMs": outcome.duration_ms}

    def _stop_template(self, key: str) -> dict:
        return {"ok": self.pool.drop(key)}

    def status(self) -> dict:
        return {"pid": os.getpid(), "socket": str(self.path),
                "templates": [{"bundleHash": t.bundle_hash, "agent": t.agent_name,
                               "alive": t.alive, "runs": t.runs,
                               "importMs": t.import_ms}
                              for t in self.pool.templates],
                "serves": self.serves, "maxEntries": self.pool.max_entries}


# ---- the claimer's side -----------------------------------------------------

class HostedTemplateProbe:
    """One-shot ``status``, for `rya template-host --status` and the e2e.

    Separate from :class:`HostedTemplate` because it asks a different question and
    needs none of the answer: a probe has no bundle, no broker and no capability to
    mint, and giving it those so it could reuse the class would mean an operator
    needed a broker running to ask a host what it is holding.
    """

    def __init__(self, socket_path: str, token: str = "") -> None:
        self.socket_path = socket_path or host_socket()
        self.token = token or host_token()

    def _once(self, op: str, payload: Optional[dict] = None) -> Any:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise RyaError(
                E_HOST_UNAVAILABLE,
                f"No template host is listening on {self.socket_path}: {exc}",
                hint="Start one with `rya template-host`, or check that this process "
                     "shares the volume the socket lives on.") from exc
        try:
            proto.send_frame(sock, {"v": PROTOCOL_VERSION, "seq": 1, "op": op,
                                    "token": self.token, **(payload or {})})
            reply = proto.recv_frame(sock)
        finally:
            sock.close()
        if not reply.get("ok"):
            proto.raise_from_error(reply)
        return reply.get("result")

    def hello(self) -> dict:
        return dict(self._once(OP_HELLO) or {})

    def status(self) -> dict:
        return dict(self._once(OP_STATUS) or {})


class HostedTemplate:
    """A :class:`~rya.execution.pool.WarmTemplate` that lives in another container.

    Duck-typed against ``WarmTemplate`` on purpose rather than sharing a base class:
    the two have almost no implementation in common — one spawns a process, the other
    opens a socket — and the thing that must stay identical is the *interface the
    executor uses*, which a base class full of ``NotImplementedError`` would document
    less clearly than this sentence does. ``tests/test_template_host.py`` asserts the
    surfaces match, so the duck typing is checked rather than assumed.

    The capability is still minted **here**, in the claimer, by the broker that holds
    the signing secret. The host is a courier for it.
    """

    def __init__(self, *, bundle_hash: Optional[str], root: Path,
                 version: Optional[dict] = None, workspace: str = "default",
                 environment: Optional[str] = None,
                 broker=None, agent_name: Optional[str] = None,
                 routes: Optional[dict] = None,
                 socket_path: str = "", token: str = "",
                 run_timeout_seconds: float = 0.0,
                 start_timeout: float = CONTROL_TIMEOUT,
                 state_root: Optional[Path] = None,
                 broker_socket: str = "") -> None:
        # Accepted and dropped, both of them, and named rather than swallowed by a
        # `**kwargs` so the discard is visible. `WarmPool.acquire` passes the claimer's
        # state root and possibly a broker socket to every template it builds; on this
        # topology the first is a path to a database this container cannot and must not
        # reach, and the second is the broker's own — which `start` reads from
        # `self.broker` instead, because the object that HOLDS the broker is the only
        # honest source for where it is listening.
        del state_root, broker_socket
        if broker is None:
            raise RyaError(
                E_HOST_UNMEDIATED,
                "A hosted template needs a broker, and none was given.",
                hint="The split topology exists to serve the untrusted posture. "
                     "Without a broker the template in the sandbox would have no way "
                     "to reach the platform at all — the host deliberately cannot give "
                     "it one.")
        self.broker = broker
        self.bundle_hash = bundle_hash
        self.root = Path(root)
        self.version = version or {}
        self.workspace = workspace
        self.environment = environment
        self.agent_name = agent_name
        self.routes = routes or {}
        self.run_timeout_seconds = run_timeout_seconds
        self.start_timeout = start_timeout
        self.socket_path = socket_path or host_socket()
        self.token = token or host_token()
        self.handlers: Dict[str, Any] = {}
        self.missing: List[str] = []
        self.import_ms = 0
        self.scrubbed: List[str] = []
        self.mediated = True
        self.runs = 0
        self.dispatches = 0
        self.key = bundle_hash or "local"
        self._sock: Optional[socket.socket] = None
        self._seq = 0
        self._alive = False

    # ---- the wire --------------------------------------------------------
    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        if not self.socket_path:
            raise RyaError(
                E_HOST_UNAVAILABLE,
                "No template host socket is configured.",
                hint=f"Set {HOST_SOCKET_ENV} to the shared path the sandbox container's "
                     "`rya template-host` is listening on.")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise RyaError(
                E_HOST_UNAVAILABLE,
                f"Could not reach the template host at {self.socket_path}: {exc}",
                hint="The sandbox container is not up, or the two containers do not "
                     "share the volume the socket lives on. In Kubernetes that is the "
                     "`broker` emptyDir; with `docker run` it is the bind mount both "
                     "halves of the pair are given.") from exc
        self._sock = sock
        return sock

    def _call(self, op: str, payload: Optional[dict] = None, *,
              timeout: Optional[float] = CONTROL_TIMEOUT) -> Any:
        sock = self._connect()
        self._seq += 1
        frame = {"v": PROTOCOL_VERSION, "seq": self._seq, "op": op,
                 "token": self.token, **(payload or {})}
        previous = sock.gettimeout()
        try:
            sock.settimeout(timeout)
            proto.send_frame(sock, frame)
            # The response deadline is the caller's, not the frame's: `recv_frame`'s
            # body timeout guards a half-sent frame, and this guards a host that
            # accepted the request and never answered. `timeout=None` on a drain is
            # what makes a ten-minute handler work.
            reply = proto.recv_frame(sock)
        except (EOFError, OSError) as exc:
            self.close_socket()
            raise RyaError(
                E_HOST_UNAVAILABLE,
                f"The template host closed the connection during '{op}': {exc}",
                hint="A host that dies mid-dispatch loses no work — the item's queue "
                     "lease expires and the reclaim path re-runs it — but a host that "
                     "dies repeatedly is a resource limit in the sandbox, usually "
                     "memory, and the pool inside it is the thing to size.") from exc
        finally:
            if self._sock is not None:
                try:
                    self._sock.settimeout(previous)
                except OSError:  # pragma: no cover - socket already closed
                    pass
        if not reply.get("ok"):
            proto.raise_from_error(reply)
        return reply.get("result")

    def close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ---- the WarmTemplate surface ----------------------------------------
    @property
    def alive(self) -> bool:
        return self._alive and self._sock is not None

    def start(self) -> dict:
        req = StartRequest(bundle_hash=self.bundle_hash, root=str(self.root),
                           version=self.version, workspace=self.workspace,
                           environment=self.environment, agent=self.agent_name or "",
                           routes=self.routes,
                           broker_socket=str(self.broker.socket_path),
                           run_timeout_seconds=self.run_timeout_seconds)
        # Refused on the way out as well as on the way in. The claimer is the process
        # that HAS the credentials, so it is the one that can leak them, and finding
        # that here names the caller rather than the wire.
        _refuse_credentials(req.wire(), where="outgoing start request")
        ready = self._call(OP_START, {"start": req.wire()}, timeout=self.start_timeout)
        self.key = str(ready.get("key") or self.key)
        self.handlers = dict(ready.get("handlers") or {})
        self.missing = list(ready.get("missing") or [])
        self.import_ms = int(ready.get("importMs") or 0)
        self.agent_name = ready.get("agent") or self.agent_name
        self.scrubbed = list(ready.get("scrubbed") or [])
        loaded = ready.get("bundleHash")
        if self.bundle_hash and loaded != self.bundle_hash:
            # The same assertion `WarmTemplate.start` makes, repeated across the hop
            # rather than delegated to it. A pool-keying bug on the host's side is
            # invisible to the host — it would be confirming what it was asked for —
            # and this is the only place the two views of "which bundle" can disagree.
            self.stop()
            raise RyaError(
                "E_POOL_HASH_MISMATCH",
                f"A hosted template asked for bundle {self.bundle_hash} loaded "
                f"{loaded} instead.")
        if not ready.get("mediated"):
            self.stop()
            raise RyaError(
                proto.E_UNAVAILABLE,
                "A hosted template reported that it is not mediated.",
                hint="The host was given a broker socket and the template did not use "
                     "it. On the split topology that is not a downgrade to a direct "
                     "store — the sandbox has no database to reach — it is a template "
                     "that will fail every call, so it is refused at warm time.")
        self._alive = True
        log.info("hosted template up for %s (import %dms, agent=%s)",
                 self.bundle_hash or "(working tree)", self.import_ms, self.agent_name)
        return {"handlers": self.handlers, "importMs": self.import_ms,
                "missing": self.missing, "agent": self.agent_name,
                "mediated": True, "scrubbed": self.scrubbed}

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._sock is not None:
            try:
                self._call(OP_STOP, {"key": self.key}, timeout=timeout)
            except RyaError:
                pass
        self._alive = False
        self.close_socket()

    def drain(self, *, limit: int = 1, worker_id: str = "fork",
              capability: str = "") -> "Any":
        """Mint here, execute there. The dispatch's authority still ends here too."""
        from .pool import ForkOutcome

        if not self.alive:
            raise RyaError(
                "E_TEMPLATE_NOT_RUNNING",
                f"The hosted template for {self.bundle_hash or '(working tree)'} is "
                "not running.")
        self.dispatches += 1
        dispatch = f"{self.dispatches}:{worker_id}"
        token = capability or self.broker.mint(
            dispatch=dispatch, agent=self.agent_name or "",
            version_id=str((self.version or {}).get("id") or ""))
        try:
            # No timeout: one item's execution, bounded by the queue lease and by
            # `runTimeoutSeconds` inside the sandbox. See `_call`.
            raw = self._call(OP_DRAIN, {"key": self.key, "limit": limit,
                                        "workerId": worker_id, "capability": token},
                             timeout=None)
        finally:
            self.broker.release(dispatch)
        self.runs += 1
        return ForkOutcome(turns=list(raw.get("turns") or []),
                           jobs=list(raw.get("jobs") or []),
                           resumes=list(raw.get("resumes") or []),
                           count=int(raw.get("count") or 0),
                           exit_code=int(raw.get("exitCode") or 0),
                           error=raw.get("error"),
                           duration_ms=int(raw.get("durationMs") or 0))
