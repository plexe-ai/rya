# Rya — Deep Dive

The detailed reference for Rya: the production backend/runtime for AI agents.
For the front-door summary see the [README](../README.md); for the
vision-vs-built gap see [VISION_GAP.md](VISION_GAP.md); for the OSS/cloud
architecture see [architecture.md](architecture.md).

---

## 1. What Rya is

Agent demos are easy; production agents are hard. The hard 90% — identity,
durable execution, memory, permissioned tools, human approvals, channels, jobs,
model routing, secrets, traces, cost — is the same internal platform every
serious AI team eventually rebuilds. Rya is that platform, exposed as clean
primitives.

Two design commitments shape everything:

1. **Coding-agent-first.** The primary user is a coding agent (Claude Code,
   Codex, Cursor), not a human clicking a dashboard. Every surface is
   machine-readable (`--json`), self-correcting (stable error codes + a fix),
   idempotent, and resumable.
2. **Production is a checklist, not expertise.** A model can write a handler; it
   can't *know* production. So Rya encodes "what production requires" as a
   green checklist (`rya deploy --check`) the agent satisfies — and as hard
   guards (Action Guard, approval gates, secret enforcement) that make unsafe
   agents un-shippable.

---

## 2. The agent's journey

```
orient → scaffold → author → prove → check → deploy → verify → operate
  │         │          │        │       │        │        │         │
rya       rya        SDK +   triggers  rya     rya      synthetic  runs,
context   create     manifest + traces  deploy  deploy   event +    traces,
                                        --check          trace      replay
```

- **orient** — `rya context --json` returns the whole state *and* the
  production-readiness verdict *and* the invariants to respect, in one call.
- **scaffold** — `rya create` lays down a runnable, safe-by-default project.
- **author** — write `src/agent.py` (the handler) + `rya.agent.yaml` (the
  contract). Validation errors carry the exact fix.
- **prove** — trigger synthetic events, read the deterministic trace, assert.
- **check** — `rya deploy --check` until green.
- **deploy** — gated on readiness; emits a self-contained image + plan.
- **verify** — hit the deployed runtime, read the trace.
- **operate** — runs, approvals, traces; fix → redeploy.

---

## 3. The manifest — `rya.agent.yaml`

The declarative contract, validated before any run or deploy
([manifest/schema.py](../src/rya/manifest/schema.py)).

```yaml
name: support-followup-agent
runtime: python                 # python (node reserved)
entrypoint: src/agent.py
version: 0.1.0
timeout_seconds: 300            # per-run-segment execution timeout
model:
  provider: auto                # auto | mock | anthropic | openai | bedrock | adapter
  default: claude-haiku-4-5     # model name
  fallback: gpt-4.1-mini        # used on provider failure
  temperature: 0.2
memory:
  collections: [conversations, customer_context]
tools:
  - id: crm.lookup
    permission: allowed         # read_only | allowed | approval_required | disabled
  - id: email.send
    permission: approval_required
  - id: remote.score
    url: https://scorer.example.com/score   # HTTP tool
models:
  - { id: churn-risk-v1, type: custom, permission: allowed }
channels:
  - { type: webhook, path: /inbound }
  - { type: slack, enabled: false }
triggers:
  - { id: daily, type: cron, schedule: "0 9 * * *", handler: daily_followup }
approvals:
  default: required_for_external_actions
observability: { traces: true, audit: true }
```

The manifest is **environment-invariant** (D11): there is no `environment:` field,
and one content-hashed bundle is promoted *between* dev/staging/prod. Anything
that differs per environment — model routes, egress allowlists, credentials,
trace export — is per-environment platform state, not a manifest field. A
leftover `environment:` line is a readiness **block** (`E_MANIFEST_ENVIRONMENT`).

A companion `rya.guard.yaml` holds the egress policy (§11).

---

## 4. The SDK and the `ctx` surface

Agent code is business logic; everything around it is the platform.

