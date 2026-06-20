# Rya

**Production backend/runtime for AI agents.** Rya gives developers and coding
agents the runtime, memory, tools, approvals, channels, jobs, model access, and
observability needed to build production-grade AI agents — without rebuilding
the infrastructure every time.

> From prompt to production-grade agent backend in minutes.

Rya is **coding-agent-first**: Claude Code, Codex, and Cursor drive it through a
CLI, an MCP server, and skills — and the platform encodes *what production
requires* as a green checklist (`rya deploy --check`) the agent satisfies, so it
ships something safe without being a production expert.

One command — `rya provision` — stands up the **full base infrastructure** a
production agent needs and reports it as an inventory: durable database, memory,
conversation sessions, authentication, guardrails, the real-time **WebSocket**
channel, background jobs with retry + dead-letter, horizontal scale, and
observability.

What's real today: durable runs on **Postgres** (survive restarts), **per-user
row-level-security** multi-tenancy, real **Anthropic/OpenAI** + channel seams, a
signed-webhook HTTP server **and a real-time WebSocket** with token/JWT auth, a
built-in **web console**, an **Action Guard** egress firewall, conversation
**sessions**, **remote MCP** over HTTP, self-serve project provisioning, a
first-class **LLM layer** (real models, structured output, a governed agent
loop), real general-purpose **built-in tools** (`web.fetch`, `http.request`), and
a **production-readiness gate**. What's still mocked: the default *domain* tool +
model stubs (`crm.lookup`, `churn-risk-v1` — deterministic for reproducibility;
the real seams and real built-ins are live). What's not built yet: a *managed*
hosting platform — the hosted
instance *is* `rya serve` (deploy it via `docker compose` / `rya deploy`
artifacts); there's no one-click cloud yet.

**For the full picture, read [docs/DEEP_DIVE.md](docs/DEEP_DIVE.md).**

## Install

```bash
uvx rya create support-agent      # zero-install: scaffold + run in one command
pipx install rya                  # or install the CLI globally
pip install 'rya[api,mcp,postgres,llm]'   # full: control-plane API, MCP, Postgres, real Claude
```

From a checkout, for development:

```bash
cd rya
pip install -e '.[api,mcp,postgres,llm,dev]'
```

Maintainers — build + publish:

```bash
uv build                      # → dist/rya-*.whl + .tar.gz
uvx --from dist/rya-*.whl rya --version   # smoke-test the artifact in isolation
uv publish                    # needs a PyPI token; this is what makes `uvx rya` resolve
```

## Self-host (production-grade, OSS)

The same runtime runs on a real Postgres — durable runs that survive restarts:

```bash
cp .env.example .env       # optionally add ANTHROPIC_API_KEY for real Claude
docker compose up          # Postgres + `rya serve` on :8787
```

Locally, set `RYA_DATABASE_URL=postgres://…` and every command uses Postgres
instead of files — no code change. See [docs/architecture.md](docs/architecture.md)
for the OSS-core / managed-cloud split.

### Trigger a live agent over HTTP

`rya serve` exposes a webhook so external systems drive real runs. Set
`RYA_TOKEN` to require an operator token on the control API, and
`RYA_WEBHOOK_SECRET` to require signed inbound webhooks:

```bash
export RYA_TOKEN=$(rya token --json | jq -r .token)
export RYA_WEBHOOK_SECRET=whsec123
rya serve --port 8787      # POST /inbound (signed) → real run; control routes need the token

# external system fires a signed webhook → agent runs, pauses for approval
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$RYA_WEBHOOK_SECRET" | sed 's/^.*= //')"
curl -XPOST localhost:8787/inbound -H "X-Rya-Signature: $SIG" -d "$BODY"

# operator approves with the token → run resumes and completes
curl -XPOST localhost:8787/approvals/<id>/approve -H "Authorization: Bearer $RYA_TOKEN"
```

## 60-second tour

```bash
rya create support-agent
cd support-agent
rya dev                                                    # validate + inspect
rya events send --type message.received \
  --payload '{"email":"ada@example.com"}'                  # run pauses for approval
rya approvals list                                         # see the gate
rya approvals approve <approval_id>                        # resume; email "sent"
rya runs trace <run_id>                                    # full run trace
rya jobs run --all                                         # run the scheduled follow-up
```

Add `--json` to **any** command for machine-readable output, and
`--non-interactive` to forbid hidden prompts. Errors carry a stable `E_*` code,
a suggested next action, and a semantic exit code.

## What the vertical slice proves

The example follow-up agent ([examples/followup_agent](examples/followup_agent))
exercises every core primitive in one run:

