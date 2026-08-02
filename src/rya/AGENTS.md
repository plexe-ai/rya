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
  **Agent-agnostic since D21**: `build_app` reads no manifest and imports no
  handler. Agents come from published versions and environment pointers
  (`agents.py`), routes carry `{agent_id}` for real (D28; `_` is the sole-agent
  alias), and the route dependency is a `Plane` - a store with no manifest and no
  agent - so "the api runs no tenant code" is a property of the type.
- `observability/` - structured logs, token/cost usage, run export (Langfuse/OTLP).
- `mcp/` - MCP server so coding agents drive Rya; `ops` = plain testable functions.
- `cli/` - the `rya` CLI (Typer), project scaffolding, deploy templates. Two
  entry points: `main.py` is the operator CLI (`rya-server`), `client.py` is the
  client subset shipped in the thin `rya` SDK (D16 / §14).
- `execution/` - the scheduling half of the execution plane: the `ExecutionDriver`
  substrate seam (D26) with the `local`, `docker` and `kubernetes` drivers, the
  supervisor that decides what to start (D25), the hash-keyed warm interpreter
  pool that runs each item in a fork (D27), and the credential-free template host
  that lets that fork happen in a different container from the claimer (D32).
  `worker.py` is what gets scheduled and deliberately lives outside it. Also holds
  the **launch gate** (`require_untrusted_posture`), which refuses to start unless
  D18, D23, D24 and D32 are all in force.
- `broker/` - the credential boundary (D18). Tenant code holds a socket and a
  short-lived capability; the broker holds the DSN, the seal keys, the pooled
  provider key and the only route out. `protocol.py` carries the method allowlist and
  imports neither of the other two, so the security-relevant part reads on its own.
- `console/` - the built-in web console (single-file SPA served by `rya serve`).
- `skills/` - bundled coding-agent skills (authoring + operating).

## Single-file modules (in this directory)

- `store.py` - `FileStore` (JSON under `.rya/`) + `open_store()` seam. The state substrate for local dev.
- `store_postgres.py` - `PostgresStore` (JSONB + RLS). Same method surface as FileStore; used for self-host + cloud.
- `tenancy.py` - workspaces, users, API keys, members/invites, per-user RLS install. Multi-tenant control.
- `queue.py` - durable external-worker job queue (enqueue/claim/lease/heartbeat/complete, retries, DLQ, concurrency caps).
- `turns.py` - durable chat turns over the queue (leased, reclaimable, resumable stream buffer + post-approval continuation).
- `guard.py` - Action Guard egress **policy** + grounding gate. Not enforcement
  since D24: an in-process check is bypassed by code that does not call it, so
  `egress.py` carries the network verdict and this module carries the reviewable,
  attributable, auditable one.
- `egress.py` - what can physically leave a sandbox (D24), and the reconciler for the
  two verdicts. A divergence between the policy and the network is the ordinary
  consequence of a policy change, so it is recorded and alertable rather than a bug.
- `bundles.py` - the client bundle: build, content-hash (D12), pack/unpack/verify, local or S3-compatible archive store (`RYA_BUNDLES_S3_ENDPOINT` for MinIO/Ceph/R2, which also forces path-style addressing).
- `deployments.py` - immutable versions + environment pointers: promote, rollback, retire, retention (D11/D12/§9).
- `agents.py` - which agents this deployment serves and what each declares (D21). A version or an environment pointer is what makes an agent exist; no file, no import.
- `worker.py` - the execution-plane process: loads a pinned bundle, advertises handlers, registers, heartbeats, scales to zero (§6). Since D27 it *claims* and delegates *executing* to an executor - `InlineExecutor` (the import lives in this process) or `ForkExecutor` (a fork of a warm interpreter, so this process holds no tenant code). Since Phase 5 the `ForkExecutor` also serves a **whole tenant** (`--scope tenant`): no bundle, agent or version resolved at startup, one per item instead, and a peek→warm→fork order that keeps preflight before the claim.
- `gates.py` - promotion gates: readiness/eval evidence as a server-side admission check, attested against a version (§9).
- `quotas.py` - per-workspace limits (runs, tokens, cost, workers). Admission-only
  for runs and jobs; since D30 also on the **inference call path** (`kind="model"`),
  because with a pooled provider key an overrun spends the platform's money.
  `require_admission` is also where D31 refuses a disabled workspace, and where D29's
  org budget is appended - as a *read of this workspace's own derived row*, never a
  cross-tenant query.
- `orgs.py` - the billing boundary above a workspace (D29/D35): budget vocabulary, the
  cross-workspace rollup (admin DSN, outside the tenant plane), and the derived
  per-workspace verdict the admission path reads. Nothing here moves an isolation
  boundary; no RLS policy references `org_id`. The rollup is scheduled by the
  supervisor's multi-workspace fan-out (`supervisor.reconcile_orgs`), *not* by
  `Supervisor.tick` - a `Supervisor` is scoped to one workspace and an org spans them,
  so putting it there would hand the tenant-scoped object the cross-workspace read D29
  keeps out of it. `orgs.freshness` is for deployments that run neither a supervisor
  nor a cron: a verdict nothing refreshes says so instead of looking current.
- `config.py` - declared run config: model routes, secrets, per-environment values (D8 — nothing ambient).
- `seal.py` - encryption-at-rest for connection secrets. Delegates to `keys.py` when
  a per-tenant provider is declared; unchanged by default, because an upgrade must not
  re-address ciphertext already written.
- `keys.py` - the key-provider seam (D18/#13): `deployment` | `derived` | `wrapped` |
  `wrapped`+KMS, rotation, re-seal, and `destroy` - the crypto-shred D31 needs. Only
  `wrapped` can shred; `derived` refuses rather than pretending.
- `purge.py` - two-phase tenant deletion (D31). `disable` is immediate and reversible;
  `purge` shreds the key, deletes objects and rows, and leaves an anonymised audit
  stub with an *attestation* distinguishing "unreadable by construction" from "rows
  deleted".
- `accounts.py` - password hashing + session tokens for self-serve onboarding.
- `auth.py` - JWT identity (HS256 / JWKS) for per-user scoping.
- `evals.py` - declarative behavioural evals (`rya.evals.yaml`) + optional DeepEval metric.
- `readiness.py` - the `rya deploy --check` production-readiness gate.
- `snapshot.py` - `rya context` + the console aggregate (`GET /console`).
- `provision.py` - `rya provision`: stand up + report the base infra inventory.
- `cloud.py` - client for driving a hosted Rya (`rya login` / `rya cloud`) and for uploading a bundle to it (`rya publish` -> `POST /agents/{id}/versions`). Preserves the server's `E_*` code across the network rather than flattening it to `E_REMOTE`.
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