```python
from rya import define_agent
agent = define_agent()

@agent.on_event
async def handle_event(ctx, event):
    await ctx.memory.append("conversations", {"event": event.model_dump()})
    customer = await ctx.tools.call("crm.lookup", {"email": event.payload["email"]})
    risk = await ctx.models.call("churn-risk-v1", {"customer_id": customer["id"]})
    if risk["score"] > 0.8:
        msg = await ctx.llm.respond(system="Draft a follow-up.", input={"customer": customer})
        result = await ctx.approvals.request(            # PAUSES the run
            title="Send follow-up", body=msg.text,
            action={"tool": "email.send", "input": {"to": customer["email"], "body": msg.text}})
        await ctx.channels.send("email", {"messageId": result["actionResult"]["messageId"]})

@agent.tool("remote.score")                              # a real async tool
async def score(input): ...

@agent.job("daily_followup")
async def daily_followup(ctx, job): ...
```

`ctx` exposes seventeen namespaces: `llm · models · memory · knowledge · tools ·
channels · jobs · cron · approvals · sessions · files · connections · logs ·
traces · secrets · events · guard` — plus `ctx.identity` (a plain attribute: the
verified user, or `None`).

---

## 5. The runtime — durable execution

[runtime/engine.py](../src/rya/runtime/engine.py). Events create **runs**; runs
are durable and journaled to the store.

- **Pause/resume via journal replay.** Every side-effecting `ctx` op is journaled
  by sequence number. `ctx.approvals.request()` unwinds the coroutine
  (`PausedForApproval`); a *separate* approve invocation resolves it and replays
  the handler — prior steps return memoized results, so only post-approval code
  runs for real. The constraint is the standard one: issue `ctx` ops in a
  deterministic order. This survives process restarts on Postgres.
- **Nested event loops.** The engine runs handlers safely whether called from
  sync (CLI) or inside an event loop (MCP/API) via `_run_coro` (worker-thread
  fallback).
- **Timeouts.** `manifest.timeout_seconds` wraps the handler in
  `asyncio.wait_for` → `E_TIMEOUT` on overrun.
- **Jobs** carry `attempts/maxAttempts`; failures retry with exponential backoff
  (`min(30, 2^(n-1))s`) until exhausted → the dead-letter queue. `rya worker`
  drains due jobs (atomic claim via Postgres `FOR UPDATE SKIP LOCKED` — run many
  concurrently).

---

## 6. The ten primitives

| Primitive | What it is | Where |
|-----------|-----------|-------|
| **Identity** | owner, version, permission levels; per-run identity from a verified JWT | manifest, auth.py |
| **Runtime** | durable runs: pause/resume, retries, timeouts, cron | runtime/engine.py |
| **Memory** | key-value, collections, **vector recall** (embeddings + cosine) | sdk/context.py, providers/embeddings.py |
| **Tools** | typed, permissioned (`read_only/allowed/approval_required/disabled`), audited; `@agent.tool`, HTTP tools, or registry | tools/, sdk/context.py |
| **Models** | registry + gateway; real Anthropic/OpenAI, fallback, per-call usage | providers/llm.py, models/ |
| **Approvals** | durable human-in-the-loop pause/resume | approvals/, runtime |
| **Events & jobs** | webhooks, cron, delayed jobs, retries/backoff, DLQ | runtime, api |
| **Channels** | webhook + real Slack/email (Resend) + mock, one interface | providers/channels.py |
| **Observability** | forensic per-run trace, token/cost, Langfuse/webhook export | observability/ |
| **Secrets** | `ctx.secrets.get`, names-only listing, redacted from traces | sdk/context.py |

Tool permission rules are enforced: an `approval_required` tool **cannot** be
called via `ctx.tools.call` — it must flow through `ctx.approvals.request`.
Secret values read via `ctx.secrets.get` are added to a redaction vault and
scrubbed from every trace/log.

---

## 7. Persistence and multi-tenancy

`open_store(root)` picks the backend with no code change
([store.py](../src/rya/store.py)):

| Env | Backend | Use |
|-----|---------|-----|
| (none) | `FileStore` (`.rya/` JSON) | local dev, CI, tests — offline & reproducible |
| `RYA_DATABASE_URL` | `PostgresStore` (JSONB) | self-host + cloud, durable |

**Multi-tenancy** ([tenancy.py](../src/rya/tenancy.py), `RYA_MULTITENANT=1`):
workspaces + SHA-256-hashed API keys, with two isolation layers — app-layer
`workspace_id` filtering **and** Postgres **row-level security** (FORCE policies
+ a non-superuser `rya_app` role + `app.workspace_id`/`app.user_id` GUCs). Proven
that an unfiltered `SELECT *` returns only the caller's rows, and that **per-user
RLS** keeps one user from seeing another user's runs within a workspace.