| Step | Primitive |
|------|-----------|
| Receive a webhook-style event | **Events** |
| Store the event | **Memory** |
| Look up the customer | **Tools** (permissioned) |
| Score churn risk | **Models** (gateway) |
| Schedule a delayed follow-up | **Jobs / Cron** |
| Draft a message | **LLM** |
| Gate the send behind a human | **Approvals** (pause/resume) |
| Send after approval | **Channels** |
| Capture everything | **Observability** (trace) |

## How pause/resume works

A run is durable. When the handler calls `ctx.approvals.request(...)`, the run
**pauses** (the coroutine unwinds) and its journal is persisted to `.rya/`. A
*separate* `rya approvals approve` invocation resolves the approval, executes
the gated action, and **resumes** the run by replaying the handler against the
journal — prior tool/model/memory steps are memoized, so only the code after
the approval runs for real. See [src/rya/sdk/context.py](src/rya/sdk/context.py).

## Layout

```
src/rya/
  manifest/        rya.agent.yaml schema + loader/validator
  sdk/             define_agent(), the ctx runtime context
  runtime/         engine: load, execute, pause/resume, retries, timeouts, cron
  tools/ models/   permissioned tool + model registries (mock IO)
  providers/       real seams: llm (anthropic/openai), channels, embeddings
  approvals/       approval lifecycle + pause signal
  store.py         FileStore; store_postgres.py PostgresStore (RLS); open_store()
  tenancy.py       workspaces, API keys, per-user RLS provisioning
  guard.py         Action Guard — egress firewall
  provision.py     rya provision — stand up the full base infra inventory
  readiness.py     production-readiness checklist (rya deploy --check)
  snapshot.py      rya context + the console aggregate
  observability/   structured logs, usage/cost, Langfuse/webhook export
  auth.py          JWT identity (HS256 / JWKS)
  api/             FastAPI control plane + webhook + console + guard ([api])
  console/         the built-in web console (served by rya serve)
  mcp/             MCP server (25 rya_* tools) — FastMCP ([mcp] extra)
  skills/          bundled skills (rya = authoring, rya-ops = operating)
  cli/             rya CLI (Typer), scaffolding, deploy templates
clients/typescript/ @plexe/rya — typed TS client
deploy/aws/        single-tenant CloudFormation/SAM (cfn-lint clean)
examples/ docs/ tests/
```

## Web console

`rya serve` ships a built-in web console — an agent **backend infrastructure**
dashboard (the primitives a coding agent provisions and operates):

```bash
rya serve --port 8787        # → console at http://localhost:8787/
```

It renders live state from the runtime (`GET /console`): the **Overview**
primitive grid, an **Infrastructure** view (compute, data substrate, auth/RLS,
observability — computed from the running process), the tool registry with
permissions, the model gateway, runs with **forensic traces**, a working
**approvals queue** (approving resumes the real paused run), and the
**Action Guard** editor. The page is public; its data calls are auth-gated by
`RYA_TOKEN` (a Connect dialog prompts when set). Source:
[src/rya/console/index.html](src/rya/console/index.html), served by
[api/app.py](src/rya/api/app.py).

## LLM layer: real models, structured output, governed agent loop

The model is first-class, not an afterthought. `ctx.llm` is a real multi-provider
gateway (Anthropic/OpenAI over stdlib HTTP — real when a key is present, a
deterministic mock otherwise) with the two things agent-building actually needs:

```python
# 1. Structured output — pass a JSON Schema, get a validated object back.
res = await ctx.llm.respond(system="classify", input={"text": msg},
                            schema={"type": "object", "required": ["sentiment", "score"], …})
res.json["sentiment"]            # parsed + validated, not string-parsing

# 2. Governed agent loop — the model reasons and CALLS TOOLS until it answers.
out = await ctx.llm.run(input={"q": "look up Ada and summarize"}, tools=["crm.lookup"])
out["toolCalls"]                 # what the model actually ran
```

The agent loop is the differentiator: **every tool the model calls goes through
`ctx.tools.call`** — so the same permissions, **scoped + encrypted credentials**,
**Action Guard egress firewall**, and audit trail apply to what the *model*
decides to do. Approval-gated tools are never exposed to the loop, so a model can
autonomously use safe/read tools but a side-effectful action still requires an
explicit `ctx.approvals.request` (human gate). An LLM that can act, sandboxed by
the same governance as the rest of the runtime. See
[src/rya/providers/llm.py](src/rya/providers/llm.py).

Two **real, general-purpose built-in tools** ship so the loop does actual work
out of the box: `web.fetch` (GET a URL → readable text) and `http.request`
(any method) — both routed through the Action Guard before a byte leaves the
process. Declare them in the manifest like any tool; everything else an agent
integrates with is HTTP on top of these. See
[src/rya/tools/builtins.py](src/rya/tools/builtins.py).

