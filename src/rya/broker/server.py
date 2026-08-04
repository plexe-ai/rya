"""The broker — the process that holds the credentials, so the tenant does not (D18).

Runs inside the claimer (platform code, no tenant imports) and listens on a Unix
socket. The template process and its forks reach it over that socket with a
run-scoped capability, and that socket is the *entire* interface between tenant
code and everything valuable: the database, the seal keys, the pooled provider key,
the object store, and the network.

**Why this and not one database role per tenant.** §5 answers it directly:
role-per-tenant binds RLS to ``current_user`` instead of a settable GUC, which
fixes threat 2.1 and none of the other three — the seal key and the provider keys
are still in reachable memory and egress is still unmediated. The broker fixes all
four, and it does so by a mechanism that fails differently from the sandbox, which
is what makes the two layers worth having together (D18's stated rationale).

**Where the boundary actually is.** Not "the tenant cannot reach the database" —
that is a consequence. The property is that **every argument describing whose data
this is gets overwritten by code the tenant does not run.** A handler asking to
save run ``r_somebody_else`` does not get a permission error; it gets its own run
id substituted, because the identity fields were never the caller's to supply. See
:mod:`rya.broker.protocol` for the table and for why each withheld method is
withheld.

**Threads, not asyncio.** One thread per connection, and connections are few: one
per forked child, and a fork runs one item. asyncio would mean either an event loop
in the claimer competing with the worker's own poll loop, or a second process. The
store call underneath is blocking either way.

**A note on the store handle.** Every connection thread shares one ``Store``. For
``PostgresStore`` that is a connection pool underneath and is safe across threads;
what is *not* safe is sharing it across a fork, which is why the template is
spawned rather than forked from the claimer and why children talk over a socket
instead of inheriting a handle. The broker is the reason that constraint stopped
being a limitation and became the design.
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..errors import RyaError
from . import protocol as proto
from .protocol import Capability, Method

log = logging.getLogger("rya.broker")

# How long past a run's dispatch a capability stays valid. Generous relative to a
# turn lease (120s) because a long tool loop is legitimate, and finite because the
# token lives in tenant-reachable memory.
DEFAULT_CAPABILITY_TTL = 900.0

# Fields stripped from a connection record before it crosses to the tenant. The
# secret is the point; `secretRef` and friends are stripped for the same reason a
# denylist here would be wrong — see `_redact_connection`.
CONNECTION_SECRET_FIELDS = ("secret", "secretRef", "refreshToken", "accessToken")


@dataclass
class _Owned:
    """What one **dispatch** was actually given.

    Not derived from the capability's *contents*, because a fork claims its own work
    and cannot know its run id in advance (D27 keeps the claim and the execute
    together; see :mod:`rya.broker.protocol`). Populated by the broker's own claim
    service and by ``new_run_id``, so both routes to "a run this fork may write" pass
    through code the tenant does not run.

    **Keyed by dispatch rather than by connection since Phase 5.** It used to live on
    the connection, on the reasoning that a connection is one fork's lifetime and
    therefore "naturally one dispatch". That was approximately true and broke on the
    exact case D30 cares about: a mediated ``ctx.llm.respond`` streams, its
    ``on_token`` writes to ``store.stream_append``, and a nested store call cannot
    share a socket with the request that is still in flight on it. So a nested call
    needs its own connection — and with per-connection ownership that second
    connection had no authority to write the run it was streaming.

    Keying on ``cap.dispatch`` is not a weakening: a capability is HMAC-signed by this
    process and issued per fork, so presenting one *is* being that dispatch, whichever
    socket it arrives on. The connection was only ever a proxy for it.
    """

    runs: set = field(default_factory=set)
    jobs: set = field(default_factory=set)
    approvals: set = field(default_factory=set)

    def describe(self) -> dict:
        return {"runs": sorted(self.runs), "jobs": sorted(self.jobs),
                "approvals": sorted(self.approvals)}


class BrokerServer:
    """Serve mediated state and IO to tenant processes over a Unix socket.

    Construction takes the things a tenant must not hold, one argument each, so
    that "what does the broker have that the sandbox does not" is answerable by
    reading the signature.
    """

    def __init__(self, *, store, project_root: Optional[Path] = None,
                 workspace: str = "default", agent: str = "",
                 config=None, keyring=None, egress=None,
                 socket_path: Optional[Path] = None,
                 capability_ttl: float = DEFAULT_CAPABILITY_TTL,
                 body_timeout: float = proto.BODY_TIMEOUT,
                 quota_check: Optional[Callable[[str, dict], None]] = None,
                 config_for: Optional[Callable[..., Any]] = None,
                 egress_for: Optional[Callable[[str], Any]] = None) -> None:
        self.body_timeout = body_timeout
        self.store = store
        self.project_root = Path(project_root) if project_root else None
        self.workspace = workspace or "default"
        self.agent = agent
        self.config = config
        self.keyring = keyring
        self.egress = egress
        # Phase 5, #19-8b: at the wide claimer scope one broker serves every agent a
        # tenant owns, so the two things that are *declared per agent* — the model
        # routes and the secrets (from the manifest) and the egress service (from the
        # agent-qualified guard policy, D28) — stop being constructor arguments and
        # become lookups. Everything else the broker holds is workspace-wide: the
        # store, the key ring, the quota check. That split is not incidental; it is
        # the line between deployment configuration and tenant declaration.
        self._config_for = config_for
        self._egress_for = egress_for
        self._config_cache: Dict[tuple, Any] = {}
        self._egress_cache: Dict[str, Any] = {}
        self.capability_ttl = capability_ttl
        self._quota_check = quota_check
        # The signing secret. Generated per server, never written down, never sent:
        # a capability is only forgeable by something that can read this process's
        # memory, which is the same thing as being the broker.
        self._secret = os.urandom(32)
        self._dir: Optional[str] = None
        self._path = Path(socket_path) if socket_path else None
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._conns: List[socket.socket] = []
        self._lock = threading.Lock()
        # Observability the operator needs and the tenant cannot alter: every
        # refusal, counted by code. This is what makes "a tenant tried to reach
        # outside its surface" visible rather than merely prevented.
        self.calls: Dict[str, int] = {}
        self.refusals: List[dict] = []
        # (workspace, dispatch) -> what that dispatch may write. See `_Owned`.
        self._owned: Dict[tuple, _Owned] = {}

    # ---- lifecycle --------------------------------------------------------
    @property
    def socket_path(self) -> Path:
        if self._path is None:
            raise RyaError(proto.E_UNAVAILABLE, "The broker has not been started.")
        return self._path

    def start(self) -> Path:
        """Bind, listen, and serve in a background thread. Returns the socket path.

        The socket lives in a 0700 directory rather than relying on the socket's own
        mode: a Unix socket's permission bits are honoured by Linux but the
        portable, always-checked guard is the directory, and a sandbox that shares
        a filesystem namespace with anything else must not expose this by accident.
        """
        if self._path is None:
            self._dir = tempfile.mkdtemp(prefix="rya-broker-")
            os.chmod(self._dir, stat.S_IRWXU)
            self._path = Path(self._dir) / "broker.sock"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self._path))
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        self._sock.listen(16)
        self._sock.settimeout(0.25)
        self._thread = threading.Thread(target=self._accept_loop, name="rya-broker",
                                        daemon=True)
        self._thread.start()
        log.info("broker listening on %s (workspace=%s agent=%s)", self._path,
                 self.workspace, self.agent or "-")
        return self._path

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self._lock:
            conns, self._conns = list(self._conns), []
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._path is not None and self._path.exists():
            try:
                self._path.unlink()
            except OSError:  # pragma: no cover
                pass
        if self._dir:
            try:
                os.rmdir(self._dir)
            except OSError:  # pragma: no cover
                pass

    def __enter__(self) -> "BrokerServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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
        # Ownership is looked up per request, from the capability's dispatch id — see
        # `_Owned` for why it stopped being per connection. A connection carries no
        # authority of its own, which is also what lets a mediated streaming call open
        # a second one for its nested `stream_append` writes.
        try:
            while not self._stop.is_set():
                try:
                    req = proto.recv_frame(conn, body_timeout=self.body_timeout)
                except (EOFError, OSError):
                    return
                except RyaError as exc:
                    # A malformed frame desynchronises the stream, so the only safe
                    # answer is to report and hang up rather than try to resync.
                    try:
                        proto.send_frame(conn, proto.err(0, exc))
                    except OSError:
                        pass
                    return
                seq = int(req.get("seq") or 0)
                try:
                    result = self._dispatch(req, conn=conn, seq=seq)
                    proto.send_frame(conn, proto.ok(seq, result))
                except BaseException as exc:  # noqa: BLE001 - reported, never fatal
                    self._note_refusal(req, exc)
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

    def _note_refusal(self, req: dict, exc: BaseException) -> None:
        code = getattr(exc, "code", None) or type(exc).__name__
        if not code.startswith("E_BROKER") and not code.startswith("E_CAPABILITY") \
                and code not in ("E_EGRESS_DENIED", "E_MODEL_NOT_ALLOWED",
                                 "E_QUOTA_EXCEEDED"):
            return
        self.refusals.append({"method": req.get("method"), "code": code,
                              "message": str(exc)[:300], "at": time.time()})

    # ---- capabilities -----------------------------------------------------
    def mint(self, *, dispatch: str = "", agent: Optional[str] = None,
             version_id: str = "", ttl: Optional[float] = None) -> str:
        """Issue authority for one dispatch. Called by the claimer, never by a tenant.

        ``dispatch`` empty produces the handshake capability: it authenticates the
        template as something the broker launched and authorises nothing on the
        data surface (:data:`protocol.HANDSHAKE_METHODS`). That distinction is the
        reason the template holding a token is not the same as tenant code holding
        credentials.
        """
        ttl = self.capability_ttl if ttl is None else ttl
        cap = Capability(workspace=self.workspace, agent=agent or self.agent,
                         dispatch=dispatch, version_id=version_id,
                         expires_at=time.time() + max(1.0, ttl),
                         nonce=os.urandom(6).hex())
        return proto.mint_capability(self._secret, cap)

    def _authorize(self, req: dict, method: Method) -> Capability:
        cap = proto.read_capability(self._secret, req.get("cap") or "")
        if cap.workspace != self.workspace:
            # Cannot happen with a capability this server minted; a hard refusal
            # anyway, because "cannot happen" is what a boundary is for.
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Capability names workspace '{cap.workspace}' and this broker serves "
                f"'{self.workspace}'.")
        if not cap.dispatch and method.name not in proto.HANDSHAKE_METHODS:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"'{method.name}' needs a dispatch capability and this one names none.",
                hint="The template's handshake capability authorises the handshake. A "
                     "capability that can touch data is issued per dispatch, when the "
                     "claimer asks the template to fork.",
            )
        return cap

    # ---- dispatch ---------------------------------------------------------
    def _owned_for(self, cap: Capability) -> "_Owned":
        """The authority set for one dispatch, created on first use.

        Guarded by the server lock because two connections belonging to the same
        dispatch can be served by two threads at once — which is precisely the
        streaming case this keying exists for.
        """
        key = (cap.workspace, cap.dispatch)
        with self._lock:
            owned = self._owned.get(key)
            if owned is None:
                owned = self._owned[key] = _Owned()
            return owned

    def release(self, dispatch: str) -> None:
        """Forget a finished dispatch's authority. Called by the claimer.

        Not merely hygiene. Without it the map grows for the life of the claimer, and
        a long-lived tenant claimer serving thousands of items would accumulate one
        entry per item — and, worse, a *replayed* dispatch id would inherit the
        previous one's owned runs. The claimer mints dispatch ids and knows when one
        is over, so it is the only thing that can say so.
        """
        with self._lock:
            for key in [k for k in self._owned if k[1] == dispatch]:
                self._owned.pop(key, None)

    def _dispatch(self, req: dict, *, conn: socket.socket, seq: int) -> Any:
        name = str(req.get("method") or "")
        method = proto.resolve_method(name)
        cap = self._authorize(req, method)
        owned = self._owned_for(cap)
        self.calls[name] = self.calls.get(name, 0) + 1
        args = list(req.get("args") or [])
        kwargs = dict(req.get("kwargs") or {})
        if name == "ping":
            return {"ok": True, "workspace": self.workspace, "agent": cap.agent,
                    "protocol": proto.PROTOCOL_VERSION}
        if name == "now_iso":
            # Answered here rather than proxied. `now_iso` is a MODULE function in
            # `rya.store` that FileStore and PostgresStore happen to expose as a
            # bound name; proxying it through `getattr(store, ...)` worked by accident
            # and broke the moment the mediated store's own `__getattr__` was the
            # thing being asked. The broker's clock is also the more correct answer:
            # a sandbox may have no synchronised time at all.
            from ..store import now_iso

            return now_iso()
        if name in proto.SERVICE_METHODS:
            return self._service(name, cap, kwargs, conn=conn, seq=seq, owned=owned)
        return self._store_call(method, cap, args, kwargs, owned)

    # ---- the store surface ------------------------------------------------
    def _store_call(self, method: Method, cap: Capability,
                    args: list, kwargs: dict, owned: "_Owned") -> Any:
        fn = getattr(self.store, method.name, None)
        if fn is None:
            raise RyaError(
                proto.E_METHOD_DENIED,
                f"'{method.name}' is allowlisted but this store does not implement it.",
                hint="A duck-typed third-party Store is missing a method the mediated "
                     "runtime needs. Nothing degrades silently here: the direct path "
                     "would have raised AttributeError inside the handler instead.")
        args, kwargs = self._rescope(method, cap, args, kwargs, owned)
        result = fn(*args, **kwargs)
        if method.mint == proto.MINT_RUN and isinstance(result, str):
            # A sub-run's id. Recorded as owned so the writes that follow it are
            # accepted — the fork legitimately creates child runs (ctx.jobs fan-out),
            # and it did not claim them from anywhere.
            owned.runs.add(result)
        return self._filter_result(method.name, result)

    def _bind(self, method: Method, args: list, kwargs: dict):
        """Bind positional arguments to their parameter names.

        The reason scope rules can be written against names at all. Without this,
        ``create_approval(rid, ...)`` and ``create_approval(run_id=rid, ...)`` would
        be checked differently and the positional form would be the bypass.
        """
        import inspect

        fn = getattr(self.store, method.name)
        try:
            params = [p for p in inspect.signature(fn).parameters]
        except (TypeError, ValueError):  # pragma: no cover - builtins
            params = []
        merged = dict(kwargs)
        extra: list = []
        for i, value in enumerate(args):
            if i < len(params) and params[i] not in merged:
                merged[params[i]] = value
            else:  # pragma: no cover - *args store methods do not exist today
                extra.append(value)
        return extra, merged

    def _rescope(self, method: Method, cap: Capability, args: list, kwargs: dict,
                 owned: "_Owned"):
        """Apply every scope rule: force what has one right answer, own the rest."""
        if not method.scopes:
            return args, kwargs
        extra, merged = self._bind(method, args, kwargs)
        for param, rule in method.scopes.items():
            if rule == proto.FORCE_AGENT:
                merged[param] = cap.agent
            elif rule == proto.OWN_RUN:
                merged[param] = self._own_run(param, merged, owned)
            elif rule == proto.OWN_JOB:
                merged[param] = self._own_job(param, merged, owned)
            elif rule == proto.OWN_APPROVAL:
                merged[param] = self._own_approval(param, merged, owned)
        return extra, merged

    def _own_run(self, param: str, merged: dict, owned: "_Owned"):
        """Refuse a run this connection was not given. Never substitutes.

        ``save_run`` passes the whole dict, so the id is inside it; every other
        caller passes the id directly. Both shapes come through here so there is one
        place that decides what "my run" means.
        """
        value = merged.get(param)
        run_id = value.get("id") if isinstance(value, dict) else value
        if not run_id or run_id not in owned.runs:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Run '{run_id or '(none)'}' was not claimed or created by this "
                "dispatch.",
                hint="A fork may touch the run it claimed, the run behind the approval "
                     "it is resuming, and runs it minted itself. Substituting the "
                     "right id instead of refusing would write this run's journal "
                     "under another run's identity, which is worse than a refusal.")
        return value

    def _own_job(self, param: str, merged: dict, owned: "_Owned"):
        value = merged.get(param)
        job_id = value.get("id") if isinstance(value, dict) else value
        if not job_id or job_id not in owned.jobs:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Queue item '{job_id or '(none)'}' was not claimed by this dispatch.",
                hint="Completing, failing or appending to an item this fork did not "
                     "claim would let one agent's handler finish another's work.")
        return value

    def _own_approval(self, param: str, merged: dict, owned: "_Owned"):
        """An approval is owned when its run is.

        Resolved through the store rather than tracked, because an approval is
        created mid-run and the id is the handler's to choose from its own journal.
        The lookup is the authorization: an approval whose ``runId`` this connection
        does not own is refused, so the id being guessable does not matter.
        """
        value = merged.get(param)
        approval_id = value.get("id") if isinstance(value, dict) else value
        if not approval_id:
            raise RyaError(proto.E_SCOPE_DENIED, "No approval id supplied.")
        if approval_id in owned.approvals:
            return value
        record = self.store.get_approval(approval_id)
        run_id = (record or {}).get("runId")
        if not record or run_id not in owned.runs:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Approval '{approval_id}' does not belong to a run this dispatch owns.",
                hint="Approvals are addressed by id, so the broker resolves the id to "
                     "its run and checks that instead — a guessable id buys nothing.")
        owned.approvals.add(approval_id)
        return value

    def _filter_result(self, name: str, result: Any) -> Any:
        """Redact on the way out. The seal key stays here; the secret does too.

        ``get_connection`` is the interesting one. The direct path returns the
        *opened* secret so ``_authorize_connection`` can inject it into an HTTP
        tool. Under mediation the secret never crosses: the tenant gets the record
        with a ``hasSecret`` flag, the scope check still runs tenant-side as a
        friendly error, and the injection happens in :meth:`_http_tool` where the
        credential already lives. That is the D2 contract change (§9 risk 2) at its
        narrowest point.
        """
        if name == "get_connection":
            return _redact_connection(result)
        if name == "list_connections":
            return [_redact_connection(r) for r in (result or [])]
        return result

    # ---- services ---------------------------------------------------------
    def _service(self, name: str, cap: Capability, kwargs: dict, *,
                 conn: socket.socket, seq: int, owned: "_Owned") -> Any:
        if name == proto.SERVICE_LLM:
            return self._llm_call(cap, kwargs, conn=conn, seq=seq, owned=owned)
        if name == proto.SERVICE_HTTP_TOOL:
            return self._http_tool(cap, kwargs)
        if name == proto.SERVICE_EGRESS:
            return self._egress_fetch(cap, kwargs)
        if name == proto.SERVICE_SECRET:
            return self._secret_get(cap, kwargs)
        if name == proto.SERVICE_QUEUE_CLAIM:
            return self._queue_claim(cap, kwargs, owned)
        if name == proto.SERVICE_JOB_CLAIM:
            return self._job_claim(cap, kwargs, owned)
        if name in (proto.SERVICE_QUEUE_COMPLETE, proto.SERVICE_QUEUE_FAIL,
                    proto.SERVICE_QUEUE_HEARTBEAT):
            return self._queue_transition(name, cap, kwargs, owned)
        raise RyaError(proto.E_METHOD_DENIED, f"No broker service '{name}'.")

    # ---- claiming, on the platform's side of the boundary ------------------
    def _queue_claim(self, cap: Capability, kwargs: dict, owned: "_Owned") -> List[dict]:
        """``queue.claim``, run here rather than in the fork.

        The ordinary function, with the four identity arguments taken from the
        capability instead of from the caller. That is the entire difference, and it
        is the difference between D22 being enforced and D22 being *requested*:
        ``queue.claim`` applies the agent and version filters in Python after the
        claim and releases what does not match, so when the caller is tenant-trust
        code the filter is advisory. Here the caller is the broker.

        ``limit`` is clamped to 1. A fork runs one item (D27), and a fork that could
        claim ten would hold ten leases on one child's lifetime.
        """
        from .. import queue as q

        types = kwargs.get("types")
        lease = float(kwargs.get("leaseSeconds") or q.DEFAULT_LEASE_SECONDS)
        claimed = q.claim(self.store, self._worker_id(cap),
                          types=list(types) if types else None, limit=1,
                          lease_seconds=lease,
                          version_id=cap.version_id or None,
                          agent=cap.agent or None)
        for job in claimed:
            self._own(job, owned)
        return claimed

    def _job_claim(self, cap: Capability, kwargs: dict, owned: "_Owned") -> Optional[dict]:
        """``claim_due_job``, with the agent filter forced (D22's second surface).

        Unlike the queue claim this one filters *inside* the store — Phase 3 put the
        predicate in SQL and in the file arm — so forcing the argument is genuine
        enforcement rather than a re-implementation.
        """
        job = self.store.claim_due_job(cap.agent or None)
        if job:
            self._own(job, owned)
        return job

    def _queue_transition(self, name: str, cap: Capability, kwargs: dict,
                          owned: "_Owned") -> dict:
        """Complete, fail or heartbeat an item this connection claimed.

        The worker id is the broker's own — the same one the claim used — so
        ``queue._check_holder`` compares two platform-supplied values and a fork
        cannot complete an item it does not hold. That check exists in ``queue.py``
        already; all this does is put both sides of it on the same side of the
        boundary. The bug this closes was live for exactly one test run: forcing the
        worker id on the *claim* while the child completed under its own id made every
        mediated turn fail with ``E_QUEUE_CONFLICT``.
        """
        from .. import queue as q

        job_id = str(kwargs.get("jobId") or "")
        if job_id not in owned.jobs:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Queue item '{job_id or '(none)'}' was not claimed by this dispatch.",
                hint="Completing or failing an item this fork did not claim would let "
                     "one agent's handler finish another's work.")
        worker_id = self._worker_id(cap)
        if name == proto.SERVICE_QUEUE_COMPLETE:
            return q.complete(self.store, job_id, worker_id, kwargs.get("output"))
        if name == proto.SERVICE_QUEUE_FAIL:
            return q.fail(self.store, job_id, worker_id, str(kwargs.get("error") or ""))
        # Heartbeat. The extension is clamped: a fork asking for a week-long lease
        # would make the reclaim path — the thing that recovers a wedged run —
        # ineffective for a week.
        requested = float(kwargs.get("extendSeconds") or q.DEFAULT_LEASE_SECONDS)
        return q.heartbeat(self.store, job_id, worker_id,
                           min(requested, q.DEFAULT_LEASE_SECONDS))

    def _worker_id(self, cap: Capability) -> str:
        """Attribution the tenant does not choose.

        A fork asking to be recorded as some other worker would make the worker
        registry — which the supervisor reaps and scales on — a tenant-writable
        field. It gets the dispatch id instead, which is the honest answer to "what
        holds this lease".
        """
        return f"brokered:{cap.agent or '-'}:{cap.dispatch}"

    def _own(self, job: dict, owned: "_Owned") -> None:
        """Record what a claim entitles this connection to touch.

        Three sources, because a claimed item points at its run in three different
        shapes: a chat-turn carries ``payload.runId`` (the api created the run row
        before enqueuing, so the caller gets a run id synchronously); a resume
        carries an ``approvalId`` and the run is behind it; and a due job from the
        `jobs` primitive carries ``runId`` directly.
        """
        job_id = job.get("id")
        if job_id:
            owned.jobs.add(job_id)
        payload = job.get("payload") or {}
        for candidate in (payload.get("runId"), job.get("runId")):
            if candidate:
                owned.runs.add(candidate)
        approval_id = payload.get("approvalId")
        if approval_id:
            try:
                record = self.store.get_approval(approval_id)
            except Exception:  # noqa: BLE001 - a lookup failure is not authority
                record = None
            if record:
                owned.approvals.add(approval_id)
                if record.get("runId"):
                    owned.runs.add(record["runId"])

    # ---- per-agent configuration -------------------------------------------
    def config_for(self, agent: str = "", version_id: str = ""):
        """The ``RunConfig`` for one agent, resolved once and cached.

        ``self.config`` is the narrow-scope answer: one claimer, one agent, one config
        built before the broker started. At tenant scope there is no such thing, so a
        resolver is supplied instead and this caches per ``(agent, version)`` — per
        version and not merely per agent, because two live versions of one agent may
        declare different routes and a rollout is exactly when they do.

        A resolver that raises is *not* cached. A transient store failure while reading
        a version record would otherwise poison inference for that agent for the life
        of the claimer.
        """
        if self._config_for is None:
            return self.config
        cache_key = (agent, version_id)
        if cache_key in self._config_cache:
            return self._config_cache[cache_key]
        resolved = self._config_for(agent, version_id)
        self._config_cache[cache_key] = resolved
        return resolved

    def public_routes(self, *, agent: str = "", version_id: str = "") -> dict:
        """One agent's routes with the credentials removed — the tenant-visible shape.

        Lives here, next to the credential, so there is exactly one projection of a
        ``RunConfig`` that crosses the boundary. It was previously computed by the
        claimer's executor, which was harmless while the executor also held the real
        config and became a second place to get it wrong the moment the config became
        per-agent.

        What survives: provider, model, temperature, maxTokens — the fields a handler
        legitimately reads, and ``ctx.llm``'s journal label is built from ``model``, so
        withholding the name would make every mediated replay look like drift. What
        does not: ``api_key`` (a credential) and ``base_url`` (an egress target).
        """
        config = self.config_for(agent, version_id)
        routes = dict(getattr(config, "routes", None) or {})
        return {name: {"provider": r.provider, "model": r.model,
                       "temperature": r.temperature, "maxTokens": r.max_tokens}
                for name, r in routes.items()}

    def _egress_of(self, agent: str):
        """The egress service for one agent. Cached, because it holds a posture.

        :class:`rya.egress.EgressService` reads the network posture at construction —
        it is deliberately a snapshot, so that a promoted allowlist produces a
        detectable divergence from the live guard policy until every sandbox recycles.
        Rebuilding one per call would erase that divergence by making the snapshot
        continuously fresh, which would turn a fail-closed check into a no-op.
        """
        if self._egress_for is None:
            return self.egress
        if agent not in self._egress_cache:
            self._egress_cache[agent] = self._egress_for(agent)
        return self._egress_cache[agent]

    def _route(self, cap: Capability, route_name: Optional[str]):
        """The real ``ModelRoute``, credential attached. Never leaves this process."""
        config = self.config_for(cap.agent or "", cap.version_id or "")
        if config is None:
            raise RyaError(
                proto.E_UNAVAILABLE,
                "The broker has no run config, so it cannot resolve a model route.",
                hint="The claimer builds the config (D8) and hands it to the broker, or "
                     "— at tenant scope — the broker resolves it from the version "
                     "record's persisted manifest (D21). A version published before "
                     "D21 has no manifest on its record, so its state can be mediated "
                     "and its inference cannot; re-publish it. A broker with no config "
                     "at all can mediate state but not inference.")
        return config.route(route_name)

    def _llm_call(self, cap: Capability, kwargs: dict, *, conn, seq: int,
                  owned: "_Owned") -> dict:
        """D30's boundary: attribute, allowlist, quota, call, meter — in that order.

        The order is the design. Attributing first means the meter row cannot be
        pointed at someone else's run; checking the allowlist before quota means a
        refused model never consumes budget; checking quota before the call means a
        tenant over budget never spends the pooled key; metering after the call means
        the row describes what actually happened. Doing the meter *here* rather than
        in the tenant process is what makes the billing record something the billed
        party could not have written.
        """
        from ..providers import chat as provider_chat
        from ..providers import respond as provider_respond

        run_id = str(kwargs.get("runId") or "")
        if run_id not in owned.runs:
            # Attribution, not confidentiality: this is the same workspace either
            # way. It matters because the meter row is the invoice line, and a
            # handler able to bill its inference to a different run makes per-run
            # cost — which the console shows and support reads — untrustworthy.
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Inference was attributed to run '{run_id or '(none)'}', which this "
                "dispatch does not own.")
        kind = str(kwargs.get("kind") or "respond")
        route_name = kwargs.get("route")
        route = self._route(cap, route_name)
        model = kwargs.get("model") or route.model
        self._check_model_allowed(model, route.provider)
        self._check_quota(cap, {"model": model, "kind": kind})

        def forward(chunk: str) -> None:
            # Tokens ride back as interim frames on the same connection. The client
            # forwards them to its own on_token and keeps reading, so a mediated
            # stream reaches the WebSocket exactly like a direct one.
            try:
                proto.send_frame(conn, {"v": proto.PROTOCOL_VERSION, "seq": seq,
                                        "token": chunk})
            except OSError:  # pragma: no cover - peer went away mid-stream
                pass

        on_token = forward if kwargs.get("stream") else None

        started = time.time()
        if kind == "chat":
            result = provider_chat(messages=list(kwargs.get("messages") or []),
                                   tools=kwargs.get("tools"),
                                   system=kwargs.get("system") or "",
                                   route=route, on_token=on_token)
        else:
            result = provider_respond(system=kwargs.get("system") or "",
                                      input=kwargs.get("input") or {},
                                      schema=kwargs.get("schema"),
                                      route=route, on_token=on_token,
                                      documents=kwargs.get("documents"))
        self._meter(cap, run_id=run_id, kind=kwargs.get("meterKind") or f"llm.{kind}",
                    label=kwargs.get("label") or model, result=result,
                    duration_ms=int((time.time() - started) * 1000))
        return result

    def _check_model_allowed(self, model: str, provider: str) -> None:
        """The model allowlist, from the guard policy. Absent list = no restriction.

        Read from policy rather than the manifest because the manifest is in the
        bundle, and a bundle is written by the tenant. A limit the limited party can
        raise is not a limit — the same reasoning ``quotas.py`` records for who may
        set a quota.
        """
        allowed = self._model_allowlist()
        if allowed is None:
            return
        if model in allowed or f"{provider}:{model}" in allowed:
            return
        raise RyaError(
            "E_MODEL_NOT_ALLOWED",
            f"Model '{model}' is not on this deployment's allowlist.",
            hint=f"Allowed: {', '.join(sorted(allowed)) or '(none)'}. The allowlist is "
                 "platform policy (`rya policy set models`), not manifest config, "
                 "because with a pooled provider key (D30) the model choice is a "
                 "cost decision the tenant does not own.",
        )

    def _model_allowlist(self) -> Optional[set]:
        getter = getattr(self.store, "policy_get", None)
        if getter is None:
            return None
        try:
            policy = getter("models") or {}
        except Exception:  # noqa: BLE001 - a policy read must not break inference
            return None
        allowed = policy.get("allow")
        if not allowed:
            return None
        return {str(m) for m in allowed}

    def _check_quota(self, cap: Capability, ctx: dict) -> None:
        """#21: refuse inference the workspace cannot pay for.

        Enforced *here* rather than only at admission because D30 changed what an
        overrun costs. Admission-only was right when the tenant held the provider
        key — the overshoot was bounded by one run and spent the tenant's money.
        With a pooled key the money is ours, so the check moved onto the call path.
        ``quotas.py``'s own reason for staying admission-only still holds for
        *runs*: this refuses a model call, which a handler can catch, and never
        kills a run mid-journal.
        """
        if self._quota_check is None:
            return
        self._quota_check(cap.workspace, ctx)

    def _meter(self, cap: Capability, *, run_id: str, kind: str, label: str,
               result: Any, duration_ms: int) -> None:
        """Write the billing row. The tenant cannot: ``meter_append`` is not allowlisted.

        Best-effort in the same way ``RuntimeContext._meter`` is — metering must
        never fail a run — but with one difference that matters: a failure here is
        *logged*, because a silently missing row is unbilled inference on a pooled
        key rather than a missing statistic.
        """
        append = getattr(self.store, "meter_append", None)
        if append is None or not isinstance(result, dict):
            return
        usage = result.get("usage") or {}
        record = {
            "runId": run_id,
            "kind": kind,
            "agent": cap.agent,
            "model": result.get("model") or label,
            "provider": result.get("provider"),
            "inputTokens": int(usage.get("input") or 0),
            "outputTokens": int(usage.get("output") or 0),
            "durationMs": duration_ms,
            # Provenance, so an audit can tell a brokered row from a legacy one
            # written by a trusted-posture worker.
            "source": "broker",
        }
        try:
            from ..observability.usage import price_for

            env = dict(getattr(self.config, "values", None) or {})
            record["costUsd"] = price_for(record["model"], record["inputTokens"],
                                          record["outputTokens"], env)
        except Exception:  # noqa: BLE001
            record["costUsd"] = 0.0
        try:
            append(record)
        except Exception as exc:  # noqa: BLE001
            log.error("metering failed for run %s (%s tokens): %s", run_id,
                      record["inputTokens"] + record["outputTokens"], exc)

    def _http_tool(self, cap: Capability, kwargs: dict) -> dict:
        """Execute an HTTP tool broker-side so its credential never crosses.

        The scope intersection is re-checked here even though ``_authorize_connection``
        already checked it tenant-side. The tenant-side check is the friendly error
        (it names the missing scope before anything is attempted); this one is the
        enforcement, because the tenant-side one runs in a process that could simply
        not run it.
        """
        url = str(kwargs.get("url") or "")
        provider = kwargs.get("provider") or ""
        secret = None
        if provider:
            secret = self._authorize_connection(
                provider=str(provider), owner=kwargs.get("owner"),
                required=set(kwargs.get("scopes") or []),
                user_scopes=kwargs.get("userScopes"))
        self._check_egress(url, "POST", cap)
        from ..sdk.context import _http_tool as direct

        return direct(url, dict(kwargs.get("input") or {}), secret)

    def _authorize_connection(self, *, provider: str, owner: Optional[str],
                              required: set, user_scopes) -> Optional[str]:
        conn = self.store.get_connection(provider, owner)
        if conn is None or conn.get("status") != "active":
            raise RyaError("E_NO_CONNECTION",
                           f"No active '{provider}' connection for this agent/user.",
                           hint="Create one: `rya connect <provider> --scopes <...>`.")
        granted = set(conn.get("scopes") or [])
        effective = granted if user_scopes is None else (granted & set(user_scopes))
        missing = required - effective
        if missing:
            raise RyaError(
                proto.E_SCOPE_DENIED,
                f"Tool requires {sorted(required)} on '{provider}', but the effective "
                f"grant (connection ∩ user) is {sorted(effective)}; missing {sorted(missing)}.",
                hint="Granted server-side. The tenant-side check produces the same "
                     "message earlier; this is the one that decides.")
        return conn.get("secret")

    def _egress_fetch(self, cap: Capability, kwargs: dict) -> dict:
        """The only route out of a sandbox with no network (D24).

        Deliberately not a general proxy: it speaks the two verbs a handler
        legitimately needs and applies the allowlist to both. A sandbox reaching a
        host directly does not arrive here at all — it is refused by the network,
        which is the point of D24 and the reason this method's refusal is the
        *second* line rather than the first.
        """
        url = str(kwargs.get("url") or "")
        method = str(kwargs.get("method") or "GET").upper()
        self._check_egress(url, method, cap)
        egress = self._egress_of(cap.agent)
        if egress is None:
            raise RyaError(
                "E_EGRESS_UNAVAILABLE",
                "No egress service is configured on this broker.",
                hint="A sandbox with deny-by-default networking needs a mediated "
                     "fetch, or handlers cannot reach even allowlisted hosts.")
        return egress.fetch(url, method=method, headers=kwargs.get("headers"),
                                 body=kwargs.get("body"),
                                 timeout=float(kwargs.get("timeout") or 30.0))

    def _guard_policy(self, agent: str):
        from ..guard import resolve_policy, store_key_for

        return resolve_policy(self.store, key=store_key_for(self.store, agent))

    def _check_egress(self, url: str, method: str, cap: Capability) -> None:
        """Guard's verdict, applied by the platform rather than by the tenant.

        The same policy ``guard.check_egress`` reads, evaluated on this side of the
        boundary. After D24 guard.py is governance and audit; this call is what makes
        its verdict binding on a *mediated* request, and the network is what makes it
        binding on an unmediated one — see :mod:`rya.egress`, which reconciles the
        two verdicts and alerts when they disagree.
        """
        from ..guard import check_egress

        check_egress(url, method, self._guard_policy(cap.agent))

    def _secret_get(self, cap: Capability, kwargs: dict) -> Optional[str]:
        """The tenant's OWN declared secrets. Deliberately still available.

        D18 removes *platform* credentials from the tenant process: the DSN, the
        seal key, the pooled provider key, the bucket credential. A secret the
        tenant declared for its own handler to use is theirs, and withholding it
        would break ``ctx.secrets`` for no security gain — the tenant supplied the
        value. The credential inventory has to make this distinction or it reports
        a false positive on every deployment (see :mod:`rya.broker.inventory`).
        """
        name = str(kwargs.get("name") or "")
        config = self.config_for(cap.agent or "", cap.version_id or "")
        secrets = dict(getattr(config, "secrets", None) or {})
        return secrets.get(name)

    # ---- introspection ----------------------------------------------------
    def describe(self) -> dict:
        return {"socket": str(self._path) if self._path else None,
                "workspace": self.workspace, "agent": self.agent,
                "calls": dict(self.calls), "refusals": len(self.refusals),
                "allowlist": sorted(proto.ALL_METHODS),
                "keyProvider": (self.keyring.describe() if self.keyring else None)}


def _redact_connection(record: Optional[dict]) -> Optional[dict]:
    """Strip credential fields, keeping enough for the tenant-side scope check.

    An allowlist would be wrong here and a denylist is right, which is the opposite
    of the method surface. A connection record is *mostly* metadata the handler
    needs (provider, scopes, status, owner, createdAt) with a small, named set of
    credential fields; a store adding a metadata field must not have it silently
    withheld. The risk this inverts — a store adding a *new* credential field that
    is not on the list — is mitigated by the field names being conventional and by
    ``hasSecret`` making the presence of a credential visible without its value.
    """
    if not record:
        return record
    out = {k: v for k, v in record.items() if k not in CONNECTION_SECRET_FIELDS}
    out["hasSecret"] = bool(record.get("secret"))
    out["brokered"] = True
    return out
