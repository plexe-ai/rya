# `rya` package

The Rya runtime: a production backend for AI agents. An agent is a **manifest**
(`rya.agent.yaml`) plus **handlers** (Python functions) that receive a `ctx`
object. Every side effect flows through `ctx.*` and is journaled, which is what
makes runs durable (pause for approval, resume by replay), traced, and safe.

## How a run flows

`api` / `cli` / `mcp` receive a trigger -> `runtime.Engine` creates a run and
executes the handler with a `sdk.RuntimeContext` -> each `ctx.*` step is
journaled to the `store` -> on `ctx.approvals.request` the coroutine unwinds and
the run persists (`approvals`) -> a later approve replays the handler, memoizing
completed steps. Real model/channel calls go through `providers`; tool
permissions, pins, and the Action Guard are enforced in `sdk.context`.

## Submodules (each has its own AGENTS.md)

- `sdk/` - `define_agent()` and the `ctx` runtime surface. The heart of Rya.
- `runtime/` - the engine: load agent, execute, pause/resume, retries, cron.
- `providers/` - real seams: LLM (Anthropic/OpenAI + mock), channels, embeddings.
- `manifest/` - `rya.agent.yaml` Pydantic schema + loader/validator.
- `tools/` - permissioned tool registry + real built-ins (`web.fetch`, `http.request`).
- `models/` - custom-model registry.
- `approvals/` - the pause/resume signal (PausedForApproval).
- `api/` - FastAPI control plane: REST, webhooks, WebSocket, SSE, console, MCP mount.
- `observability/` - structured logs, token/cost usage, run export (Langfuse/OTLP).
- `mcp/` - MCP server so coding agents drive Rya; `ops` = plain testable functions.
- `cli/` - the `rya` CLI (Typer), project scaffolding, deploy templates.
- `console/` - the built-in web console (single-file SPA served by `rya serve`).
- `skills/` - bundled coding-agent skills (authoring + operating).

## Single-file modules (in this directory)

- `store.py` - `FileStore` (JSON under `.rya/`) + `open_store()` seam. The state substrate for local dev.
- `store_postgres.py` - `PostgresStore` (JSONB + RLS). Same method surface as FileStore; used for self-host + cloud.
- `tenancy.py` - workspaces, users, API keys, members/invites, per-user RLS install. Multi-tenant control.
- `queue.py` - durable external-worker job queue (enqueue/claim/lease/heartbeat/complete, retries, DLQ, concurrency caps).
- `turns.py` - durable chat turns over the queue (leased, reclaimable, resumable stream buffer + post-approval continuation).
- `guard.py` - Action Guard egress firewall + grounding gate.
- `seal.py` - encryption-at-rest for connection secrets (Fernet via `RYA_SECRET_KEY`).
- `accounts.py` - password hashing + session tokens for self-serve onboarding.
- `auth.py` - JWT identity (HS256 / JWKS) for per-user scoping.
- `evals.py` - declarative behavioural evals (`rya.evals.yaml`) + optional DeepEval metric.
- `readiness.py` - the `rya deploy --check` production-readiness gate.
- `snapshot.py` - `rya context` + the console aggregate (`GET /console`).
- `provision.py` - `rya provision`: stand up + report the base infra inventory.
- `cloud.py` - client for driving a hosted Rya (`rya login` / `rya cloud`).
- `errors.py` - stable `E_*` error codes + semantic exit codes (branch on these, don't scrape prose).

## Rules for changing this package

- Durability depends on the journal: any new side-effecting `ctx` operation must
  route through `RuntimeContext._step`/`_astep` so it is memoized on replay.
- Handlers must issue `ctx` calls in a deterministic order (standard replay rule).
- `FileStore` and `PostgresStore` must keep an identical method surface - the
  engine/CLI/API are store-agnostic.
- Errors surfaced to callers carry a stable `E_*` code from `errors.py`.
