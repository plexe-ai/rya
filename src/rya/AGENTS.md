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
- `cli/` - the `rya` CLI (Typer), project scaffolding, deploy templates. Two
  entry points: `main.py` is the operator CLI (`rya-server`), `client.py` is the
  client subset shipped in the thin `rya` SDK (D16 / §14).
- `console/` - the built-in web console (single-file SPA served by `rya serve`).
- `skills/` - bundled coding-agent skills (authoring + operating).

## Single-file modules (in this directory)

- `store.py` - `FileStore` (JSON under `.rya/`) + `open_store()` seam. The state substrate for local dev.
- `store_postgres.py` - `PostgresStore` (JSONB + RLS). Same method surface as FileStore; used for self-host + cloud.
- `tenancy.py` - workspaces, users, API keys, members/invites, per-user RLS install. Multi-tenant control.
- `queue.py` - durable external-worker job queue (enqueue/claim/lease/heartbeat/complete, retries, DLQ, concurrency caps).
- `turns.py` - durable chat turns over the queue (leased, reclaimable, resumable stream buffer + post-approval continuation).
- `guard.py` - Action Guard egress firewall + grounding gate.
- `bundles.py` - the client bundle: build, content-hash (D12), pack/unpack/verify, local or S3 archive store.
- `deployments.py` - immutable versions + environment pointers: promote, rollback, retire, retention (D11/D12/§9).
- `worker.py` - the execution-plane process: loads a pinned bundle, advertises handlers, registers, heartbeats, scales to zero (§6).
- `gates.py` - promotion gates: readiness/eval evidence as a server-side admission check, attested against a version (§9).
- `quotas.py` - per-workspace limits (runs, tokens, cost, workers), enforced at admission only (§11.12/D13).
- `config.py` - declared run config: model routes, secrets, per-environment values (D8 — nothing ambient).
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
- This package ships as two mutually exclusive distributions (D16): the thin
  client SDK (`rya`) and the platform (`rya-server`). The SDK's module set is
  declared in `packaging/surface.py` and enforced by `tests/test_sdk_surface.py`
  - an SDK module may not import platform code, transitively. Adding a
  module-scope import to one of those modules will fail that test; see
  docs/PACKAGING.md.
- Errors surfaced to callers carry a stable `E_*` code from `errors.py`.