## Memory (core blocks + consolidated long-term recall)

`ctx.memory` is more than a vector store — it's the two-tier memory production
agents need:

- **Core memory blocks** (Letta-style): small, named, *always-in-context*,
  agent-editable slots — persona, the user's profile, current task state.
  `ctx.memory.block_set("persona", …)` / `block_append` / `blocks()`.
- **Long-term facts** (Mem0-style): `ctx.memory.remember(text)` extracts atomic
  facts, embeds them, and **consolidates** — a near-duplicate updates in place
  instead of piling up, which is what keeps recall token-efficient.
  `ctx.memory.recall(query)` does semantic retrieval (vector + lexical fallback).
- **Budget-bounded assembly** (virtual-memory paging): `ctx.memory.assemble(query,
  token_budget=1000)` returns the core blocks (always) plus the most relevant
  facts paged in until the budget is spent — the working context you feed the model.

All of it is scoped per user by RLS and visible in the console's **Memory** view
(core blocks + long-term collections with semantic recall). See
[src/rya/sdk/context.py](src/rya/sdk/context.py).

## Provision the full base infra

A coding agent shouldn't hand-assemble a database, auth, guardrails, a realtime
channel, and a job queue every time. `rya provision` stands the whole base
infrastructure up and reports it as one inventory — each component with a status
and an exact fix:

```bash
rya provision --json            # auto target (Postgres if RYA_DATABASE_URL, else local)
rya provision --target postgres # durable + multi-worker production substrate
rya provision --dry-run         # inspect without writing anything
```

It covers every primitive a production agent needs — **compute/runtime, durable
database, memory, conversation sessions, authentication, guardrails (egress
firewall), the real-time WebSocket channel, background jobs with retry +
dead-letter, horizontal scale, observability, secrets** — and is idempotent
(re-running converges). It provisions what it safely can (writes a default guard
policy, runs the Postgres tenancy/RLS setup, mints a workspace API key, generates
an operator token) and reports what still needs a human (missing secrets,
readiness blocks). It never deploys to a cloud or moves money — that stays an
explicit, separate step. Available to coding agents as the `rya_provision` MCP
tool. See [src/rya/provision.py](src/rya/provision.py).

## Real-time channel (WebSocket)

Beyond signed HTTP webhooks, `rya serve` exposes a **bidirectional WebSocket** at
`/ws` — drive the agent and watch a run execute live, step by step:

```js
const ws = new WebSocket("ws://localhost:8787/ws?token=$RYA_TOKEN");
ws.onmessage = (e) => console.log(JSON.parse(e.data));   // ready → trace… → run
ws.send(JSON.stringify({ type: "message", channel: "web",
                         externalId: "user-7", content: "my export is broken" }));
```

Frames are JSON: `event`/`message` trigger a real run and stream every trace
event the instant it happens, ending with a terminal `run` summary; `replay`
re-streams a stored run; `ping`→`pong`. The conversational `message` form threads
into a durable **session** and streams the agent's reply. Auth mirrors the HTTP
API (`?token=` carries the operator token or `rya_sk_…` workspace key). Needs the
`[api]` extra. See [src/rya/api/app.py](src/rya/api/app.py).

## Evals (behavioural checks, gate-able)

The readiness gate proves an agent *could* ship; **evals** prove it *behaves*.
Cases live in `rya.evals.yaml` — each fires a real event and scores the run:

```bash
rya eval --json     # runs every case; exits non-zero if any fails
```

```yaml
evals:
  - id: high_risk_pauses_for_approval
    trigger: { type: message.received, payload: { email: "risk@acme.io" } }
    expect:
      status: waiting_approval
      tools_called: [crm.lookup]
      approval_requested: true
```

Scorers are deterministic (`status`, `tools_called`, `approval_requested`,
`no_failure`, `result_contains`, `max_tokens`, `max_cost`, `trace_has/lacks`)
plus an optional LLM `judge` (skipped on the mock provider so the suite stays
runnable offline). `rya eval` exits non-zero on any failure, so you gate a deploy
on **behaviour** like `rya deploy --check` gates on readiness — and the console's
**Evals** view runs the suite with one click. Also the `rya_eval` MCP tool. See
[src/rya/evals.py](src/rya/evals.py).

## Production-readiness gate

A coding agent can write a handler; it can't *know production*. So Rya encodes
the production bar as a machine-checkable checklist — make it green to ship:

```bash
rya deploy --check --json    # blocks (must fix) + warnings, each with an exact fix
```

