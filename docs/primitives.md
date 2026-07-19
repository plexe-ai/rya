# Rya Primitives

Rya exposes agent backend primitives, kept simple and composable.

## Agent identity & manifest

Every agent is declared in `rya.agent.yaml`:

```yaml
name: support-followup-agent
runtime: python
entrypoint: src/agent.py
version: 0.1.0

model:
  default: mock-llm
  fallback: mock-llm-mini

memory:
  type: managed
  collections: [conversations, customer_context]

tools:
  - id: crm.lookup
    permission: allowed
  - id: calendar.read
    permission: read_only
  - id: email.send
    permission: approval_required

models:
  - id: churn-risk-v1
    type: custom
    permission: allowed

channels:
  - type: webhook
    path: /inbound

triggers:
  - id: daily-followups
    type: cron
    schedule: "0 9 * * *"
    handler: daily_followup

approvals:
  default: required_for_external_actions

observability:
  logs: true
  traces: true
  audit: true
```

## Runtime context (`ctx`)

Handlers receive a context exposing every primitive:

| Interface | Purpose |
|-----------|---------|
| `ctx.llm.respond(system, input)` | Call the default LLM (mock) |
| `ctx.models.call(id, input)` | Call a registered model |
| `ctx.memory.get/set/append/search` | State, conversation history, collections |
| `ctx.tools.call(id, input)` | Call a permissioned tool |
| `ctx.channels.send(channel, message)` | Send via a channel |
| `ctx.jobs.schedule(handler, payload, delay_seconds)` | Queue background work |
| `ctx.cron.schedules()` | Inspect cron triggers |
| `ctx.approvals.request(title, body, action)` | Human gate (pauses the run) |
| `ctx.logs.info/debug/warning/error` | Structured logs |
| `ctx.traces.event(name, data)` | Custom trace spans |
| `ctx.secrets.get(name)` | Secret values (never persisted/traced) |
| `ctx.events.emit(type, payload)` | Emit an event |

## Tool permissions

`read_only` · `allowed` · `approval_required` · `disabled`.

`approval_required` tools cannot be called directly — they must flow through
`ctx.approvals.request(action={"tool": ..., "input": ...})`, which executes the
tool only after a human approves.

## Approval lifecycle

`pending → approved | rejected | expired | cancelled`. The runtime pauses a run
on `pending` and resumes it on `approved` by replaying the handler against the
durable journal.

## Memory scopes

`agent` (default) · `user` · `customer` · `workspace` · `environment`. Pass
`scope=` to any memory call.

## Model routes

`model.routes` names per-purpose models (compose vs extract vs classify), so
sidecar LLM calls stop being hand-rolled clients:

```yaml
model:
  default: claude-opus-4-8
  routes:
    extract:  {model: claude-haiku-4-5, max_tokens: 512}
    classify: {model: claude-haiku-4-5, temperature: 0}
```

`ctx.llm.respond(..., route="extract")` (and `ctx.llm.run(..., route=...)`).
Unset route fields inherit from the model block; traces label calls
`route:model` so cost per purpose is visible.

## Server-side arg pinning

Never trust the model (or handler input) for scoped identifiers. A tool decl
may pin arguments to trusted sources, resolved by the runtime at call time and
overwriting whatever the caller supplied:

```yaml
tools:
  - id: crm.lookup
    permission: allowed
    pin:
      email: event.payload.email    # from the triggering event
      region: ap-south-1            # literal
      owner: identity.sub           # verified caller
      account: memory.agent.account # from scoped memory
```

Pinned fields are recorded in the trace (`pinnedArgs`).

## Runtime kill switches

`PUT /tools/{id}/permission {"permission": "disabled", "reason": ...}` overrides
a tool's permission NOW, without a redeploy - versioned, append-only history,
reflected in `GET /tools` as `effectivePermission`, enforced on the next call
and removed from the `ctx.llm.run` loop immediately. `{"clear": true}` reverts
to the manifest. Unreadable runtime config fails closed.

## Durable chat turns

A plain chat turn (`/events/stream`) is synchronous: a mid-turn server crash
strands the run and a dropped connection loses the stream. A **turn** fixes both
by making the turn a durable, leased, reclaimable job and the stream a resumable
tail over a durable buffer:

- `POST /agents/{id}/turns` → `{turnId}`. Enqueues a `chat-turn` job (so it has a
  lease and is reclaimed if its executor dies) and kicks execution inline.
- `GET /agents/{id}/turns/{turnId}/stream?after=N` → SSE tail of the durable
  buffer. Resumable: reconnect with `?after=<lastSeq>` (or the browser's
  `Last-Event-ID`) to continue exactly where a dropped connection left off. Each
  frame carries `id: <seq>`; ends on the terminal `run`/`error` frame.