---

## 8. Auth

Single deployed agent, modes by env (all enforced server-side):

| Configured | Mode | Who |
|------------|------|-----|
| nothing | open (local dev) | — |
| `RYA_TOKEN` | operator token | one operator |
| `RYA_JWT_SECRET` / `RYA_JWKS_URL` | per-user JWT (HS256 / RS256-JWKS) | end users |
| `RYA_MULTITENANT=1` + Postgres | API keys → workspace, RLS | tenants |

"Open" has one exception: `POST /agents/:id/versions` (`rya publish`) is refused
with **403 `E_UNAUTHORIZED`** while auth is off (`auth_enabled()` in
[api/app.py](../src/rya/api/app.py)). An open control plane elsewhere leaks reads
and writes; an open publish route means *anonymous code upload* to a box whose
worker imports it. `RYA_ALLOW_UNAUTHENTICATED_PUBLISH=1` re-enables it for a
local-only loop.

Inbound webhooks add `RYA_WEBHOOK_SECRET` (HMAC) and `RYA_SLACK_SIGNING_SECRET`
(Slack signature) — independent of operator auth, since third-party senders hold
the signing secret, not the token.

---

## 9. Coding-agent surfaces

The same operations over three surfaces; see [mcp.md](mcp.md), [devex.md](devex.md).

- **CLI** — every command takes `--json`; `--non-interactive` is on the handful
  that could otherwise prompt (`deploy`, `publish`, `eval`, `provision`,
  `events send`, `approvals approve/reject`). Failures return
  `{ok:false, error:{code, message, hint, exit_code}}`. Exit codes are semantic
  (§16) so an agent branches without parsing prose.
- **MCP** — `rya mcp` (stdio), **25 `rya_*` tools** including `rya_context`
  (orient) and `rya_check_readiness` (the gate). Register with `{"command":"rya","args":["mcp"]}`.
- **Skills** — `rya skills install` writes two progressive-disclosure modules:
  `rya` (authoring) and `rya-ops` (operating).
- **`rya context`** — the one-call orient: full state + readiness verdict + the
  invariants to respect, so the agent never discovers by trial and error.

---

## 10. Production-readiness gate

[readiness.py](../src/rya/readiness.py). `rya deploy --check --json` returns
`{ready, blocks, warnings}`; each item has a stable `code` + an exact `fix`.

**Blocks** (deploy-stopping): `E_NO_EVENT_HANDLER`, `E_RUNTIME_UNSUPPORTED`,
`E_TOOL_NO_IMPL`, `E_UNGATED_SIDE_EFFECT` (a side-effecting tool with permission
`allowed` while the approval policy is `required_for_external_actions`),
`E_SECRET_UNSET`.
**Warnings** (advisory): `W_LLM_MOCK`, `W_STORE_FILE`, `W_NO_TRACE_EXPORT`,
`W_NO_EVALS`, `W_NO_COST_CAP`.

`rya deploy --check` exits `7` if any block remains. A plain `rya deploy` runs
the check as a **hard gate** → `E_NOT_PRODUCTION_READY` unless `--force`. The
verdict also ships inside `rya context`.

---

## 11. Action Guard — egress policy

[guard.py](../src/rya/guard.py). Every outbound request the runtime makes — HTTP
tools, model calls, channel sends, embeddings — is evaluated **before the bytes
leave the process**:

```
SSRF blocklist  →  deny rules  →  allow rules  →  default (deny | allow)
```

- Policy in `rya.guard.yaml`, hot-reloaded by mtime per request; **no-op if
  absent** (opt-in, backward-compatible).
- Rule kinds: `prefix` (startswith), `glob` (fnmatch), `exact`; optional method
  scoping; `deny` always beats `allow`.
- **SSRF**: blocks loopback/private/link-local/reserved IP literals and
  `localhost`, `*.internal`, `169.254.169.254`, metadata hosts.
- A blocked request raises `E_EGRESS_BLOCKED` and never goes out.
- `POST /guard/test` runs a benign+attack probe suite and scores the policy
  (attacks blocked, benign false-blocks, decision accuracy) — surfaced in the
  console's **Action Guard** page, editable live (`GET`/`PUT /guard`).

