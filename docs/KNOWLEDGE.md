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

Run statuses: `running` → `completed` | `failed` | `rejected` | `waiting_approval`
| `needs_reconnect` (a scoped connection expired mid-turn — `E_CONNECTION_EXPIRED`
gets its own terminal status so the user gets a reconnect prompt, not a failure).

---

## 3. Quickstart

```bash
pip install -e '.[api,llm,mcp]'        # add ,postgres for the Postgres substrate
                                       # add ,s3 for object-store file bytes + bundle archives
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

Eighteen primitives, all journaled:

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
| `ctx.sessions` | Conversation/session handling (`get_or_create`, `append`, `history`, `get`, `search`) |
| `ctx.connections` | Scoped, encrypted third-party credentials (`get`, `list`, `upsert`, `secret`) |
| `ctx.logs` | Structured logs (into the trace) |
| `ctx.traces` | Trace access |
| `ctx.secrets` | Secret access (auto-redacted from traces) |
| `ctx.events` | Emit/consume events |
| `ctx.files` | Uploaded files: `get`, `list`, `read`, `as_document` (feeds `ctx.knowledge`) |
| `ctx.guard` | `check_grounding` (grounding gate), `scrub`/`check_secrecy` (id-secrecy) |
| `ctx.egress` | `fetch(url, method, headers, body)` — the sanctioned outbound request. Mediated (guard verdict + network verdict + audit) and journaled, so a replay after an approval pause returns the memoized response. In a sandbox this is the only route out |

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
- **Bytes live apart from records.** Blobs never belong in a JSONB column, so file
  bytes go to S3 with `RYA_FILES_S3_BUCKET` (only metadata stays in the store) and
  bundle archives with `RYA_BUNDLES_S3_BUCKET` (+ `_PREFIX`/`_REGION`); each falls
  back to a local directory when its bucket is unset. The matching `*_S3_ENDPOINT`
  points at MinIO/Ceph/R2 and *also* switches boto3 to **path-style addressing** —
  leave it blank on real AWS, which wants virtual-host style.

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
check bundle publish           # validate / content-hash / upload a version over HTTP
eval                           # run the eval suite
connect  connections           # scoped credentials (list | reseal | revoke)
provision                      # stand up the full base infrastructure
context                        # one-shot machine-readable snapshot (for coding agents)
status logs                    # runtime state, structured logs for a run
serve worker supervisor mcp    # control plane / job worker / the thing that STARTS workers / MCP
template-host                  # the sandbox half of the D32 pair: warm interpreters, no credentials
posture                        # which tenancy posture is in force, and what is unmet
token                          # operator token
agents events runs approvals   # drive the runtime
tools models channels secrets
schedules jobs skills
workspaces orgs keys cloud     # tenants / the billing boundary above them / seal keys
```

---

## 12. HTTP API

