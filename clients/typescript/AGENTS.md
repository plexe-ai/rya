# `@plexe/rya` - TypeScript client

A typed client for the Rya HTTP API. The Rya runtime is Python; this is what a
TS/JS app (or an external worker) uses to drive it. Uses global `fetch` (Node 18+).

## Files

- `src/index.ts` - `RyaClient` + the queue worker + types. Compiles to `dist/`.

## Surface

- **Runs/approvals**: `triggerEvent`, `inbound`, `getRun`, `getTrace`,
  `listRuns`, `listApprovals`, `approve`, `reject`, `health`.
- **Durable turns**: `startTurn`, `streamTurn(id, afterSeq, {untilFinal})` -
  async iterator over SSE frames (token/trace/message/ui/run/error) that resumes
  across dropped connections via `Last-Event-ID`; with `untilFinal` it tails
  through an approval pause to the terminal frame. `streamEvent` for the
  non-durable SSE endpoint.
- **Queue**: `enqueueJob`, `enqueueJobBatch`, `getQueueJob`, `cancelQueueJob`,
  `queueStats`; worker-side `claimQueueJobs`, `heartbeatQueueJob`,
  `completeQueueJob`, `failQueueJob`.
- **`createQueueWorker({client, handlers, ...})`** - the claim/heartbeat/execute/
  report loop; heartbeat aborts the handler's `AbortSignal` on cancel or lost
  lease. Rya owns retries/backoff/DLQ; the worker never retries locally.

## Notes

- `dist/` is committed (built output). Run `npx tsc` after editing `src/`.
- Frame types are yielded generically, so new turn-stream frame kinds (e.g.
  `ui`) work without client changes.
