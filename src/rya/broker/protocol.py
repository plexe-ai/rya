"""The broker wire: what a tenant process may ask for, and what proves it may (D18).

Three things live here, and keeping them in one module with no imports from the
server or the client is deliberate — the allowlist is the security-relevant part
of D18, and it should be readable without reading an implementation.

1. **The method allowlist.** 31 store methods and 9 services, out of a ``Store``
   surface of about seventy. Everything absent is refused by default, so a method added
   to ``Store`` next month is not automatically reachable from tenant code. That direction matters more than the specific list: an allowlist
   that has to be extended on purpose fails safe when someone forgets, and a denylist
   does the opposite.

2. **Scope rules.** Which argument of which method carries an identity the caller does
   not get to choose, and what to do about it. Two kinds, and the difference is the
   design: ``force:`` overwrites the argument whether or not one was supplied — used
   where there is exactly one right answer, such as the agent on a created job —
   while ``own:`` refuses — used where the value is a legitimate choice among things
   this fork was actually given, such as which of its runs to write. Substituting a
   run id would write one run's journal under another run's identity, so that case
   fails closed rather than quietly right.

3. **Capabilities.** A short-lived signed token naming ``(workspace, agent,
   dispatch)``, handed to the fork that will execute one item. The template process
   — which imports tenant code and is therefore in the tenant's trust domain —
   holds only a *handshake* capability, which authorises nothing on the data
   surface.

**The thing that made this larger than the ``ctx`` surface.** D27 put the claim
loop *inside the fork* — Phase 3 established that the claim and the execute cannot
be split, because the lease, the reclaim and the memoized replay all live in that
pair. So the mediated surface is not just what a handler calls; it is also what
``turns.execute_pending`` and ``Engine.work_once`` call, and those are the
execution plane's own methods.

Handing those to tenant-trust code unmediated would be worse than the status quo,
because ``queue.claim`` applies D22's agent filter **in Python, after the claim**
(``queue.py``: "filtering happens after the claim rather than in SQL", a
deliberate choice to avoid forking the two store backends). That filter is
therefore *the caller's own code* — fine when the caller is the platform, and not
fine when the caller is a fork with the tenant's bundle imported into it. A hostile
handler simply would not release the sibling's job it was handed.

So claiming is a **service**, not a proxied method: :data:`SERVICE_QUEUE_CLAIM`
runs the ordinary ``queue.claim`` on the platform's side of the boundary with the
agent, the version, the worker id and the lease deadline all forced. The mediated
claimer ends up *more* constrained than an unmediated one, which is the useful
inversion here — it cannot name its own filter, and it cannot grant itself a longer
lease.

**Ownership is established by the claim, not by the token.** A fork does not know
which run it will work on until it claims something, so the capability cannot name
one. Instead the broker records, per connection, the jobs it claimed on that
connection's behalf and the runs those jobs belong to — including a resume's run,
which it resolves through the approval. Every downstream write is checked against
that set. A fork can therefore only touch what it was actually given, and the check
survives a handler that lies about its ids.

**What is deliberately not on the list, and why each one matters.**

``meter_append``. The metering ledger is the billing record (D10), and under D30
the platform pools the provider key — so a tenant that can write meter rows can
write its own invoice, and a tenant that can write *negative* usage bills nothing
for real inference. Today ``RuntimeContext._meter`` calls it from inside the tenant
process, which means the party being billed writes the bill. The broker's LLM
service writes it instead, from the response it made the call for.

``policy_set``. §11.2 already carved kill switches out of ``ctx.memory`` because
"governance a client can edit is not governance". ``policy_get`` is on the list
because the runtime has to *read* the kill switches it enforces; the write side is
the api's.

``worker_*``, ``version_*``, ``env_*``. The execution plane's own control surface. A
tenant that can deregister workers or retire versions can stop its neighbours' work
from being scheduled — availability, not confidentiality, but still a cross-tenant
effect. The claimer registers itself and heartbeats from the *platform* side, so a
fork never needs these.

``queue_reap``, ``queue_counts``, ``queue_claim_one``. Reaping resets leases across
the workspace, so a fork could make a sibling's in-flight item claimable. Counting
is the claimer's ``queue_depth``, which runs before any fork exists. And
``queue_claim_one`` is subsumed by :data:`SERVICE_QUEUE_CLAIM`, which is the whole
reason the service exists.

``list_runs``/``journal_read``/``run_counts``. Reads across the workspace's own
runs. Refused not because they cross a tenant boundary — they do not — but because
a handler has ``ctx.run`` for its own run and no legitimate need to enumerate the
tenant's other runs, and a replay-drift bug is easier to reason about when nothing
else can read a journal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from ..errors import RyaError

# ---- framing ----------------------------------------------------------------
# Length-prefixed JSON. Not pickle: `multiprocessing.Connection` (which the pool
# already uses for the template handshake) sends pickles, and a pickle from a
# hostile process is arbitrary code execution in the PARENT — the exact direction
# D18 exists to close. The broker's peer is tenant code, so the wire has to be a
# format whose parser cannot be talked into constructing objects.
HEADER = struct.Struct("!I")
MAX_FRAME = 32 * 1024 * 1024      # a tool result or a file read, not a stream

PROTOCOL_VERSION = 1

E_PROTOCOL = "E_BROKER_PROTOCOL"
E_METHOD_DENIED = "E_BROKER_METHOD_DENIED"
E_SCOPE_DENIED = "E_BROKER_SCOPE_DENIED"
E_UNAVAILABLE = "E_BROKER_UNAVAILABLE"
E_CAPABILITY_INVALID = "E_CAPABILITY_INVALID"
E_CAPABILITY_EXPIRED = "E_CAPABILITY_EXPIRED"

SOCKET_ENV = "RYA_BROKER_SOCKET"
CAPABILITY_ENV = "RYA_BROKER_CAPABILITY"
BROKER_ENV = "RYA_BROKER"          # "1" turns mediation on for a claimer


def broker_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = env if env is not None else os.environ
    return (env.get(BROKER_ENV) or "").strip().lower() in ("1", "true", "yes")


def send_frame(sock, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode()
    if len(body) > MAX_FRAME:
        raise RyaError(E_PROTOCOL, f"Frame of {len(body)} bytes exceeds the {MAX_FRAME} limit.",
                       hint="Broker calls carry results, not streams. Write large "
                            "payloads through ctx.files instead.")
    sock.sendall(HEADER.pack(len(body)) + body)


# How long a peer gets to finish a frame it has already started. Separate from the
# connection's own idle timeout, and short, because the two situations are different:
# a fork sitting idle between calls is normal and may last a whole run, while a peer
# that has announced a length and then stopped sending is either broken or holding a
# broker thread on purpose. Without this split, a single partial frame pins a thread
# for the life of the process — a denial of service available to any tenant, which is
# not what the mediated surface is for.
BODY_TIMEOUT = 30.0


def _recv_exactly(sock, n: int, *, timeout: Optional[float] = None) -> bytes:
    chunks, got = [], 0
    previous = sock.gettimeout()
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        while got < n:
            block = sock.recv(min(65536, n - got))
            if not block:
                raise EOFError("broker connection closed mid-frame")
            chunks.append(block)
            got += len(block)
    finally:
        if timeout is not None:
            sock.settimeout(previous)
    return b"".join(chunks)


def recv_frame(sock, *, body_timeout: float = BODY_TIMEOUT) -> dict:
    header = _recv_exactly(sock, HEADER.size)
    (length,) = HEADER.unpack(header)
    if length > MAX_FRAME:
        # Refuse before allocating. An unauthenticated peer announcing a 4 GiB
        # frame must not be able to make the broker reserve it.
        raise RyaError(E_PROTOCOL, f"Peer announced a {length}-byte frame; the limit is {MAX_FRAME}.")
    try:
        body = _recv_exactly(sock, length, timeout=body_timeout)
    except socket.timeout as exc:
        raise RyaError(
            E_PROTOCOL,
            f"Peer announced a {length}-byte frame and sent less within "
            f"{body_timeout:.0f}s.",
            hint="The connection is closed rather than waited on: a half-sent frame "
                 "desynchronises the stream, and waiting indefinitely would let one "
                 "peer hold a broker thread.") from exc
    try:
        out = json.loads(body.decode())
    except Exception as exc:  # noqa: BLE001
        raise RyaError(E_PROTOCOL, f"Peer sent a frame that is not JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise RyaError(E_PROTOCOL, "Peer sent a frame that is not a JSON object.")
    return out


# ---- capabilities -----------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """Authority for one run: who, which agent, which run, until when.

    Short-lived and run-scoped because of where it has to live. The template
    process imports the tenant's bundle, so anything it holds is reachable by
    tenant code — meaning a long-lived credential there would be a long-lived
    credential in the tenant's hands. Binding the token to a run and a deadline
    makes the worst case "authority over a run this agent was legitimately given",
    which is inside its own trust domain and therefore not a boundary crossing.

    ``dispatch`` is empty for the template's own handshake capability, which carries
    no data authority at all — see :data:`HANDSHAKE_METHODS`. A dispatch capability
    names no run, because a fork claims its own work and does not know which run it
    will serve until it has; what it may touch is decided by what it claimed, and
    the broker tracks that per connection.

    ``version_id`` is carried so the broker can force the version filter on a claim.
    A fork must not be able to claim a run pinned to code it is not running — that
    is D12, and under mediation it stops depending on the claimer's own honesty.
    """

    workspace: str
    agent: str
    dispatch: str = ""
    version_id: str = ""
    expires_at: float = 0.0
    nonce: str = ""

    def payload(self) -> dict:
        return {"ws": self.workspace, "agent": self.agent, "d": self.dispatch,
                "vid": self.version_id,
                "exp": round(self.expires_at, 3), "n": self.nonce}

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at

    def describe(self) -> dict:
        return {"workspace": self.workspace, "agent": self.agent,
                "dispatch": self.dispatch or None,
                "versionId": self.version_id or None,
                "expiresAt": self.expires_at or None}


def mint_capability(secret: bytes, cap: Capability) -> str:
    """``<b64(payload)>.<b64(hmac)>`` — a signed token, not an opaque handle.

    Signed rather than stored so the broker can validate a capability without
    shared mutable state, which matters because a fork can outlive the request that
    created it and the server must not have to keep a table of live children. The
    secret never leaves the broker process.
    """
    body = json.dumps(cap.payload(), sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(secret, body, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(body).decode().rstrip("=") + "."
            + base64.urlsafe_b64encode(mac).decode().rstrip("="))


def _unpad(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def read_capability(secret: bytes, token: str) -> Capability:
    """Verify and parse. Raises rather than returning None — there is no ambiguous case."""
    try:
        body_b64, _, mac_b64 = (token or "").partition(".")
        body, mac = _unpad(body_b64), _unpad(mac_b64)
    except Exception as exc:  # noqa: BLE001
        raise RyaError(E_CAPABILITY_INVALID, f"Malformed capability token: {exc}") from exc
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise RyaError(
            E_CAPABILITY_INVALID,
            "Capability signature does not verify.",
            hint="The token was not minted by this broker, or it was altered. A "
                 "tenant cannot forge one: the signing secret never leaves the "
                 "broker process.",
        )
    data = json.loads(body.decode())
    cap = Capability(workspace=data.get("ws") or "", agent=data.get("agent") or "",
                     dispatch=data.get("d") or "",
                     version_id=data.get("vid") or "",
                     expires_at=float(data.get("exp") or 0.0),
                     nonce=data.get("n") or "")
    if cap.expired:
        raise RyaError(
            E_CAPABILITY_EXPIRED,
            f"Capability for dispatch '{cap.dispatch or '-'}' expired "
            f"{time.time() - cap.expires_at:.0f}s ago.",
            hint="A capability is scoped to one dispatch and outlives it by a margin, "
                 "not indefinitely. A handler still running past the deadline needs a "
                 "longer lease, not a longer token.",
        )
    return cap


# ---- the allowlist ----------------------------------------------------------

# How an argument is treated server-side. Two kinds, and the distinction is the
# design: FORCE overwrites whatever the caller sent, OWN checks it against what this
# connection actually claimed. Forcing is used where there is one right answer the
# caller does not get to pick (the agent); owning is used where the value is a
# legitimate choice among things the caller was given (which of my runs).
FORCE_AGENT = "force:agent"
OWN_RUN = "own:run"       # a run id this connection claimed or minted
OWN_JOB = "own:job"       # a queue job / turn id this connection claimed
OWN_APPROVAL = "own:approval"  # an approval whose run this connection owns
MINT_RUN = "mint:run"     # the RESULT is a run id; record it as owned


@dataclass(frozen=True)
class Method:
    """One allowlisted call, and what the server does to its arguments.

    ``scopes`` maps a parameter name to a rule. A ``force:`` rule overwrites the
    argument whether or not the caller supplied one, which is the point: a rule that
    only triggers on a *mismatch* is one an attacker learns to satisfy, and one that
    always substitutes has no such edge. An ``own:`` rule refuses instead of
    substituting, because substituting a run id would write this run's journal under
    another run's identity — quietly wrong is worse than refused.
    """

    name: str
    scopes: Mapping[str, str] = field(default_factory=dict)
    writes: bool = False
    mint: str = ""
    note: str = ""


def _m(name: str, **kw) -> Tuple[str, Method]:
    return name, Method(name=name, **kw)


# The store surface tenant code legitimately reaches, derived from what
# `sdk/context.py` actually calls — not from what `Store` offers. Anything the ctx
# surface does not call has no business being reachable, and the two lists drifting
# apart is a signal rather than a nuisance: a new ctx method that needs a new store
# call has to be added here on purpose.
STORE_METHODS: Dict[str, Method] = dict([
    # -- the run itself. `save_run` takes the whole dict, so the id inside it is
    #    what gets checked; see server.py's `_rescope`.
    _m("save_run", scopes={"run": OWN_RUN}, writes=True),
    _m("get_run", scopes={"run_id": OWN_RUN}),
    _m("journal_append", scopes={"run_id": OWN_RUN}, writes=True,
       note="durability for a run this fork was given"),
    _m("new_run_id", mint=MINT_RUN,
       note="a sub-run's id is minted here so the broker knows to expect writes to it"),
    # -- memory (scope strings are checked by _Memory before they get here)
    _m("load_memory"), _m("save_memory", writes=True),
    # -- files
    _m("save_file", writes=True), _m("get_file"), _m("read_file"), _m("list_files"),
    # -- approvals: creating one pauses this run, resolving one resumes it
    _m("create_approval", scopes={"run_id": OWN_RUN}, writes=True),
    _m("get_approval", scopes={"approval_id": OWN_APPROVAL}),
    _m("save_approval", writes=True, scopes={"approval": OWN_APPROVAL}),
    # -- jobs and cron fan-out
    _m("create_job", scopes={"run_id": OWN_RUN, "agent": FORCE_AGENT}, writes=True,
       note="D22: the agent on the row is the platform's, so a tenant cannot "
            "enqueue work that a sibling agent's worker will claim"),
    _m("create_job_group", writes=True), _m("group_job_done", writes=True),
    _m("get_job", scopes={"job_id": OWN_JOB}),
    _m("save_job", scopes={"job": OWN_JOB}, writes=True),
    _m("list_jobs", note="the tenant's own due-job list; the fan-out reads it"),
    # NOTE: `queue_get` and `queue_save` are deliberately NOT here. The queue's
    # lease lifecycle — claim, heartbeat, complete, fail — checks the holder in
    # `queue._check_holder`, which is *caller-side* code, so all four verbs had to
    # become services rather than just the claim. See SERVICE_QUEUE_* below.
    # -- sessions and messages
    _m("create_session", scopes={"agent": FORCE_AGENT}, writes=True),
    _m("get_session"), _m("find_session", scopes={"agent": FORCE_AGENT}),
    _m("append_message", writes=True), _m("list_messages"),
    # -- connections: reads are REDACTED server-side (see server.py), writes seal
    _m("get_connection"), _m("list_connections"),
    _m("upsert_connection", writes=True,
       note="a handler may store a credential it obtained; the seal happens "
            "server-side, so the seal key stays out of the tenant process"),
    # -- turn stream: the durable frame buffer for a turn this fork claimed
    _m("stream_append", scopes={"turn_id": OWN_JOB}, writes=True),
    _m("stream_read", scopes={"turn_id": OWN_JOB}),
    # -- governance READS the runtime enforces (kill switches, guard policy)
    _m("policy_get", note="read-only: §11.2's carve-out is about the write side"),
    # -- trivia
    _m("now_iso"),
])

# Services the broker performs rather than proxies. Two reasons a method is here
# rather than in the table above: a credential must not travel (llm, tools.http,
# egress, secrets), or the authorization logic itself lives in the caller and has to
# be moved across the boundary (queue.claim, jobs.claimDue).
SERVICE_LLM = "llm.call"            # D30: pooled provider key, allowlist, quota, meter
SERVICE_HTTP_TOOL = "tools.http"    # the connection secret is injected server-side
SERVICE_EGRESS = "egress.fetch"     # D24: the only route out of a sandbox
SERVICE_SECRET = "secrets.get"      # the tenant's OWN declared secrets
SERVICE_QUEUE_CLAIM = "queue.claim"   # D22/D12 filters forced platform-side
SERVICE_JOB_CLAIM = "jobs.claimDue"   # same, for the `jobs`/`cron` primitive
# The rest of the lease lifecycle. Services for the same reason the claim is: the
# holder check is caller-side, so a fork holding the lease is the thing deciding
# whether it holds the lease. Moving all four keeps the invariant that a queue row's
# state transitions are made by the platform.
SERVICE_QUEUE_COMPLETE = "queue.complete"
SERVICE_QUEUE_FAIL = "queue.fail"
SERVICE_QUEUE_HEARTBEAT = "queue.heartbeat"

SERVICE_METHODS: Dict[str, Method] = dict([
    _m(SERVICE_LLM, writes=True,
       note="the meter row is written here, by the party that made the call"),
    _m(SERVICE_HTTP_TOOL, writes=False),
    _m(SERVICE_EGRESS, writes=False),
    _m(SERVICE_SECRET, writes=False),
    _m(SERVICE_QUEUE_CLAIM, writes=True,
       note="the reap, the claim, the version filter and the agent filter, all on "
            "the platform's side. Establishes what this connection then owns"),
    _m(SERVICE_JOB_CLAIM, writes=True),
    _m(SERVICE_QUEUE_COMPLETE, writes=True),
    _m(SERVICE_QUEUE_FAIL, writes=True),
    _m(SERVICE_QUEUE_HEARTBEAT, writes=True,
       note="the lease extension is clamped server-side: a fork must not be able to "
            "grant itself an unbounded hold on an item"),
])

# What the template may call with its handshake capability — which names no
# dispatch, so nothing on the data surface will accept it. Kept explicit rather than
# derived from "methods with no scope rule": `load_memory` has no scope rule either
# and must not be reachable before an item is dispatched.
HANDSHAKE_METHODS = frozenset({"ping", "now_iso", "policy_get"})

# Methods a store may not implement and the mediated runtime must not require. The
# client answers these locally, because `Engine.__init__` calls `ensure()` on every
# construction and a fork has no business creating schema.
LOCAL_NOOP_METHODS = frozenset({"ensure", "close"})

ALL_METHODS: Dict[str, Method] = {**STORE_METHODS, **SERVICE_METHODS,
                                  "ping": Method(name="ping")}


def resolve_method(name: str) -> Method:
    """The allowlist gate. Refuses by default, and names what it refused.

    The message says whether the method *exists* on ``Store``, because the two
    cases mean different things to whoever is reading the error: an unknown name is
    usually a typo or a version skew, and a known-but-denied one is a handler
    reaching for something D18 took away on purpose, which needs the reason.
    """
    method = ALL_METHODS.get(name)
    if method is not None:
        return method
    from ..store import Store

    known = callable(getattr(Store, name, None))
    raise RyaError(
        E_METHOD_DENIED,
        f"The broker does not expose '{name}'." + (
            " That method exists on the store but is not on the tenant-reachable "
            "allowlist." if known else ""),
        hint=("Governance, billing and execution-plane methods are withheld on "
              "purpose (D18): the tenant process holds no credentials and gets a "
              "mediated surface, not a database. See broker/protocol.py for the "
              "list and the reason each omission is there."
              if known else
              f"Allowlisted: {', '.join(sorted(ALL_METHODS))}."),
    )


# ---- envelopes --------------------------------------------------------------

def request(method: str, args: Optional[list] = None,
            kwargs: Optional[dict] = None, *, capability: str = "",
            seq: int = 0) -> dict:
    return {"v": PROTOCOL_VERSION, "seq": seq, "method": method,
            "args": list(args or []), "kwargs": dict(kwargs or {}),
            "cap": capability}


def ok(seq: int, result: Any) -> dict:
    return {"v": PROTOCOL_VERSION, "seq": seq, "ok": True, "result": result}


def err(seq: int, exc: BaseException) -> dict:
    """Errors cross the wire as data, keeping the code and hint.

    A ``RyaError`` raised in the broker has to arrive in the tenant process as the
    same ``RyaError``, or a handler's ``except RyaError as e: if e.code == ...``
    stops working the moment mediation is turned on — and the whole design depends
    on the mediated path being behaviourally identical to the direct one.
    """
    code = getattr(exc, "code", None) or "E_RUNTIME"
    return {"v": PROTOCOL_VERSION, "seq": seq, "ok": False,
            "error": {"code": code, "message": str(exc),
                      "hint": getattr(exc, "hint", None),
                      "httpStatus": getattr(exc, "http_status", None)}}


def raise_from_error(payload: dict) -> None:
    """Rebuild and raise the far side's error. Never returns."""
    e = payload.get("error") or {}
    raise RyaError(e.get("code") or "E_RUNTIME", e.get("message") or "broker call failed",
                   hint=e.get("hint"), http_status=e.get("httpStatus"))
