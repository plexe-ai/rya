# TypeScript SDK (`@plexe/rya`)

The TypeScript client for the Rya platform, in [`clients/typescript`](../clients/typescript).

Rya's runtime is Python. PLATFORM_DESIGN §3 draws the boundary at *platform code
vs. client code*, and §2 states the contract this package implements: **a client
repo needs `rya-sdk` and a deploy token. It never imports the runtime, never runs
a server, and never knows which deployment it is running in.** So this is an HTTP
client, not a second runtime — there is no `defineAgent`, no `ctx`, and no tool
registry, because governed execution (permission resolution, the journal, the
guard verdict, approvals) is server-side and a TS reimplementation would be a
second thing to keep honest.

One half of that contract is Python's: the deploy token is spendable by
`rya publish`, not from here. This client **drives** an agent and **operates** its
versions; it does not ship one.

Zero runtime dependencies: global `fetch` and web streams, both built in from
Node 18. It runs unchanged in the browser, a worker, or an edge runtime.

```bash
cd clients/typescript && npm install && npm run build
```

## The three things it covers

| Surface | Why a TS client gets it |
|---------|-------------------------|
| **The agent platform** | Events, runs, traces, approvals, sessions, files, usage. Plus versions and environments to *read, promote, roll back and retire* — **not create**; publishing is Python's (see below). The app driving the agent is the client. |
| **Durable streaming (D6)** | Every stream is a tail over the durable turn buffer, resumed by `Last-Event-ID`. A dropped socket is a cursor, not a lost turn. |
| **The durable job API (D14)** | `/queue/*` is deliberately SDK-free so foreign code can drive it, and the design names TypeScript DAG workers as the consumer. |

## Quick start

```ts
import { RyaClient, isFrame } from "@plexe/rya";

const rya = new RyaClient({
  baseUrl: "http://localhost:8787",
  token: process.env.RYA_TOKEN,     // operator token, rya_sk_… API key, or a user JWT
});

const run = await rya.triggerEvent({ email: "ada@example.com" });
if (run.status === "waiting_approval") {
  const [pending] = await rya.listApprovals("pending");
  await rya.approve(pending.id);
}
```

## Errors carry a code and a hint

`errors.py`: a failure must carry a *stable* code plus a next action "so a coding
agent can branch on the failure deterministically instead of scraping human
prose". `RyaError` carries that pair through the HTTP hop unchanged.

```ts
import { RyaError } from "@plexe/rya";

try {
  await rya.retireVersion(versionId);
} catch (err) {
  if (err instanceof RyaError && err.code === "E_VERSION_IN_USE") {
    const { runs } = await rya.pinnedRuns(versionId);   // err.hint says this
    console.error(err.hint, runs.length);
  }
}
```

| Field | Meaning |
|-------|---------|
| `code` | The `E_*` code. Widened to `string`, because the server emits codes not in `errors.py`'s table (`E_NOT_FOUND`, `E_SESSION_NOT_FOUND`) and will add more. |
| `hint` | The suggested next action, or `null`. |
| `exitCode` | The semantic exit bucket, when the server sent one. |
| `httpStatus` | `0` for transport failures and timeouts. |
| `codeFromServer` | `false` when the code was **inferred** from the status because the response had no envelope — see the caveats below. |
| `retryable` | 5xx, transport failure, or `E_TIMEOUT`. |
| `body` | The raw payload, for anything not modelled yet. |

All four envelope shapes the API actually produces are handled:
`{detail: {...}}`, `{ok: false, error: {...}}`, a bare `{code, ...}`, and
FastAPI's `{detail: [{loc, msg}]}` validation array. A non-JSON body (a proxy's
HTML 502, FastAPI's plain-text 500) becomes a typed error too, with
`codeFromServer: false`.

## Streaming: resumable by construction

D6 makes the durable turn buffer the one streaming path. The executor *appends*
frames; the endpoint *tails* them by sequence. The client's job is to hold the
cursor and re-send it.

```ts
const { turnId } = await rya.startTurn({ email: "ada@example.com", body: "refund" });

for await (const frame of rya.streamTurn(turnId, { untilFinal: true })) {
  if (isFrame(frame, "token")) process.stdout.write(frame.data.text);
  if (isFrame(frame, "trace")) log(frame.data.kind);
  if (isFrame(frame, "run") && frame.data.status === "waiting_approval") {
    const [pending] = await rya.listApprovals("pending");
    await rya.approve(pending.id);   // the continuation lands on THIS stream
  }
}
```

Guarantees, each covered by a test in `test/stream.test.mjs`:

- **Resume, don't replay.** Reconnects send `Last-Event-ID` (and `?after=`, for
  proxies that strip unknown request headers) set to the last frame *actually
  yielded*. Frames at or below the cursor are dropped client-side too, so even a
  server that resumed inclusively could not double-deliver.
- **An idle close is protocol, not failure.** The server closes an idle tail
  deliberately (`RYA_TURN_STREAM_IDLE_SECONDS`, 60s) so proxies do not. A clean
  re-open does not spend the error budget; only attempts that yield no frames do.
