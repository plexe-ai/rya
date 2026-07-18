# Rya — Knowledge Doc

A single document to get a person (or a coding agent) fully up to speed on Rya:
what it is, how it thinks, every surface it exposes, and — honestly — what works
today and what doesn't.

> Companion docs: [`primitives.md`](primitives.md) (primitive reference),
> [`architecture.md`](architecture.md) (OSS core vs. managed cloud),
> [`devex.md`](devex.md) (coding-agent DevEx), [`mcp.md`](mcp.md) (MCP + skill),
> [`DEEP_DIVE.md`](DEEP_DIVE.md), [`VISION_GAP.md`](VISION_GAP.md).

---

## 1. What Rya is

**Rya is a production backend/runtime for AI agents** — a Supabase-style backend
where the focus is the AI agent and LLM integration rather than CRUD.

You write two things: a **manifest** (`rya.agent.yaml`) and a **handler**
(`agent.py`). Rya provides everything around them — the durable runtime, memory,
RAG, LLM integration, human approvals, scoped credentials, governance, jobs,
sessions, observability, evals, multi-tenancy, and the deploy.

Two principles shape it:

1. **Coding-agent-first.** The CLI, the MCP server, and `rya context` (a one-shot
   machine-readable snapshot) are primary surfaces, not afterthoughts. A coding
   agent should be able to stand up a production agent backend without a GUI.