Blockers (deploy-stopping, e.g. `E_UNGATED_SIDE_EFFECT`, `E_SECRET_UNSET`,
`E_TOOL_NO_IMPL`, `E_NO_EVENT_HANDLER`) carry a stable code + the fix; warnings
(mock LLM, file store, no trace export…) are advisory. A plain `rya deploy`
runs the check as a **hard gate** (`E_NOT_PRODUCTION_READY` unless `--force`),
so an unsafe agent is un-shippable. The verdict also rides along in
`rya context`, so the agent's one orient call already knows what to fix.

## Action Guard (egress firewall)

Every outbound request the runtime makes — HTTP tools, model calls, channel
sends — is checked **before the bytes leave the process**:

```
SSRF blocklist  →  deny rules  →  allow rules  →  default (deny | allow)
```

The policy lives in `rya.guard.yaml` (hot-reloaded on save; a no-op if absent).
A blocked request raises `E_EGRESS_BLOCKED` and never goes out — real
network-level blocking, editable live in the console's **Action Guard** page,
with a built-in policy test suite. See [src/rya/guard.py](src/rya/guard.py).

## Connected credentials (scoped, vaulted, delegated)

The industry default is still an unscoped API key the agent can read — over-broad
and leakable. Rya makes a tool's credential a **scoped connection** instead:

```bash
rya connect github --scopes "issues:write,repo:read" --token "$GH_TOKEN"
rya connections list          # provider, scopes, owner, status — never the secret
```

A tool binds to a provider in the manifest (`provider: github`, `scopes:
[issues:write]`). At call time the runtime enforces the **intersection rule** —
a tool may run only if its required scopes are within *(connection scopes ∩ the
requesting user's scopes)* — then **injects the secret** into the call. The secret
is protected in three places: **encrypted at rest** (Fernet; key from
`RYA_SECRET_KEY` / KMS in production, or a per-project `0600` keyfile for dev),
**redacted** from every trace/log, and **never returned** by any read — the
handler and model never see it. A missing connection raises `E_NO_CONNECTION`; an
out-of-scope call raises `E_SCOPE_DENIED`, before any bytes leave the process.
Connections are per-user/workspace under the same RLS as runs. CLI + the
`rya_connect` MCP tool + a console **Connections** view. See
[src/rya/seal.py](src/rya/seal.py) and [src/rya/sdk/context.py](src/rya/sdk/context.py).

## Coding-agent surfaces

Four ways a coding agent drives Rya, all over the same operations:

```bash
rya <cmd> --json        # 1. CLI — every command emits machine-readable JSON
rya mcp                 # 2a. MCP server (stdio) — 25 rya_* tools  [pip install 'rya[mcp]']
rya mcp --http          # 2b. REMOTE MCP over HTTP — agents connect by URL, no local install
rya skills install      # 3. Skills — teach the workflow so agents don't guess
rya context --json      #    one-call orient: state + readiness + the rules to respect
rya provision --json    #    stand up the full base infra inventory (rya_provision tool)
```

See [docs/mcp.md](docs/mcp.md) for the tool list and how to register the MCP
server with Claude Code.

## Remote MCP & hosted instances

`rya serve` is a **single hosted origin** for everything — the control plane, the
console, the real-time WebSocket, **and remote MCP at `/mcp`**. An agent in any
editor connects to the URL with no local Rya install:

```jsonc
// .mcp.json (Claude Code / Cursor)
{ "mcpServers": { "rya": {
  "type": "http", "url": "https://your-rya-host/mcp",
  "headers": { "Authorization": "Bearer ${RYA_TOKEN}" } } } }
```

Point the CLI and your agent at a hosted instance in one command:

```bash
rya login https://rya.yourco.com --key rya_sk_…   # verifies, stores creds, prints .mcp.json
rya whoami                                          # cloud → https://rya.yourco.com
rya cloud send --type message.received --payload '{"email":"a@b.co"}'   # drive the hosted agent
rya cloud approvals && rya cloud approve <id>       # resume a hosted run
rya logout                                          # back to the local runtime
```

`GET /v1/info` advertises the endpoints (remote MCP, API, console, WebSocket).
In **multi-tenant** mode (`RYA_MULTITENANT=1` + Postgres), `POST /v1/projects`
self-provisions a project — a workspace + a one-time `rya_sk_…` API key — the
hosted "create a project" flow (gate signup with `RYA_ADMIN_TOKEN`). Remote MCP
is auth-gated: when `RYA_TOKEN` is set, `/mcp` requires it. Nothing phones home —
the cloud is strictly opt-in (`rya login` / `RYA_REMOTE_URL`). Deploy the same
runtime to your own host via `docker compose` / `rya deploy` artifacts — there is
no separate cloud build; the hosted instance *is* `rya serve`.

See [docs/primitives.md](docs/primitives.md), [docs/devex.md](docs/devex.md), and
[docs/mcp.md](docs/mcp.md).
