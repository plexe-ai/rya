"""The tenant side of the broker — a ``Store`` that is a socket (D18).

What runs in the sandbox holds a socket path and a run-scoped capability, and
nothing else. :class:`BrokerStore` is duck-typed to ``Store`` closely enough that
``RuntimeContext`` cannot tell, which is the whole trick: the mediated path has to
be behaviourally identical to the direct one, or every handler becomes
posture-dependent and the trusted and untrusted deployments diverge into two
products.

**Unknown methods raise ``AttributeError``, not a refusal.** That looks like the
weaker choice and is the stronger one. ``RuntimeContext`` is written defensively
around store capability — ``getattr(self.store, "meter_append", None)``,
``hasattr(self.store, "upsert_connection")`` — because a third-party duck-typed
store may predate a method. Presenting a callable for everything and refusing on
the wire would make ``_meter`` fire a denied call on every model step and swallow
the error, which is worse than useless: it would look like metering was happening.
Absent means absent, so the tenant-side runtime degrades to *not metering* — and
the broker writes the row instead, which is exactly D30's arrangement.

The server refuses independently (:func:`protocol.resolve_method`). Two layers, and
the client's is a courtesy: a handler that hand-crafts a frame gets the server's.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..errors import RyaError
from . import protocol as proto

log = logging.getLogger("rya.broker")


class BrokerClient:
    """One connection to the broker, with the capability for this run attached.

    Lazily connected so constructing one is free — the fork builds it before it
    knows whether the handler will touch state at all, and a handler that only
    computes should not pay for a socket.
    """

    def __init__(self, socket_path: str | Path, capability: str, *,
                 connect_timeout: float = 5.0, timeout: float = 300.0) -> None:
        self.socket_path = str(socket_path)
        self.capability = capability
        self._connect_timeout = connect_timeout
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._seq = 0
        # One request/response exchange at a time on the shared socket. The engine's
        # threaded path (`work_once(concurrency=N)`) could otherwise interleave two
        # calls on one socket and each would read the other's reply — the same hazard
        # the pool documents for a psycopg connection across a fork, one layer up.
        self._lock = threading.Lock()
        # Whether THIS thread already has a call in flight. A nested call is not a
        # hypothetical: a mediated `ctx.llm.respond` streams, and `turns.py` wires
        # `on_token` to `store.stream_append`, so every token writes to the store from
        # inside the model call. With a plain lock that deadlocked the fork until its
        # queue lease expired — which is every mediated streaming turn, the exact path
        # D30 exists for. A reentrant lock would have been worse than the deadlock: the
        # nested request would have gone out on a socket with a reply still in flight,
        # and the two exchanges would have read each other's frames.
        #
        # So a nested call gets its own connection, and the broker keys authority by
        # dispatch rather than by connection so the second one can still write the run
        # it is streaming (see `BrokerServer._Owned`).
        self._depth = threading.local()
        # The sequence counter alone. Separate from `_lock` because a nested call
        # cannot take that one (the outer call holds it) and still needs a distinct
        # `seq` — two exchanges sharing a number would be indistinguishable in a log.
        self._seq_lock = threading.Lock()

    # ---- connection -------------------------------------------------------
    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        self._sock = self._open()
        return self._sock

    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            raise RyaError(
                proto.E_UNAVAILABLE,
                f"Cannot reach the broker at {self.socket_path}: {exc}",
                hint="The broker runs in the claimer and the sandbox mounts its "
                     "socket. If this is a hand-started worker, mediation was "
                     "requested without anything to mediate through.",
            ) from exc
        sock.settimeout(self._timeout)
        return sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover
                pass
            self._sock = None

    def __enter__(self) -> "BrokerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- the one call path ------------------------------------------------
    def call(self, method: str, *args, on_token: Optional[Callable[[str], None]] = None,
             **kwargs) -> Any:
        """Send one request and return its result, forwarding stream frames.

        Interim frames carrying ``token`` are handed to ``on_token`` and do not
        terminate the exchange, so a mediated ``ctx.llm.respond(stream=True)``
        reaches the WebSocket the same way a direct one does. Without that, turning
        on mediation would silently convert every streaming turn into a
        wait-then-dump, which is a user-visible regression disguised as a security
        improvement.
        """
        nested = getattr(self._depth, "n", 0) > 0
        if nested:
            # A call made from inside another call's callback. Its own socket, closed
            # when it finishes, and NOT under `self._lock` — the outer call holds that.
            sock = self._open()
            try:
                return self._exchange(sock, method, args, kwargs, on_token)
            finally:
                try:
                    sock.close()
                except OSError:  # pragma: no cover
                    pass
        with self._lock:
            sock = self._connect()
            self._depth.n = 1
            try:
                return self._exchange(sock, method, args, kwargs, on_token)
            finally:
                self._depth.n = 0

    def _exchange(self, sock: socket.socket, method: str, args, kwargs,
                  on_token: Optional[Callable[[str], None]]) -> Any:
        """One request, then frames until a terminal one. Shared by both call paths."""
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        try:
            proto.send_frame(sock, proto.request(method, list(args), kwargs,
                                                 capability=self.capability,
                                                 seq=seq))
            while True:
                reply = proto.recv_frame(sock)
                if "token" in reply:
                    if on_token is not None:
                        on_token(reply.get("token") or "")
                    continue
                if reply.get("ok"):
                    return reply.get("result")
                proto.raise_from_error(reply)
        except (EOFError, OSError) as exc:
            if sock is self._sock:
                self.close()
            raise RyaError(
                proto.E_UNAVAILABLE,
                f"The broker connection dropped during '{method}': {exc}",
                hint="The claimer holding the broker exited while this run was in "
                     "flight. The queue lease will make the item claimable again.",
            ) from exc


    def ping(self) -> dict:
        return self.call("ping")

    # ---- services ---------------------------------------------------------
    def llm_call(self, **kwargs) -> dict:
        on_token = kwargs.pop("on_token", None)
        return self.call(proto.SERVICE_LLM, on_token=on_token,
                         stream=on_token is not None, **kwargs)

    def http_tool(self, **kwargs) -> dict:
        return self.call(proto.SERVICE_HTTP_TOOL, **kwargs)

    def egress_fetch(self, url: str, **kwargs) -> dict:
        return self.call(proto.SERVICE_EGRESS, url=url, **kwargs)

    def secret_get(self, name: str) -> Optional[str]:
        return self.call(proto.SERVICE_SECRET, name=name)

    def queue_claim(self, *, types=None, lease_seconds: float = 120.0) -> list:
        """Ask the broker to claim. Note what is *not* passed.

        No worker id, no version, no agent, no limit. All four are the broker's to
        decide from the capability, which is what makes D22's agent filter and D12's
        version pin enforced rather than requested — see ``protocol``'s note on why
        ``queue.claim``'s after-the-claim filtering cannot be trusted to a fork.
        """
        return self.call(proto.SERVICE_QUEUE_CLAIM, types=list(types) if types else None,
                         leaseSeconds=lease_seconds) or []

    def claim_due_job(self) -> Optional[dict]:
        return self.call(proto.SERVICE_JOB_CLAIM)

    def queue_complete(self, job_id: str, output=None) -> dict:
        return self.call(proto.SERVICE_QUEUE_COMPLETE, jobId=job_id, output=output)

    def queue_fail(self, job_id: str, error: str) -> dict:
        return self.call(proto.SERVICE_QUEUE_FAIL, jobId=job_id, error=error)

    def queue_heartbeat(self, job_id: str, extend_seconds: float) -> dict:
        return self.call(proto.SERVICE_QUEUE_HEARTBEAT, jobId=job_id,
                         extendSeconds=extend_seconds)


class BrokerStore:
    """A ``Store``-shaped façade over a :class:`BrokerClient`.

    Holds no root, no DSN and no key. ``root`` exists as an attribute because
    ``RuntimeContext`` and the file helpers read it, and it points at the unpacked
    bundle — which the sandbox does have, and which contains no credentials.
    """

    def __init__(self, client: BrokerClient, *, root: Optional[Path] = None,
                 workspace: str = "", agent: str = "") -> None:
        self._client = client
        self.root = Path(root) if root else None
        self.workspace_id = workspace
        self.agent = agent

    # `describe` is how the platform reports what backs a deployment, and a
    # mediated store must not claim to be the thing behind the broker.
    def describe(self) -> dict:
        return {"kind": "broker", "socket": self._client.socket_path,
                "workspace": self.workspace_id,
                "note": "mediated (D18): this process holds no database credential"}

    # `Engine.__init__` calls `ensure()` on every construction and `close()` on
    # teardown. Neither belongs on the wire: a fork must not create schema, and the
    # connection's lifetime is the fork's, not a store's. Answered locally so the
    # mediated store is a drop-in without `ensure` having to be allowlisted.
    def ensure(self) -> None:
        return None

    # The four hooks `queue.py` looks for to know it must not do the lease
    # transitions locally. Named methods rather than an ``isinstance`` check in
    # ``queue.py``, so the queue module keeps knowing nothing about the broker — the
    # same duck-typing the store seam already uses, and it keeps ``rya.queue``
    # importable without pulling in a socket protocol.
    def broker_claim(self, *, types=None, lease_seconds: float = 120.0) -> list:
        return self._client.queue_claim(types=types, lease_seconds=lease_seconds)

    def broker_complete(self, job_id: str, output=None) -> dict:
        return self._client.queue_complete(job_id, output)

    def broker_fail(self, job_id: str, error: str) -> dict:
        return self._client.queue_fail(job_id, error)

    def broker_heartbeat(self, job_id: str, extend_seconds: float) -> dict:
        return self._client.queue_heartbeat(job_id, extend_seconds)

    def claim_due_job(self, agent=None):
        """Route the `jobs` primitive's claim through the broker too.

        Shadows ``__getattr__`` on purpose: ``claim_due_job`` is not on the store
        allowlist, because the agent filter must be applied by the broker rather than
        supplied by the caller. ``agent`` is accepted and ignored for the same reason
        ``broker_claim`` ignores its arguments.
        """
        return self._client.claim_due_job()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in proto.LOCAL_NOOP_METHODS:
            return lambda *a, **k: None
        if name not in proto.ALL_METHODS:
            raise AttributeError(
                f"{name!r} is not on the broker's allowlist. The mediated store "
                "exposes the surface the ctx runtime needs; governance, billing and "
                "execution-plane methods are withheld (D18, broker/protocol.py)."
            )
        client = self._client

        def _call(*args, **kwargs):
            return client.call(name, *args, **kwargs)

        _call.__name__ = name
        return _call

    def close(self) -> None:
        self._client.close()


def client_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[BrokerClient]:
    """Build a client from the two variables a sandbox is handed, or None.

    Returning None rather than raising when the variables are absent is what lets
    one code path serve both postures: an unmediated worker finds nothing here and
    opens a store directly, which is the trusted deployment and the entire test
    suite.
    """
    env = env if env is not None else os.environ
    path = (env.get(proto.SOCKET_ENV) or "").strip()
    cap = (env.get(proto.CAPABILITY_ENV) or "").strip()
    if not path or not cap:
        return None
    return BrokerClient(path, cap)