- **Give up loudly.** Exhausting `maxReconnects` throws `RyaStreamError`, which
  carries `lastEventId`. Returning quietly would make "the turn finished" and
  "we stopped watching" the same event.
- **Cancellable.** Pass `signal`; the generator returns and the socket closes.
  Breaking out of the `for await` closes it too.
- **`untilFinal`.** A `run` frame with `waiting_approval` is a *pause marker*.
  Default `false` stops there; `true` tails through the approval to the real
  terminal frame.

`streamEvent()` triggers and streams in one call. Its first frame is
`turn {turnId}` — the resume handle — and if the connection drops it transparently
continues on the durable tail from the same cursor.

## The durable job API (D14)

A queue job is **not** a governed run: it gets leases, retries with backoff,
dead-lettering and crash reclaim, and nothing else — no permission resolution, no
journal, no guard verdict, no approval gate.

```ts
import { createQueueWorker } from "@plexe/rya";

await rya.enqueueJob("render-pdf", { docId: 7 }, { jobId: "doc-7", maxAttempts: 3 });

const worker = createQueueWorker({
  client: rya,
  concurrency: 8,
  handlers: {
    "render-pdf": async (payload, signal) => renderPdf(payload, { signal }),
  },
});
await worker.run();   // worker.stop() cancels the in-flight long poll
```

The loop claims with a lease, heartbeats while running, and reports once. A
`cancelRequested` flag or a lost lease (`409 E_QUEUE_CONFLICT`) aborts the
handler's `AbortSignal`. **Rya owns retries** — a thrown handler error is reported
once and never retried locally, because a client-side retry would silently spend
the attempt budget and defeat the server's backoff.

## What is deliberately not here

| Not exposed | Why |
|-------------|-----|
| Defining agents, tools, `ctx` | §3: governed execution is platform code. Python owns it. |
| `publish` (`POST /agents/{id}/versions`) | Publishing is not an HTTP call, it is a *build*: walk the project, honour `.ryaignore`, and reproduce a content hash that folds in the **Python** SDK version. A TS reimplementation would disagree with the platform about the digest of byte-identical trees and fail `E_BUNDLE_MISMATCH` — a second hash function to keep honest, for a step that runs in CI where `rya publish` already lives. `promote`/`rollback`/`retireVersion` *are* here because a TS app operates versions; it does not create them. |
| `/ws` | D6 makes the durable buffer the one streaming path; resumable SSE strictly dominates a socket that loses frames on drop, and `WebSocket` is not a stable global on Node 18. |
| `/v1/signup`, `/v1/login`, `/v1/workspaces`, `/v1/projects`, key and member CRUD | §2: a client repo gets a *token*. A client that can mint its own workspace keys is a control plane, not a client. That is dashboard and CLI territory. |
| `/evals`, `/evals/run` | Runs against the project directory on the server's disk, single-tenant only (it 400s in the hosted mode a TS client would talk to). A CLI/CI concern. |
| `/slack/events` | An inbound adapter Slack calls, not a method a client invokes. |
| `/console` | The built-in console page's private aggregate; its shape is not a contract. |
| `/mcp` | A protocol mount with its own clients. |

## Testing

```bash
npm run typecheck    # tsc --noEmit, strict
npm test             # builds, runs node --test (fetch stubbed, no server), then the type checks
npm run check:types  # compiles checks/usage.ts against the emitted .d.ts
npm run smoke        # drives a live `rya serve`
```

`checks/usage.ts` is never executed. It asserts the things a unit test cannot:
that `isFrame` genuinely narrows, that no public type widened back to `any`, and
— via `@ts-expect-error` — that `frame.data`, `job.payload` and `err.body` stay
`unknown` until the caller narrows them.

## Caveats found in the HTTP surface

These are server-side and the client works around them; they are worth fixing.

1. **`POST /agents/{id}/events` does not catch `RyaError`.** Unlike `/inbound`,
   it lets one escape FastAPI as a bare `500 Internal Server Error` with no
   envelope, so a guard block or a validation failure loses its code. The client
   infers `E_RUNTIME` and sets `codeFromServer: false`.
2. **`E_NOT_FOUND` and `E_SESSION_NOT_FOUND` are not declared in `errors.py`.**
   `GET /files/{id}` and the session routes return them anyway, hence the widened
   `RyaErrorCode`.
3. **Several 404s carry a code and no message** (`GET /queue/jobs/{id}` raises
   `{"code": "E_JOB_NOT_FOUND"}`), so `message` is synthesised client-side.
4. **The turn handle is only delivered in-band** as `streamEvent`'s first frame.
   Losing the connection before it arrives leaves a durable turn nobody can name.
   Use `startTurn()` + `streamTurn()` when that matters.
5. **CORS allows `GET, POST, PUT` only.** No route this SDK uses needs `DELETE`,
   but a browser client cannot reach the `/v1/workspaces/**` delete routes.