> Note: the LLM policy *judge* (evaluate-with-a-model when no static rule
> matches) is scaffolded in the UI but not yet wired to a real model; `default`
> handles the no-match case today.

### Policy is not the same as enforcement (D24)

An in-process check is bypassed by any code that does not call it, so against a
*hostile* handler this module enforces nothing — it is a policy, an audit trail and a
set of attributable verdicts, all of which it is genuinely good at. Enforcement lives
one layer down, in [egress.py](../src/rya/egress.py) and the sandbox's network:

| layer | answers | mechanism |
|---|---|---|
| `guard.py` | *what is allowed* | the reviewable allowlist, versioned and etagged |
| `egress.py` + the sandbox | *what can physically leave* | no network route; the only way out is a mediated `ctx.egress.fetch` |

Both verdicts are evaluated on every mediated request, and a **divergence** is
recorded rather than resolved: the sandbox's network rules are a snapshot taken when it
started and the policy is live, so promoting a new allowlist produces disagreement
until every sandbox is recycled. Disagreement **fails closed** — trusting the live
policy would let a request out through a network the operator has not permitted;
trusting the snapshot would keep enforcing a rule already revoked. `egress.reconcile`
rolls the divergences up across a fleet with the action to take.

`guard.py` describing itself as a firewall was accurate about a cooperative runtime and
an overclaim about a hostile one, which is why the wording changed with D24 rather than
the module being deleted.

---

## 12. Observability

- **Traces** — every run is a forensic record: events, tool/model/memory/approval
  /channel/job steps, retries, final status, with timings. `rya runs trace`,
  `GET /runs/:id/trace`.
- **Usage & cost** — token usage is summed from the trace (replay-safe);
  cost is computed only when `RYA_PRICE_<MODEL>_IN/_OUT` is configured (no
  hard-coded prices). [observability/usage.py](../src/rya/observability/usage.py).
- **Export** — on a terminal run, traces are pushed to **Langfuse**
  (`LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`), an **OTLP/HTTP** collector
  (`RYA_OTLP_ENDPOINT`, + `RYA_OTLP_HEADERS`/`OTEL_SERVICE_NAME` — GenAI semantic
  conventions, so Phoenix/Tempo/Datadog read it), or a generic
  `RYA_TRACE_WEBHOOK`; all three stdlib-only, best-effort, never fails a run.
  [observability/export.py](../src/rya/observability/export.py).
- **Secret redaction** — secret values are scrubbed from traces/logs.

---

## 13. The web console

`rya serve` serves a built-in console at `/`
([console/index.html](../src/rya/console/index.html)). It's a data-driven
frontend over `GET /console` (auth-gated), with views: **Overview** (stats +
primitive grid + `rya context` terminal), **Infrastructure** (compute, data
substrate, auth/RLS, observability — live from the process), **Manifest**,
**Tools/Memory/Models/Channels**, **Runs & traces** (clickable forensic trace),
**Approvals** (working approve/reject), **Action Guard** (editable policy + test
suite), **Jobs & cron**, **Secrets**. Same-origin by default; a Connect dialog
prompts for the token when auth is on.

---

## 14. Deployment

- **Self-host (OSS):** `docker compose up` brings up four services —
  `postgres`; `minio`, the S3-compatible bundle-archive store the api and workers
  share; `rya`, the api (control plane, `RYA_API_INLINE_WORKER: "0"` so it runs no
  handler code); and `worker`, the execution plane that claims from the queue and
  executes handlers. A fifth, `worker-pinned`, sits behind the `pinned` profile
  (`docker compose --profile pinned up -d worker-pinned`) and runs
  `rya worker --env prod` — one content-hashed bundle, so a re-publish needs a
  restart. Your project mounts at `${RYA_PROJECT:-./examples/followup_agent}`
  → `/project`, with `/project/.rya` on a named volume (`rya_project_state`) so
  the containers' root-owned state never lands in your working tree.
  [docker-compose.yml](../docker-compose.yml). Or
  `rya deploy --target docker|fly|render` generates a self-contained image
  (agent baked in; state external via `RYA_DATABASE_URL`) + the exact command.
  [deploy_templates.py](../src/rya/cli/deploy_templates.py).
- **AWS reference:** [deploy/aws/template.yaml](../deploy/aws/template.yaml) — a
  single-tenant SAM/CloudFormation stack (Cognito, ECS Fargate + ALB, RDS
  Postgres, ElastiCache, Secrets Manager, mutator Lambda), cfn-lint clean.
  `sam deploy` is an operator step (real, billable).