2. **Own the runtime; integrate the rest.** Rya is the *runtime + governance*
   layer. For deep, well-solved domains it **emits to / delegates to**
   best-in-class tools instead of reimplementing them — Postgres for data,
   Langfuse/OpenTelemetry for observability, DeepEval for output-quality metrics.
   (Supabase ships Postgres; it doesn't rewrite a database.)

---

## 2. The core idea: a run is a durable, journaled execution

This is the concept everything else hangs off. Understand this and Rya makes sense.

- Every `ctx.*` call is a **journaled step**: `{seq, kind, label, status, result}`.
- The run also accumulates a **trace** (`{seq, ts, kind, label, data}`) — the
  human/observability view, with secrets redacted.
- If a run **pauses** (waiting on a human approval) or crashes, resuming
  **replays the journal**: steps already marked `done` return their cached result
  instead of re-executing.

That replay rule is what makes "pause for a human, resume hours later" safe — the
CRM isn't queried twice and the email isn't sent twice. It's the same trick a
durable-workflow engine uses, scoped to agents.

**The determinism rule:** a handler may only do side-effectful or non-deterministic
work *through `ctx`* (the journaled leaves). Raw `requests.post(...)` or
`random()` in a handler breaks replay. Tool handlers must be leaves — no nested
journaled `ctx` calls inside them.

Run statuses: `running` → `completed` | `failed` | `rejected` | `waiting_approval`.

---

## 3. Quickstart

```bash
pip install -e '.[api,llm,mcp]'        # add ,postgres for the Postgres substrate
rya create myagent && cd myagent
rya dev            # load + validate the manifest and agent code; report what's wired
rya serve          # control plane + console on :8787 (also mounts remote MCP at /mcp)
```

Anatomy of an agent:

```yaml
# rya.agent.yaml
name: followup
runtime: python
entrypoint: agent.py
tools:
  - id: crm.lookup
    permission: allowed
  - id: email.send
    permission: approval_required     # read_only | allowed | approval_required | disabled
memory:
  collections: [notes]
```

```python
# agent.py
from rya import define_agent
agent = define_agent()

@agent.on_event
async def handle(ctx, event):
    profile = await ctx.tools.call("crm.lookup", {"email": "ada@acme.io"})
    draft   = await ctx.llm.respond(system="draft a reply", input=profile)
    await ctx.approvals.request(
        title="Send this reply?", body=draft.text,
        action={"tool": "email.send", "input": {"to": "ada@acme.io", "body": draft.text}},
    )
    return "queued"
```

The run pauses at `approvals.request`. Approving it (console, CLI, or
`POST /approvals/{id}/approve`) resumes the run and executes the action through
the **real** provider seam.

---

## 4. The primitives (`ctx.*`)

Fifteen primitives, all journaled:

| Primitive | What it does |
|---|---|
| `ctx.llm` | `respond(system, input, schema?)` → structured output; `run(input, system, tools)` → **governed agent loop** (tool calls flow through permissions + the guard) |
| `ctx.models` | Direct model calls, model fallback on provider error |
| `ctx.memory` | Durable agent memory: `append`, `search` (vector), `block_set`/`remember`/`recall`/`assemble` |
| `ctx.knowledge` | RAG: `add` (chunk → embed), `search` (vector + lexical blend), `documents` |
| `ctx.tools` | `call(id, input)` — permission- and guard-checked |
| `ctx.channels` | Outbound delivery (mock / slack / webhook / resend) |
| `ctx.jobs` | Schedule background work (`job.schedule`; executed by `rya worker`) |
| `ctx.cron` | Recurring schedules |
| `ctx.approvals` | `request(title, body, action)` → pause the run for a human |
| `ctx.sessions` | Conversation/session handling (`get_or_create`, `append`) |
| `ctx.connections` | Scoped, encrypted third-party credentials |
| `ctx.logs` | Structured logs (into the trace) |
| `ctx.traces` | Trace access |
| `ctx.secrets` | Secret access (auto-redacted from traces) |
| `ctx.events` | Emit/consume events |

---

## 5. Governance (the differentiator)

Rya's evals and guard know about the runtime, which is why they can assert things
a generic eval tool can't.

- **Tool permissions** — declared per tool in the manifest: `read_only`,
  `allowed`, `approval_required`, `disabled`. Enforced at `ctx.tools.call` *and*
  inside the LLM agent loop, so a model can't talk its way into a gated tool.
- **Action Guard** — a policy layer over actions (`rya`'s `/guard`, `PUT /guard`,
  `POST /guard/test`). Governs real side effects (HTTP egress, channels).
- **Scoped connections** — third-party credentials with scopes, **encrypted at
  rest** (Fernet; key from `RYA_SECRET_KEY` or a per-project keyfile; ciphertext
  carries an `enc:v1:` prefix). Managed via `rya connect`, `rya connections
  list|reseal|revoke`.
- **Secret redaction** — secrets are redacted out of traces automatically.

---

## 6. LLM layer

- **Providers**: Anthropic, OpenAI, and a `mock` provider. Resolution is
  automatic (`auto`) based on which key is present; **with no key it resolves to
  `mock`**, so everything stays runnable offline.
- **Structured output**: `ctx.llm.respond(..., schema=...)`.
- **Governed agent loop**: `ctx.llm.run(input, system, tools=[...])` — the model
  picks tools; every call is permission- + guard-checked and journaled.
- **RAG**: `ctx.knowledge` — chunk → embed → retrieve, ranked by a vector +
  token-overlap lexical blend.
- **Usage + cost**: computed *from the trace* (correct across approval replays).
  Cost only reported when priced via `RYA_PRICE_<MODEL>_IN` / `_OUT` — Rya never
  hard-codes provider prices that might be wrong.

---

## 7. Storage substrate

One API, two backends, chosen by env:

- **FileStore** — `.rya/` JSON. Default; zero setup.
- **PostgresStore** — JSONB + row-level security. Used when `RYA_DATABASE_URL` is set.

`open_store()` picks the backend; agent code never knows the difference.

---

## 8. Multi-tenancy

Enabled with `RYA_MULTITENANT=1` + Postgres.

- **Workspaces** + **API keys** (`rya_sk_…`, stored only as a SHA-256 hash;
  plaintext shown once).
- **Postgres RLS** does the real isolation: `FORCE` policies, a non-superuser
  `rya_app` role, and `app.workspace_id` / `app.user_id` GUCs — the *database*
  refuses cross-tenant reads, not just the app layer.
- **Self-serve onboarding** (no admin token): `POST /v1/signup` → user + first
  workspace + API key + session token; `POST /v1/login`, `GET /v1/me`,
  `POST /v1/workspaces`. Passwords are PBKDF2-hashed; sessions are HMAC-signed
  (stdlib only, no new deps).

---

## 9. Observability — emit, don't rebuild

Every run carries a trace + usage. On **terminal status** the engine calls
`export_run` (best-effort — an export failure never fails a run):

| Backend | Env | What it gets |
|---|---|---|
| Webhook | `RYA_TRACE_WEBHOOK` | The run summary, POSTed to any URL |
| **Langfuse** | `LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` | A trace with **nested observations** — model/LLM steps → `GENERATION` (with token usage), tool steps → `SPAN`, others → `EVENT` |
| **OpenTelemetry** | `RYA_OTLP_ENDPOINT` (+ `RYA_OTLP_HEADERS`, `OTEL_SERVICE_NAME`) | OTLP/HTTP JSON spans with **GenAI semantic conventions** (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`) → Arize Phoenix, Grafana Tempo, Datadog, any collector |

Both are stdlib-only (no SDK dependency). The built-in console (`/console`) is for
quick in-loop debugging; serious dashboards/analytics belong in Langfuse.

---

## 10. Evals

`rya.evals.yaml` + `rya eval`. Two layers, deliberately split:

**Native — behavioural / governance** (Rya's differentiator; these know the runtime):

`status`, `tools_called`, `tools_not_called`, `approval_requested`, `no_failure`,
`result_contains`, `max_tokens`, `max_cost`, `trace_has`, `trace_lacks`, `judge`

**Delegated — LLM output quality** via **DeepEval** (`deepeval` scorer):

```yaml
expect:
  approval_requested: true                      # native: governance
  deepeval: {metric: faithfulness, threshold: 0.8}   # delegated: output quality
```

Metrics: `faithfulness`, `answer_relevancy`, `hallucination`, `bias`, `toxicity`,
`contextual_relevancy|precision|recall`. Needs `pip install 'rya[deepeval]'` plus a
judge key — `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` (+ the `anthropic` package,
which `rya[llm]` provides; override the judge with `RYA_DEEPEVAL_MODEL`). Without a
key or the package it **skips gracefully** (counts as pass) so evals stay runnable.

`rya deploy` gates on eval readiness.

---

## 11. CLI surface

```
login logout whoami            # point the CLI at a hosted Rya or local
create init dev deploy         # scaffold / validate / ship
eval                           # run the eval suite
connect  connections           # scoped credentials (list | reseal | revoke)
provision                      # stand up the full base infrastructure
context                        # one-shot machine-readable snapshot (for coding agents)
status logs                    # runtime state, structured logs for a run
serve worker mcp token         # control plane / job worker / MCP server / operator token
agents events runs approvals   # drive the runtime
tools models channels secrets
schedules jobs skills
workspaces keys cloud
```

---

## 12. HTTP API

| Group | Routes |
|---|---|
| Health / info | `GET /healthz`, `GET /v1/info` |
| Console | `GET /`, `GET /console` |
| Agents & runs | `GET /agents/{id}`, `POST /agents/{id}/events`, `GET /agents/{id}/runs`, `GET /runs/{id}`, `GET /runs/{id}/trace` |
| Approvals | `GET /approvals`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject` |
| Onboarding | `POST /v1/signup`, `POST /v1/login`, `GET /v1/me`, `POST /v1/workspaces` |
| Admin | `POST /v1/projects` (gated by `RYA_ADMIN_TOKEN`; fails closed if unset) |
| Guard / evals | `GET|PUT /guard`, `POST /guard/test`, `GET /evals`, `POST /evals/run` |
| Read surfaces | `GET /connections`, `GET /knowledge`, `POST /knowledge/search`, `GET /sessions`, `GET /tools`, `GET /models`, `GET /channels` |
| Inbound | `POST /inbound`, `POST /slack/events` |
| Remote MCP | `/mcp` (mounted last so its catch-all doesn't shadow API routes) |

Auth: `Authorization: Bearer <RYA_TOKEN>` (single-tenant operator) or a workspace
`rya_sk_…` key (multi-tenant). `X-Rya-User-Token` enables per-user RLS.

---

## 13. MCP

- **Local**: `rya mcp` (stdio) — an MCP-native coding agent drives Rya directly.
- **Remote**: mounted at `/mcp` on `rya serve` (streamable-http).
- `rya context` gives a coding agent the whole backend state in one call.

---

## 14. Deployment

- **Local**: `rya serve` (FileStore, zero setup).
- **Self-host**: `rya serve` + `RYA_DATABASE_URL` (+ `RYA_MULTITENANT=1`), plus
  `rya worker` for jobs.
- **AWS**: `deploy/aws/template.yaml` (SAM) — Cognito, ALB, ECS Fargate (ARM64),
  RDS Postgres, Secrets Manager, and a single-purpose mutator Lambda.
  `deploy/aws/Dockerfile.baked` builds the image with an agent baked at `/project`.

**Deploy gotcha (image updates):** `--disable-rollback` **blocks TaskDefinition
replacement**, so an image update fails with *"Replacement type updates not
supported on stack with disable-rollback"* (the old task keeps serving). Deploy
image updates *without* `--disable-rollback`; from `UPDATE_FAILED` you must
`aws cloudformation rollback-stack` first, then re-deploy.

---

## 15. Environment variables

| Var | Purpose |
|---|---|
| `RYA_TOKEN` | Operator token (single-tenant auth) |
| `RYA_DATABASE_URL` | Switches the substrate to Postgres |
| `RYA_MULTITENANT` | `1` → workspaces + API keys + RLS |
| `RYA_SECRET_KEY` | Fernet key — encryption-at-rest for connections |
| `RYA_SESSION_SECRET` | Signing key for onboarding session tokens (falls back to `RYA_SECRET_KEY`) |
| `RYA_ADMIN_TOKEN` | Gates `POST /v1/projects` (fails closed when unset) |
| `RYA_JWKS_URL`, `RYA_JWT_SECRET` | Edge identity (e.g. Cognito) |
| `RYA_LLM_MODEL`, `RYA_OPENAI_MODEL`, `RYA_EMBEDDINGS`, `RYA_EMBEDDING_MODEL` | Model selection |
| `RYA_PRICE_<MODEL>_IN` / `_OUT` | Cost accounting (per 1M tokens) |
| `RYA_TRACE_WEBHOOK`, `LANGFUSE_*`, `RYA_OTLP_ENDPOINT`, `RYA_OTLP_HEADERS`, `OTEL_SERVICE_NAME` | Trace export |
| `RYA_DEEPEVAL_MODEL` | Override the DeepEval judge model |
| `RYA_REMOTE_URL`, `RYA_API_KEY` | Point the CLI at a hosted Rya |
| `RYA_CORS_ORIGINS`, `RYA_WEBHOOK_SECRET`, `RYA_SLACK_SIGNING_SECRET` | Edge config |
| `RYA_HOME`, `RYA_GUARD_PATH`, `RYA_APP_DB_PASSWORD` | Paths / infra |

---

## 16. Current state — honest

**Test suite: 137 passed, 9 skipped** (Postgres-gated + the live DeepEval test).

**Verified working (not claims — exercised):**
- **Live on AWS** (ECS Fargate + RDS, multi-tenant). Self-serve signup → workspace
  → API key works on the live URL; the key is workspace-scoped.
- **Durable runtime on RDS**: a triggered run exercised sessions, memory, tools,
  models, jobs, LLM and approvals; it paused for approval and a *separate* request
  resumed it to `completed`.
- **OTLP** validated against a real `otel/opentelemetry-collector-contrib`.
- **Langfuse** validated against a real Langfuse instance (generation carried exact
  token usage).
- **DeepEval** produces real, discriminating scores (faithful → 1.0 pass;
  hallucinated → 0.0 fail) with Claude as judge.

**Known gaps — do not mistake these for done:**

| Gap | Reality |
|---|---|
| **No TLS on the AWS deploy** | HTTP-only. Passwords + API keys travel in **plaintext**. Must fix before sharing publicly. |
| **No worker on the deploy** | `rya worker` exists, but no worker task runs on AWS → scheduled jobs never execute. |
| **Evals are single-tenant only** | `POST /evals/run` → `400 "Evals are single-tenant only."` The console's Run-evals button fails on the cloud. |
| **Connections + knowledge are read-only from the cloud** | No HTTP endpoint to *create* them (CLI/MCP/`ctx` only), so the console shows the views but can't populate them. |
| **Remote MCP** | Unauthenticated when `RYA_TOKEN` is unset, and operates on the baked `/project` rather than the caller's workspace. |
| **LLM defaults to mock** | With no provider key, a new user's agent returns canned output (`tokens: 0`). |
| **Some built-in tools are mocks** | `crm.lookup`, `calendar.read`, `email.send` are mocks (flagged `mock: true`); `web.fetch` and `http.request` are real. |
| **Infra** | Single Fargate task; RDS has no backups/Multi-AZ; no DB connection pooling; no email verification, rate limiting, password reset, or billing. |

The honest summary: **the runtime — the hard part — is real and works in
production.** The *build/configure-from-the-cloud* surfaces are incomplete, and
the deploy is a demo posture, not a product posture.

---

## 17. Repo layout

```
src/rya/
  manifest/        loader.py, schema.py — rya.agent.yaml
  runtime/         engine.py — the durable run loop, journal/replay, approvals
  sdk/context.py   every ctx.* primitive
  providers/       llm.py, embeddings.py, channels.py
  tools/           registry.py, builtins.py (web.fetch, http.request)
  observability/   export.py (webhook/Langfuse/OTLP), usage.py (tokens + cost)
  api/app.py       FastAPI control plane + console + remote MCP mount
  console/         the built-in dashboard (single-file HTML)
  store.py store_postgres.py    the two substrates
  tenancy.py       workspaces, API keys, RLS, self-serve accounts
  accounts.py      PBKDF2 passwords + HMAC session tokens
  seal.py          Fernet encryption-at-rest
  evals.py         the eval harness + scorers (incl. deepeval)
  cloud.py cli.py
deploy/aws/        template.yaml (SAM), Dockerfile.baked
docs/              this doc + the companions
tests/             137 tests
```
