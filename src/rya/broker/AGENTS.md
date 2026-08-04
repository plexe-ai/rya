# `rya.broker` - the credential boundary (D18)

Tenant code holds a **socket and a short-lived capability**. This package holds
everything else: the database DSN, the seal keys, the pooled provider key, the object
store credential, and the only route out of a sandbox.

Read `protocol.py` first. It is the security-relevant part, it imports neither of the
other two modules, and the allowlist plus the reason for each omission is in its
docstring.

## Files

- `protocol.py` - the wire (length-prefixed **JSON**, never pickle), the method
  allowlist, the scope rules, and capabilities. No imports from server or client.
- `server.py` - runs in the claimer. Holds the credentials, re-scopes every identity
  argument, performs the six services, writes the meter row.
- `client.py` - runs in the sandbox. `BrokerStore` is duck-typed to `Store` closely
  enough that `RuntimeContext` cannot tell.
- `inventory.py` - the credential audit **and** the scrub, from one list.

## Where the boundary actually is

Not "the tenant cannot reach the database" - that is a consequence. The property is
that **every argument describing whose data this is gets overwritten or checked by
code the tenant does not run.** Two rules, and the difference matters:

| rule | behaviour | used where |
|---|---|---|
| `force:` | overwrites whatever the caller sent | there is one right answer (the agent on a created job) |
| `own:` | **refuses** | the value is a legitimate choice among things this fork was given (which of my runs) |

`own:` refuses rather than substituting because substituting a run id would write one
run's journal under another run's identity. Quietly wrong is worse than refused.

## Ownership comes from the claim, not the token

A fork claims its own work (D27 keeps claim and execute together), so it does not know
its run id when the capability is minted. The broker records per connection what it
claimed and which runs those items belong to - including a resume's run, resolved
through the approval - and checks every write against that set.

**Keyed by dispatch, not by connection** (changed in Phase 5). The old keying was "a
connection is one fork's lifetime, so the scope is naturally one dispatch" — which was
approximately true and broke on the case D30 cares about most. A mediated
`ctx.llm.respond` streams; `turns.py` wires `on_token` to `store.stream_append`; so
every token is a store write from *inside* the model call. A nested call cannot share a
socket with a request still in flight on it, so it opens its own — and with
per-connection ownership that second connection had no authority to write the run it
was streaming.

`cap.dispatch` is not a weaker key: a capability is HMAC-signed by this process and
issued per fork, so presenting one *is* being that dispatch, whichever socket it arrives
on. The connection was only ever a proxy. The claimer calls `release(dispatch)` when the
item finishes, because the broker cannot tell "the last socket closed" from "the work
finished".

## Why the surface is bigger than `ctx`

Because D27 put the claim loop inside the fork. So the mediated surface is also what
`turns.execute_pending` and `Engine.work_once` call, and handing *those* over
unmediated would be worse than the status quo:

- `queue.claim` applies D22's agent filter **in the caller's Python, after the claim**
  (a deliberate choice recorded in `queue.py`, to avoid forking the two store
  backends). A hostile handler simply would not release the sibling's job.
- `queue._check_holder` decides the lease **in the caller's Python** too.

Both are sound when the caller is the platform and worthless when it is a fork with the
tenant's bundle in it. So all five verbs - claim, claim-due, complete, fail, heartbeat -
are **services**, with the agent, the version, the worker id and the lease deadline
forced platform-side. The mediated claimer ends up *more* constrained than an
unmediated one, which is the useful inversion.

## Load-bearing details (do not break)

- **`BrokerStore.__getattr__` raises `AttributeError` for anything off the allowlist**,
  rather than presenting a callable that refuses on the wire. `RuntimeContext` branches
  on `getattr(store, "meter_append", None)`, so a refusing callable would make `_meter`
  fire a denied call per model step and swallow the error - metering that *looks* like
  it works. Absent means absent.
- **The wire is JSON.** `multiprocessing.Connection` sends pickles and the peer here is
  tenant code; a pickle from a hostile peer is arbitrary code execution in the parent,
  which is the direction D18 exists to close.
- **A body deadline separate from the idle timeout.** An idle connection between calls
  is normal and may last a whole run; a peer that announced a length and stopped
  sending is holding a thread. Without the split, one partial frame is a denial of
  service any tenant can run.
- **The signing secret never leaves the process.** A capability is only forgeable by
  something that can read the broker's memory, which is the same as being the broker.
- **`meter_append` is not on the allowlist and must not be added.** With a pooled
  provider key (D30) a tenant that can write meter rows writes its own invoice.
- **`BrokerClient._lock` is not reentrant, and must not become one.** A reentrant lock
  would be worse than the deadlock it looks like it fixes: the nested request would go
  out on a socket with a reply still in flight and the two exchanges would read each
  other's frames. The nesting is handled by a thread-local depth and a temporary socket.
- **One broker serves every agent at the wide claimer scope**, and what became a lookup
  is exactly what a *manifest declares*: model routes, secrets, and the
  agent-qualified guard policy (D28). What stayed a constructor argument is what came
  from the *deployment*: the store, the key ring, the quota check. If you find yourself
  adding a per-agent thing that came from the deployment, or a per-deployment thing that
  came from a manifest, one of the two is in the wrong place.
- **`public_routes` lives on the server, next to the credential.** It used to be
  computed by the claimer's executor, which was harmless while the executor also held
  the real config and became a second place to get it wrong the moment the config went
  per-agent. One projection of a `RunConfig` crosses the boundary; that is it.

## What the tenant legitimately keeps

`ctx.secrets.get` still works. D18 removes the **platform's** credentials; a secret the
tenant declared for its own handler is theirs, and the inventory classifies it as
`tenant` rather than a violation - otherwise it would report a finding on every
deployment and be ignored.

## What the inventory cannot prove

Freed heap. A value that transited this process before the scrub may still be resident,
and CPython offers no way to overwrite it. So the scrub is defence in depth.

**And the place Phase 4 said the criterion is *genuinely* met took two more phases to
become runnable.** `ContainerDriver.sandbox_env` does build the environment from `{}` -
the credentials are never added, so there is no list to forget to update - but the
process it configured was the **claimer**, which needs the DSN in order to *be* the
broker. Both were the same container. Phase 5 named the arrangement that resolves it
(D32) and made the launch gate refuse; Phase 6 built it.

The resolution is worth reading, because the obvious diagnosis was wrong.
`sandbox_env` was never the defective function — it and `worker_env` describe opposite
processes and both were correct. What was missing was the **second container**. So
`claimer_env` now sits beside `sandbox_env`, `rya template-host` is what the sandbox
half runs, and the mediation boundary that actually executes is a container boundary
with a different uid and no shared PID namespace. The scrub in `_template_main` is
still there and is still second-best, and it is now defence in depth behind a real
boundary rather than the only one. Do not close any future gap here by adding a DSN to
`sandbox_env`.
