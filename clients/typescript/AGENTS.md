# `@plexe/rya` — TypeScript client SDK

The TS side of PLATFORM_DESIGN §11 item 13. Rya's runtime is Python; this is what
a TS/JS app, an ops console, or an external worker uses to drive it over HTTP.

§2 is the whole scope rule: *a client repo needs `rya-sdk` and a deploy token. It
never imports the runtime, never runs a server, and never knows which deployment
it is running in.* So there is **no** `defineAgent`, no `ctx`, no tool registry
here — governed execution lives in Python (§3), and a TS reimplementation would
be a second thing to keep honest. Every method corresponds to a route that
actually exists in `src/rya/api/app.py`.

Zero runtime dependencies (global `fetch` + web streams, Node 18+). Full prose
docs: [`docs/typescript-sdk.md`](../../docs/typescript-sdk.md).

## Files

| File | Contents |
|------|----------|
| `src/index.ts` | The barrel — public exports only. Entry point (`dist/index.js`). |
| `src/errors.ts` | `RyaError` (stable `code` + `hint` + `exitCode`), `RyaStreamError`, envelope normalization for all four shapes the API emits. |
| `src/types.ts` | Wire types read off `app.py` and the store records. `isFrame()` narrows a turn frame by kind. |
| `src/http.ts` | `Transport`: URL, auth headers, timeout, signal linking, error mapping. Every method funnels through `request()` / `stream()`. |
| `src/sse.ts` | Spec-shaped SSE parser. Pure generator over a byte stream, so resume is testable with no server. |
| `src/turns.ts` | `streamTurn` / `streamEvent` — the resumable tail (D6). |
| `src/queue.ts` | `createQueueWorker` — the claim/heartbeat/execute/report loop (D14). |
| `src/client.ts` | `RyaClient`, the whole method surface. |
| `test/*.test.mjs` | `node --test`, fetch stubbed. No server, no network. |
| `checks/usage.ts` | Compile-only. Asserts `isFrame` narrows, nothing is `any`, and `data`/`payload`/`body` stay `unknown` (`tsconfig.check.json`). |
| `smoke.mjs` | Drives a live `rya serve`; proves the wire shapes match. |

## Surface

- **Runs & events** — `triggerEvent`, `sendEvent`, `inbound`/`inboundRaw`,
  `getRun`, `getTrace`, `listRuns`, `ingestRun`, `getAgent`, `health`, `info`.
- **Durable turns (D6)** — `startTurn`, `streamTurn`, `streamEvent`,
  `reclaimTurns`. Async iterators, resumable by `Last-Event-ID`, cancellable by
  `AbortSignal`.
- **Approvals** — `listApprovals`, `approve`, `reject` (the response's `turnId`
  is where the continuation streams).
- **Sessions** — `listSessions`, `findSession`, `getSession`, `listMessages`.
- **Files** — `uploadFile`, `presignFile`, `confirmFile`, `listFiles`, `getFile`.
- **Knowledge** — `knowledge`, `searchKnowledge`.
- **Governance** — `listTools`, `setToolPermission`, `clearToolPermission`,
  `toolLog`, `getGuard`, `putGuard`, `testGuard`, `guardLog`.
- **Usage (D10)** — `getUsage`, `getUsageBy` (from the durable meter, not traces).
- **Deployments (D11/D12/§9)** — `listVersions`, `getVersion`, `pinnedRuns`,
  `retireVersion`, `listEnvironments`, `describeEnvironment`, `promote`,
  `rollback`, `environmentHistory`, `listWorkers`.
- **Queue (D14)** — `enqueueJob`, `enqueueJobBatch`/`enqueueJobs`,
  `listQueueJobs`, `getQueueJob`, `cancelQueueJob`, `retryQueueJob`,
  `queueStats`; worker-side `claimQueueJobs`, `heartbeatQueueJob`,
  `completeQueueJob`, `failQueueJob`; and `createQueueWorker`.

Deliberately absent: `/ws` (D6 makes the durable buffer the one streaming path),
`/v1/*` tenancy and key CRUD (§2 — a client gets a token, it does not mint them),
`/evals` (server-disk, single-tenant, a CLI concern), `/slack/events`, `/console`.

## Notes for editors

- **`dist/` is gitignored, not committed.** Run `npm run build` before `smoke.mjs`
  or the tests; `npm test` builds first.
- **Relative imports must end in `.js`.** The package is ESM and `smoke.mjs`
  loads `dist/` under Node, which does not resolve extensionless specifiers.
- **`data` on a turn frame is `unknown`, on purpose.** Frame kinds are additive —
  `ui` arrived after `token`/`trace`/`message`/`run` — so a closed union would
  have made a server-side addition a compile error in every deployed client.
  Narrow with `isFrame(frame, "token")`.
- **`RyaErrorCode` is widened with `(string & {})`** for the same reason, and
  because the server already emits codes absent from `errors.py`
  (`E_NOT_FOUND`, `E_SESSION_NOT_FOUND`).
- **Never idle-spin the worker loop.** A claim that returns instantly with no
  jobs must hit a `setTimeout`; a promise loop that only ever reaches microtasks
  starves every timer in the process, heartbeats included (`idleDelayMs`).
- Server-side gaps this client works around are listed at the bottom of
  `docs/typescript-sdk.md`.

## Commands

```
npm install
npm run typecheck    # tsc --noEmit, strict
npm test             # build + node --test + the compile-only type checks
npm run check:types  # checks/usage.ts against the emitted .d.ts
npm run build
npm run smoke        # needs a live `rya serve` at $RYA_URL
```