- **Distribution:** `uv build` → wheel/sdist; `uvx rya serve` ships the console.
  Not yet published to PyPI. The platform image installs
  `.[api,postgres,llm,mcp,s3]` ([Dockerfile](../Dockerfile)) — the `s3` extra
  (boto3) is not optional in practice, because it is what lets bundle archives and
  file bytes live in an object store rather than on one container's disk. Without
  it `rya publish` fails with `E_BUNDLE_STORE`.

---

## 15. Testing

`pytest` — **63 test files, 562 test functions** (before parametrization, so the
collected count is higher). Skips are Postgres-gated (run those with
`RYA_TEST_DATABASE_URL=…`) plus the live-provider and DeepEval tests.

Two things to know before running it. **Unset provider keys first** — with
`ANTHROPIC_API_KEY` present, `_concrete_provider` in
[config.py](../src/rya/config.py) (reached via `resolve_model_route`) resolves
`auto` → `anthropic` from the key's mere presence, so tests written against the
deterministic mock silently hit the live API. `RYA_FORCE_MOCK=1` overrides it. And
**`rya[mcp]` must be `<2`**: `pyproject.toml` declares `mcp>=1.2.0` with no upper
bound, but `mcp` 2.0.0 removed `streamablehttp_client`, which breaks both
`test_remote_mcp.py` tests.

Coverage includes: manifest validation, the durable
pause/resume slice (file + Postgres, cross-process), per-user RLS, JWT, real
tool/channel HTTP delivery, the signed-webhook + auth flows, the
production-readiness gate, the console aggregate, and the **Action Guard**
enforcement (a blocked request provably never reaches its destination).
External IO (default tools/models/LLM) is deterministic so runs are reproducible
and assertable.

---

## 16. Reference

### CLI

```
login init create dev check status context logs
deploy(--env/--promote/--target/--check/--force/--actor/--metadata/--write
       | aws|status|destroy: --region/--stack/--count/--ha
                             --skip-build/--langfuse/--yes)
publish(--env/--promote/--actor/--metadata/--url/--key
        --skip-check/--non-interactive)
agents(list/inspect) events(send) runs(list/trace) approvals(list/approve/reject)
tools(list/register) models(list/register) channels(list/connect)
secrets(set/list) schedules(list/create/run) jobs(list/run/dlq/retry)
skills(install/path) workspaces(create/list) keys(create) token mcp serve worker
supervisor(--plan/--once/--all-workspaces)   # starts, scales and reaps workers
```

`check` (manifest + handler set, starts nothing — what CI runs) and `publish`
(the deploy pipeline over HTTP, for a repo with no database or bucket access) are
defined in the thin client CLI [cli/client.py](../src/rya/cli/client.py) and
re-registered in [cli/main.py](../src/rya/cli/main.py), so they survive an
editable install of the platform. `rya deploy --env` is the same pipeline as
`publish`, run locally by an operator who already has both.

### HTTP API (single-tenant)

```
GET  /                      console page          GET  /console        live aggregate
GET  /healthz                                     GET  /guard          egress policy
POST /inbound               signed webhook        PUT  /guard          save policy
POST /slack/events          Slack adapter         POST /guard/test     score policy
POST /agents/:id/events     trigger a run         GET  /agents/:id     manifest
GET  /agents/:id/runs       GET /runs/:id         GET  /runs/:id/trace
GET  /approvals             POST /approvals/:id/approve  /reject
GET  /tools /models /channels
GET  /agents/:id/versions   list versions         POST /agents/:id/versions  publish
```

`POST /agents/:id/versions` takes the raw `application/gzip` bundle as the request
body with `?hash=` (required) plus `env`, `promote`, `actor` and repeatable
`meta.<k>=<v>` provenance. It rebuilds the hash from the bytes it received rather
than trusting the sidecar (`E_BUNDLE_MISMATCH`, 409), caps the body at
`RYA_MAX_BUNDLE_BYTES` (413), and **never imports the bundle** — the control plane
does not run tenant code — so the response carries
`"attested": false, "notAttested": ["readiness"]`.

### Error / exit codes