- `POST /agents/{id}/turns/reclaim` → runs any pending or crashed (lease-expired)
  turns for the workspace. The durability backstop - call it from a periodic
  sweeper / `rya` worker loop so an interrupted turn always finishes.

A reclaimed re-run appends a `restart` frame then fresh frames (monotonic seqs
preserved). Crash-retry re-runs the handler fresh (same idempotency contract as
any queue job); an approval PAUSE inside a turn is durable via journal replay.
TS client: `startTurn()` + `streamTurn(turnId, afterSeq)` (auto-reconnects from
the last seq on a dropped connection).

**Approvals inside turns**: a `run` frame with status `waiting_approval` is a
pause marker, not the end. Approving (or rejecting) streams the POST-approval
continuation onto the same turn buffer - an `approval.approved` trace frame,
the continuation's trace/token frames (memoized pre-approval steps never
re-stream or re-bill), session replies, then the REAL terminal `run` frame.
TS: `streamTurn(id, -1, {untilFinal: true})` tails through the pause to the
final frame.

**Built-in sweeper**: `rya serve` runs a background reclaim loop
(`RYA_TURN_SWEEP_SECONDS`, default 30, 0 disables) across ALL workspaces, so
crashed/stranded turns always finish with no external cron.

## Token streaming

Two transports, same frames:

- **SSE (default for clients)**: `POST /agents/{id}/events/stream` triggers a
  run and streams it as Server-Sent Events - `token` (LLM chunks), `trace`
  (journaled steps), `message` (session replies for chat agents), and ALWAYS a
  terminal `run` (or `error`) frame, so clients never guess whether more is
  coming. Plain HTTP: works through ALBs, proxies, and `fetch`. The TS client
  wraps it as `for await (const frame of rya.streamEvent(payload))`.
- **WebSocket** (`/ws`): the bidirectional surface for the console and
  long-lived chat sessions; emits the same `token` frames.

Provider streaming is Anthropic/OpenAI SSE (chunked mock offline). Tokens are
not journaled - only the final response is - so a replay after an approval
pause never re-streams.

## Grounding gate

`ctx.guard.check_grounding(text)` verifies every money figure in `text` exists
in a tool output of THIS run. With `grounding: {enabled: true}` in
`rya.guard.yaml`, `ctx.channels.send` enforces it: an outbound message quoting
an amount no tool returned is blocked (`E_GROUNDING_BLOCKED`) and traced.

## Queue: durable jobs for external workers

The `jobs` primitive is handler-bound (Python, executed by `rya worker`). The
**queue** primitive is its polyglot complement: any backend in any language
enqueues jobs over HTTP and runs its own workers against them - Rya owns
durability, retries with exponential backoff, dead-lettering, idempotent
enqueue, per-key concurrency caps, leases, and cancellation signalling.
Designed against a real consumer: Sim's `JobQueueBackend` maps 1:1 onto it.

| Endpoint | Purpose |
|----------|---------|
| `POST /queue/jobs` | Enqueue: `type`, `payload`, `jobId` (idempotency key), `maxAttempts`, `delaySeconds`, `priority`, `tags`, `metadata`, `concurrencyKey` + `concurrencyLimit`, `retryDelaySeconds` |
| `POST /queue/jobs/batch` | Enqueue many; dispatch preserves input order |
| `POST /queue/claim` | Worker claims due jobs: `workerId`, `types`, `limit`, `leaseSeconds`, `waitSeconds` (short long-poll) |
| `POST /queue/jobs/{id}/heartbeat` | Extend the lease; response carries `cancelRequested` |
| `POST /queue/jobs/{id}/complete` | Report success with `output` |
| `POST /queue/jobs/{id}/fail` | Failed attempt: retries with backoff until `maxAttempts`, then dead-letters |
| `POST /queue/jobs/{id}/cancel` | Pending: cancels now. Running: graceful via heartbeat flag, or `force: true` |
| `POST /queue/jobs/{id}/retry` | Requeue a dead-lettered/cancelled job with a fresh attempt budget |
| `GET /queue/jobs/{id}`, `GET /queue/jobs`, `GET /queue/stats` | Inspect |

Lifecycle: `pending -> running -> completed | failed (deadLetter) | cancelled`,
with expired leases automatically reclaimed (or dead-lettered when attempts are
exhausted). On Postgres, claims use `FOR UPDATE SKIP LOCKED`, so N concurrent
workers never double-claim; every transition verifies the reporting worker still
holds the job, so a zombie worker whose lease was reclaimed gets a 409 instead
of clobbering another worker's run.