| Group | Routes |
|---|---|
| Health / info | `GET /healthz`, `GET /v1/info` |
| Console | `GET /`, `GET /console` |
| Agents & runs | `GET /agents/{id}`, `POST /agents/{id}/events`, `GET /agents/{id}/runs`, `GET /runs/{id}`, `GET /runs/{id}/trace` |
| Approvals | `GET /approvals`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject` |
| Versions & environments | `POST /agents/{id}/versions` (upload a bundle as a new immutable version — raw `application/gzip` body, `?hash=` required, plus `env`/`promote`/`actor`/`meta.<k>`), `GET /agents/{id}/versions`, `GET /versions/{id}`, `POST /versions/{id}/retire`, `GET /agents/{id}/environments`, `POST …/{env}/promote`, `POST …/{env}/rollback`, `GET …/{env}/history`, `GET`/`PUT /gate`, `GET /gate/check` |
| Onboarding | `POST /v1/signup`, `POST /v1/login`, `GET /v1/me`, `POST /v1/workspaces` |
| Admin | `POST /v1/projects` (gated by `RYA_ADMIN_TOKEN`; fails closed if unset) |
| Guard / evals | `GET`/`PUT` `/guard`, `POST /guard/test`, `GET /evals`, `POST /evals/run` |
| Read surfaces | `GET /connections`, `GET /knowledge`, `POST /knowledge/search`, `GET /sessions`, `GET /tools`, `GET /models`, `GET /channels` |
| Inbound | `POST /inbound`, `POST /slack/events` |
| Remote MCP | `/mcp` (mounted last so its catch-all doesn't shadow API routes) |

Auth: `Authorization: Bearer <RYA_TOKEN>` (single-tenant operator) or a workspace
`rya_sk_…` key (multi-tenant). `X-Rya-User-Token` enables per-user RLS.

**Publishing additionally requires auth to be configured at all.** With no
`RYA_TOKEN` and no multitenancy, `POST /agents/{id}/versions` returns **403**
unless `RYA_ALLOW_UNAUTHENTICATED_PUBLISH=1` — an open control plane elsewhere
leaks data; an open publish route is anonymous code upload to a box whose worker
imports it.

Stable codes map to statuses: `E_VALIDATION` 400, `E_BUNDLE_MISMATCH` 409,
`E_BUNDLE_NOT_FOUND` 404, `E_BUNDLE_STORE` 503 (the operator's bucket, not the
caller's request), `E_PROMOTION_BLOCKED` 422, `E_QUOTA_EXCEEDED` 429. The CLI
re-raises the *server's* code rather than flattening it to `E_REMOTE`, so the
error contract survives the network boundary.

---

## 13. MCP

- **Local**: `rya mcp` (stdio) — an MCP-native coding agent drives Rya directly.
- **Remote**: mounted at `/mcp` on `rya serve` (streamable-http).
- `rya context` gives a coding agent the whole backend state in one call.

---

## 14. Deployment

- **Local**: `rya serve` (FileStore, zero setup).
- **Self-host**: `rya serve` + `RYA_DATABASE_URL` (+ `RYA_MULTITENANT=1`), plus
  `rya worker` for jobs. If the api and the worker do **not** share a filesystem
  (separate containers, separate hosts), also set `RYA_BUNDLES_S3_*`:
  `rya publish` writes the version's archive there and a pinned worker
  (`rya worker --env prod`) reads it back, hash-verifying before it imports
  anything. Without a shared store the worker resolves a version whose artifact it
  cannot see. `docker compose up` wires this to a bundled MinIO.
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
| `RYA_BUNDLES_S3_BUCKET`, `_PREFIX`, `_REGION`, `_ENDPOINT` | Bundle archives in an object store (unset → a local `.rya/bundles` directory); `_ENDPOINT` = MinIO/Ceph/R2, and forces path-style addressing |
| `RYA_FILES_S3_BUCKET`, `RYA_FILES_S3_ENDPOINT` | File **bytes** in an object store; metadata stays in the store |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Credentials for both of the above (boto3's own chain) |
| `RYA_MAX_BUNDLE_BYTES` | Publish body cap, default 20 MB — over it, `413` |
| `RYA_ALLOW_UNAUTHENTICATED_PUBLISH` | `1` → allow `POST /agents/{id}/versions` with auth off (local loops only) |
| `RYA_PROJECT` | Which project tree `docker compose` mounts at `/project` |
| `RYA_WORKSPACE_A`, `RYA_WORKSPACE_B` | Which workspaces `docker-compose.multitenant.yml`'s two tenant claimers serve; `rya supervisor --all-workspaces` is the answer past a worked example |
| `RYA_REMOTE_URL`, `RYA_API_KEY` | Point the CLI at a hosted Rya |
| `RYA_CORS_ORIGINS`, `RYA_WEBHOOK_SECRET`, `RYA_SLACK_SIGNING_SECRET` | Edge config |
| `RYA_HOME`, `RYA_GUARD_PATH`, `RYA_APP_DB_PASSWORD` | Paths / infra |
| `RYA_EXECUTION_DRIVER` | How the supervisor launches workers: `local` (default), `docker`, `kubernetes` |
| `RYA_CONTAINER_RUNTIME` | `runsc` makes the `docker` driver `sandboxed`; anything else is a shared kernel |
| `RYA_K8S_RUNTIME_CLASS` | The gVisor RuntimeClass, default `gvisor`; `none` opts out and downgrades the claim |
| `RYA_SANDBOX_IMAGE`, `RYA_SANDBOX_MEMORY`, `RYA_SANDBOX_CPUS` | What a sandbox runs and what it may consume |
| `RYA_UNTRUSTED_TENANTS` | `1` → declare the hostile-tenant posture. Refuses to start unless mediation, a sandbox, network egress AND a driver that launches the broker outside the tenant's sandbox are ALL in force (D32) |
| `RYA_TEMPLATE_HOST` | Path to the template host's socket (D32). Set on **both** halves of the pair: the sandbox container binds it, the claimer dials it. Unset → the claimer spawns its own templates (the weak topology, and the default) |
| `RYA_TEMPLATE_HOST_TOKEN` | Shared secret for the host's control surface. Not a credential to anything outside the pair, and scrubbed from a template's environment before tenant code runs — a tenant that could drive it could evict a sibling agent's warm interpreter |
| `RYA_CLAIMER_MEMORY`, `RYA_CLAIMER_CPUS` | The *claimer* half's limits, separate from `RYA_SANDBOX_*`. Default `256m` / `0.5`: it imports nothing and mostly waits on a socket |
| `RYA_CLAIMER_SCOPE` | `version` (default: one claimer per workspace+agent+version) \| `tenant` (one per workspace, forking whichever version each item is pinned to). An unrecognised value is refused, not defaulted |
| `RYA_POOL_MAX_ENTRIES` | Warm interpreters one claimer holds. Default 4 at version scope, 12 at tenant scope |
| `RYA_BROKER` | `1` → mediate tenant IO (D18). Requires `--fork` (implied by `--scope tenant`); the tenant process gets a socket instead of the DSN, the seal keys and the provider key |
| `RYA_BROKER_SOCKET` | Where the broker listens. On the claimer it means *bind here* — needed on the D32 pair so the other container can reach it; unset, the broker picks a private 0700 temp directory |
| `RYA_EGRESS` | `none` (default, restricts nothing) \| `proxy` \| `netpolicy` — what the substrate actually enforces |
| `RYA_KEY_PROVIDER` | `deployment` (default) \| `derived` \| `wrapped`. Only `wrapped` can crypto-shred a tenant (D31) |
| `RYA_KMS_KEY_ID` | Wrap per-tenant keys with AWS KMS instead of the root key |
| `RYA_ACTOR` | Who a CLI lifecycle action is recorded as. Falls back to the OS user |

---

## 16. Current state — honest

**Test suite: 63 files, 562 test functions** (before parametrization; skips are
Postgres-gated plus the live-provider and DeepEval tests).
Run with provider keys **unset** — a present `ANTHROPIC_API_KEY` silently routes
mock-expecting tests to the live API (`RYA_FORCE_MOCK=1` overrides it); and pin
`mcp<2`, since `mcp` 2.0.0 removed `streamablehttp_client`.

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
| **One agent per WORKER, not per deployment** | `build_app` reads no manifest since D21: agents come from `rya_versions`/`rya_environments` and `{id}` in the route paths is real, so one api serves many and `POST /agents/{id}/versions` accepts an unknown name. `load_agent` mutates `sys.path` and never unloads, so a second agent costs a second worker — not a second api, port, database or bundle store. |
| **Publish files no readiness attestation** | The control plane never imports a bundle, so `POST /agents/{id}/versions` cannot evaluate readiness: the response says `"attested": false`, and a readiness-gated environment refuses the promotion (`E_PROMOTION_BLOCKED`). Use `rya deploy --env` from a box with store access. |
| **Evals are single-tenant only** | `POST /evals/run` → `400 "Evals are single-tenant only."` The console's Run-evals button fails on the cloud. |
| **Connections + knowledge are read-only from the cloud** | No HTTP endpoint to *create* them (CLI/MCP/`ctx` only), so the console shows the views but can't populate them. |
| **Remote MCP** | Unauthenticated when `RYA_TOKEN` is unset, and operates on the baked `/project` rather than the caller's workspace. |
| **LLM defaults to mock** | With no provider key, a new user's agent returns canned output (`tokens: 0`). |
| **Some built-in tools are mocks** | `crm.lookup`, `calendar.read`, `email.send` are mocks (flagged `mock: true`); `web.fetch` and `http.request` are real. |
| **The fleet spans boxes on paper, and has never done it** | `rya supervisor` starts, scales and reaps through the `ExecutionDriver` seam, and `docker`/`kubernetes` exist beside `local`. Both container drivers launch the D32 *pair* — a credentialed claimer container beside a credential-free sandbox running `rya template-host` — so `RYA_UNTRUSTED_TENANTS=1` is launchable rather than a permanent refusal. What has not happened is a cluster: the `kubernetes` driver renders a manifest and applies it with `kubectl`, and none has been applied. `ecs` is unwritten. |
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
  worker.py        the execution plane — claims from the queue, loads the bundle
  execution/       who starts the workers: drivers.py (substrate seam),
                   supervisor.py (the policy), pool.py (fork per run)
  bundles.py       content-hash / pack / verify + the archive store (local | S3)
  deployments.py   immutable versions, environments, promote, rollback
  gates.py         promotion gates — readiness + evals as admission checks
  readiness.py     the production-readiness checklist (blocks, warnings, fixes)
  queue.py         the durable job queue (lease / heartbeat / DLQ)
  turns.py         durable, resumable chat turns (survive a dropped connection)
  quotas.py        per-workspace quotas — concurrency, runs, tokens, cost
  guard.py         egress firewall + grounding gate + id-secrecy scrub
  config.py        per-environment run config: model routes, values, secrets
  snapshot.py      what `rya context` returns — the live state in one payload
  store.py store_postgres.py    the two record substrates
  files_s3.py      the S3 bytes backend for the files primitive
  tenancy.py       workspaces, API keys, RLS, self-serve accounts
  accounts.py      PBKDF2 passwords + HMAC session tokens
  seal.py          Fernet encryption-at-rest
  evals.py         the eval harness + scorers (incl. deepeval)
  cloud.py         RemoteClient — drive a hosted Rya, preserving its E_* codes
  cli/main.py      the operator CLI (platform)
  cli/client.py    the thin SDK CLI — where `check` / `bundle` / `publish` live
deploy/aws/        template.yaml (SAM), Dockerfile.baked
docs/              this doc + the companions
tests/             63 files, 562 test functions
```