| Exit | Codes |
|------|-------|
| 1 generic | `E_RUNTIME`, `E_TIMEOUT`, `E_BUNDLE_STORE` |
| 3 manifest | `E_MANIFEST_*`, `E_ENTRYPOINT_NOT_FOUND`, `E_AGENT_NOT_DEFINED` |
| 4 not-found | `E_*_NOT_FOUND` (run/approval/tool/model/job/handler/version/bundle/environment) |
| 5 permission | `E_TOOL_PERMISSION_DENIED`, `E_UNAUTHORIZED`, `E_BAD_SIGNATURE`, `E_EGRESS_BLOCKED` |
| 6 state | `E_APPROVAL_NOT_PENDING`, `E_RUN_NOT_PAUSED`, `E_BUNDLE_MISMATCH` |
| 7 validation | `E_VALIDATION`, `E_NOT_PRODUCTION_READY` |

A code not in [errors.py](../src/rya/errors.py)'s table falls through to exit 1 —
which is why a *declared* code is part of the contract, not paperwork. Over HTTP
the same codes carry a status: `E_BUNDLE_STORE` **503** (the operator's bucket, not
the caller's request), `E_BUNDLE_NOT_FOUND` **404**, `E_BUNDLE_MISMATCH` **409**,
`E_PROMOTION_BLOCKED` **422**, `E_QUOTA_EXCEEDED` **429**. `RemoteClient`
([cloud.py](../src/rya/cloud.py)) re-raises the server's code instead of collapsing
it to `E_REMOTE`, so the contract survives the network boundary — a caller can
still tell a content-hash mismatch from an unreachable bucket.

### Key environment variables

| Group | Vars |
|-------|------|
| Persistence | `RYA_DATABASE_URL` / `DATABASE_URL`, `RYA_APP_DB_PASSWORD` |
| Auth | `RYA_TOKEN`, `RYA_JWT_SECRET`, `RYA_JWKS_URL`, `RYA_MULTITENANT` |
| Webhooks | `RYA_WEBHOOK_SECRET`, `RYA_SLACK_SIGNING_SECRET` |
| Models | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `RYA_LLM_MODEL`, `RYA_OPENAI_MODEL` |
| Channels | `SLACK_WEBHOOK_URL`, `RESEND_API_KEY`, `RYA_EMAIL_FROM`, `RYA_CHANNEL_<TYPE>_URL` |
| Cost/obs | `RYA_PRICE_<MODEL>_IN/_OUT`, `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`, `RYA_TRACE_WEBHOOK`, `RYA_OTLP_ENDPOINT`, `RYA_OTLP_HEADERS`, `OTEL_SERVICE_NAME` |
| Object stores | `RYA_BUNDLES_S3_BUCKET`, `_PREFIX`, `_REGION`, `_ENDPOINT`†; `RYA_FILES_S3_BUCKET`, `RYA_FILES_S3_ENDPOINT`†; `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` |
| Publish | `RYA_MAX_BUNDLE_BYTES` (default 20 MB → 413), `RYA_ALLOW_UNAUTHENTICATED_PUBLISH` |
| Compose | `RYA_PROJECT` (which project tree to mount), `RYA_API_INLINE_WORKER` |
| Guard | `RYA_GUARD_PATH` |

† Each object store falls back to a local directory when its `_BUCKET` is unset. A
declared `*_S3_ENDPOINT` means "S3-compatible, not S3" (MinIO/Ceph/R2) and also
forces **path-style addressing** — botocore has no env var for
`s3.addressing_style`, so it must be passed on the client, and left at the default
`http://minio:9000` + bucket `rya-bundles` becomes `http://rya-bundles.minio:9000`
and fails to resolve. Leave it blank on real AWS, which wants the virtual-host form.

---

## 17. Honest status

- **Real & verified:** durable runs on Postgres (cross-process pause/resume),
  per-user RLS multi-tenancy, real Anthropic/OpenAI + Slack/email seams (wiring
  proven; live success needs your keys), signed webhooks + token/JWT auth, the
  web console, the production-readiness gate, the Action Guard (network-level
  enforcement), job retry/DLQ, the TS client, deploy artifacts, and `uvx`.
- **Mocked:** the default tool/model implementations (deterministic IO; the
  registries/permissions/traces around them are real).
- **One control plane serves many agents; one worker serves one.** `build_app` no
  longer loads a manifest (D21) — agents come from published versions and
  environment pointers, so `:id` in the route paths above is real and
  `POST /agents/:id/versions` accepts an agent this deployment has never heard of.
  What still holds one agent is the *worker*: `load_agent` mutates `sys.path` and
  never unloads. `rya worker --fork` (D27) moves that import into a warm interpreter
  the claimer forks per run, so the long-lived process holds no tenant code — which
  does not raise the count (a fork is still one agent on one version) but makes
  raising it later a config change. Agent-scoped routes live under `/agents/{id}/…` (D28); the
  unprefixed spellings resolve while a deployment serves one agent, with
  `Deprecation`/`Sunset` headers, and 400 naming the candidates once it serves
  several. `_` is the reserved "the one agent" alias.
- **The publish path files no readiness attestation.** The control plane never
  imports a bundle, so readiness is not evaluated: the response says
  `"attested": false, "notAttested": ["readiness"]`, and a readiness-gated
  environment refuses the promotion (`E_PROMOTION_BLOCKED`). There is no
  `rya attest readiness` command — the attestation is filed by
  `rya deploy --env`, which runs the gate locally because it has the store.
- **Workers schedule themselves.** `rya supervisor` (D25) watches claimable depth per
  key and the worker registry, then starts, scales, pre-warms and reaps through a
  pluggable `ExecutionDriver` (D26) — so scale-to-zero is two-way rather than a one-way
  exit. Three drivers: `local` (a subprocess here, isolation `none`), `docker` and
  `kubernetes`. It holds a per-workspace lease (D34), so a second replica stands by and
  logs the plan it did not apply instead of doubling the fleet.
- **One claimer can serve a whole tenant.** `RYA_CLAIMER_SCOPE=tenant` (D27/#19-8b)
  makes sandbox count track active tenants rather than the agents×versions product: the
  claimer peeks at the queue, warms the version the next item is pinned to — which is
  the preflight, before the claim — and forks a child for it. Dispatches are shared
  equally across the tenant's own agents (D33), because starving a sibling is the
  question `concurrency_key` answers between workspaces and this one answers within one.
- **Orgs are the billing boundary above a workspace** (D29). Usage is metered at
  `workspace_id` and budgeted at `org_id`; the rollup is computed by
  `rya orgs reconcile` with the admin DSN and its *verdict* pushed down into each
  member workspace's own policy row, so no tenant's admission path holds a credential
  that can read a sibling (D35). The supervisor's multi-workspace fan-out schedules it
  (`SupervisorPolicy.reconcile_orgs_seconds`, default 300s), and `orgs.freshness`
  reports a verdict nothing is refreshing — which is the case for a deployment running
  neither a supervisor nor a cron, and it now says so rather than looking current.
- **The untrusted-tenant posture is built and gated.** Tenant code runs in a fork with
  no credentials (D18: it gets a Unix socket and a capability that expires with the
  dispatch), in a gVisor sandbox with no network route (D23/D24), against per-tenant
  seal keys that a purge can destroy (D31). `RYA_UNTRUSTED_TENANTS=1` refuses to start
  unless all four boundaries are in force — `rya posture` shows the answer without a
  deploy. **The fourth is D32, and both container drivers satisfy it since Phase 6**:
  a launch is a *pair* — a credentialed claimer container beside a credential-free
  sandbox container running `rya template-host`, sharing an in-memory volume for the
  two sockets. Phase 5 had found that one container could not be both, and the fix
  turned out not to be repairing either environment builder but adding the container
  that was missing. A driver that launches only the sandbox half is still refused.
- **Not built / not proven:** a *managed* cloud deploy (`rya deploy` emits self-host
  artifacts — `sam deploy` is manual), the Action Guard LLM judge, remote MCP + OAuth, a
  PyPI publish and the `ecs` driver. **gVisor has now been run** —
  `scripts/verify_gvisor.sh` puts a real sentry under `cryptography`, `pydantic-core`,
  `psycopg`, `yaml`, `httpx` and `os.fork`, and all six work — and doing it found the
  isolation probe matching a kernel string no real sentry emits, which made it *refute*
  a genuine sandbox rather than merely fail to confirm one. What remains unproven is
  **cost, not correctness**: the sentry runs nested in a privileged container with
  `--ignore-cgroups`, and nothing has been measured on `x86_64` at all.
