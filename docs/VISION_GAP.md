# RYA — Vision vs. Built: Honest Gap & Roadmap

**Date:** 2026-06-18 · **Re-audited:** 2026-07-29
**Compares:** the RYA platform vision (positioning doc) against the actual
`rya` repository as built and tested in this codebase.

> **Why the re-audit.** Sections 1–6 were written against the pre-Phase-A/B/C
> state; the roadmap at the bottom was then updated in place as things shipped,
> and the earlier sections were never revised. The document therefore spent six
> weeks contradicting itself — marking per-user JWT identity, vector retrieval,
> Langfuse export, the SAM template and the TS client as gaps in §1/§5 and in
> "Claims to correct," while ticking each one ✅ in its own roadmap. Corrections
> are struck through rather than deleted, so the record of what was true when
> stays legible. Counts (tests, MCP tools) were re-measured against the code.

This is deliberately conservative. Where the vision describes something we have
not built, it says so plainly. "Verified" means there is a passing test or a
reproduced live run in this session, not just code that looks right.

**Legend:** ✅ built & verified · 🟡 partial · ❌ not built · 💼 business/GTM (not code)

Current test state (re-measured 2026-07-30): **63 test files, 562 test
functions — 599 collected, 560 passing, 39 skipped, 0 failing.** Skips are
Postgres-gated (`RYA_TEST_DATABASE_URL`) plus the live-provider and DeepEval
tests. The previously-recorded failure
(`test_llm_layer.py::test_governance_applies_inside_the_loop`, which needed a
model that actually emits a tool call) no longer fails.

**Run it with no provider keys in the environment.** With `ANTHROPIC_API_KEY` set,
16 tests fail and the suite takes 179s instead of 19s, because
`providers/llm.py:36-54` resolves `auto` → `anthropic` from the key's mere
*presence* — so tests written against the deterministic mock silently talk to the
live API. That is the ambient-config bug class, reproduced in our own suite.

---

## 1. The ten primitives

### Identity — 🟡 partial
- **Vision:** every agent has owner, version, permission *graph*, channels, tools,
  memories, models, approval policy; identity is the contract bound per session.
- **Built:** manifest declares `name/owner/version/tools/models/channels/approvals`
  ([manifest/schema.py](../src/rya/manifest/schema.py)); workspaces + hashed API
  keys map a caller to a tenant ([tenancy.py](../src/rya/tenancy.py)).
- **Gap:** permissions are flat per-tool *levels* (`read_only/allowed/approval_required/disabled`),
  not a graph (agent→tool→scope→user).
  ~~No per-session `Identity{...}` handshake derived from a verified user JWT —
  auth resolves a *workspace*, not a *user*.~~ **Closed** — this was written before
  Phase B/C and contradicted its own roadmap below. Per-user identity exists:
  [auth.py](../src/rya/auth.py) verifies HS256 (stdlib) and RS256/JWKS (`[auth]`
  extra); `ctx.identity` carries `sub`/`scopes` per run; `rya_runs` and
  `rya_conversations` have per-user RLS keyed on `app.user_id`
  ([tenancy.py](../src/rya/tenancy.py)); and a tool declaring `require_user` fails
  closed with `E_NO_IDENTITY` rather than falling through to a workspace-shared
  credential.
- **Takes (M–L):** a permission model richer than four enum levels. *(The
  JWT/identity half of this item is done; what remains of §3's enterprise story is
  the mutator/Cognito topology, not identity itself.)*

### Runtime — 🟡 partial
- **Vision:** long-lived Fargate fleet, durable, pause/resume, retries, timeouts,
  version migrations, horizontal scale.
- **Built:** durable runtime with **pause/resume on approval verified across
  separate processes on Postgres**; job retries with exponential backoff
  ([engine.py](../src/rya/runtime/engine.py)); runs store `agentVersion`.
- **Gap:** ~~single worker process (no fleet / horizontal claim queue); no per-run
  **timeouts**~~ **Closed** — the claim is an atomic `FOR UPDATE SKIP LOCKED`
  claim over the queue table so N `rya worker` processes pull work
  ([store_postgres.py](../src/rya/store_postgres.py)), and `timeout_seconds` is
  enforced per run, failing with `E_TIMEOUT`
  ([engine.py](../src/rya/runtime/engine.py)). ~~no in-flight **version
  migration**~~ **Closed as multi-version loading, and deliberately not as
  migration** — runs are pinned to an immutable content-hashed version and a
  worker serves exactly one, re-verifying its bundle hash before importing
  anything ([worker.py](../src/rya/worker.py)). Moving a *live* run onto a new
  version stays impossible **on purpose** (PLATFORM_DESIGN D12): replay is only
  sound against the code that wrote the journal, so a version is retained while
  any run is pinned to it and in-flight runs finish on theirs. What is genuinely
  open is the *fleet lifecycle*: **no autoscale, no start-on-demand, no
  scale-to-zero** — a worker can exit after an idle window (`--idle-exit`) and
  reports the queue depth it could claim, but nothing starts one back up or
  varies replica count.
- **Takes (L):** the §6 worker lifecycle of PLATFORM_DESIGN — a supervisor that
  starts a process per (workspace, agent, version) on queue depth and pre-warms
  promoted versions against a cold-start target.

### Manifest — ✅ built & verified
Declarative YAML, Pydantic-validated, diff-able, validated before any run/deploy.
Matches the vision. (`provider/temperature` and tool/model/channel/trigger blocks present.)

### Deployment pipeline — 🟡 partial
*(An eleventh row. Not one of the vision's ten primitives — it is how the other
ten get shipped, and it had no row at all, so the bundle/version/gate/worker work
was invisible in this ledger.)*
- **Vision:** the agent is deployed **to** the platform, not compiled into it — an
  immutable, content-hashed bundle promoted between environments, with rollback as
  a pointer flip.
- **Built:** content-hashed bundles whose digest folds in the SDK version, with
  `.ryaignore` ([bundles.py](../src/rya/bundles.py)); immutable versions,
  environments, promote/rollback/retire, and retention while any run is pinned to
  a version ([deployments.py](../src/rya/deployments.py)); readiness and eval
  **promotion gates** backed by per-version attestations
  ([gates.py](../src/rya/gates.py)); two publish paths — `rya deploy --env` for an
  operator who already has the store and the archive root, and `rya publish` over
  HTTP for a client repo that has neither
  ([cli/client.py](../src/rya/cli/client.py), `POST /agents/{id}/versions`); a
  shared archive store, either a local content-addressed directory or
  S3/MinIO/Ceph/R2; and a **version-pinned worker** that fetches, unpacks and
  re-verifies the hash before importing anything
  ([worker.py](../src/rya/worker.py)).
- **Gap:** the HTTP publish path files **no readiness attestation** — D13 forbids
  the control plane importing tenant code, so the endpoint answers
  `"attested": false, "notAttested": ["readiness"]` and a gate that requires
  readiness refuses the version. There is no way to file one out of band either:
  `gates.py` points at `rya attest readiness --version <id>`, **a command that
  does not exist** (only `rya eval --attest` does). Worker orchestration is
  manual — no start-on-demand, no scale-to-zero supervisor (see Runtime above).
  And one deployment still serves exactly **one** agent: `build_app` resolves a
  single manifest, `agent_id` in the routes is decorative (checked against
  `manifest.name` and otherwise unused), and the worker passes
  `agent_name=manifest.name`.
- **Takes:** (M) run readiness in an isolated process outside the api, or accept a
  signed client attestation; (L) the §6 worker lifecycle of PLATFORM_DESIGN;
  (L) multi-agent routing within one deployment.

### SDK — 🟡 partial
- **Built:** Python `define_agent()` + the full typed `ctx` surface
  (`llm, models, tools, memory, approvals, channels, jobs, cron, secrets, logs,
  traces, events`) — [sdk/context.py](../src/rya/sdk/context.py). ✅
- **Gap:** ~~TypeScript SDK not started.~~ **Half closed** — this contradicted
  §5 and the shipped [clients/typescript](../clients/typescript) in this same
  document. A typed TS **client** ships (`RyaClient`, strict `tsc` clean,
  runtime-proven against a live `rya serve`). There is no TS **runtime** — you
  cannot author an agent in TypeScript. Nor can you *publish* from TypeScript,
  deliberately: `client.ts` carries `listVersions`/`getVersion`/`pinnedRuns`/
  `retireVersion`/`promote`/`rollback` but **no `publish`**, because building a
  bundle means walking a project tree, honouring `.ryaignore`, and reproducing a
  content hash that folds in the *Python* SDK version — a digest a TS client
  cannot compute, and one the platform verifies by rebuilding it.
- **Takes (L):** port the SDK + a runtime to TS. Publishing from TS additionally
  needs the bundle format specified as a cross-language contract, not just
  implemented in `bundles.py`.

### Tools — 🟡 partial
- **Built:** typed registry, four permission levels, `approval_required` cannot be
  called directly, every call audited into the trace ([tools/registry.py](../src/rya/tools/registry.py)).
- **Gap:** tool *implementations* are deterministic mocks (crm.lookup/email.send/
  calendar.read). No real tool-execution path (HTTP/code tools), no in-process
  MCP dispatch, no single-purpose mutator isolation.
- **Takes (M):** a real tool executor (HTTP + Python-callable tools) with schema
  validation; (L) the mutator-isolation security model (see §3).

### Memory — 🟡 partial
- **Built:** kv, conversation/collection append, scoped, durable (file + Postgres).
- **Gap:** "vector retrieval" is a **naive substring search**, not embeddings.
  No retention policy or access-control enforcement on memory.
- **Takes (M):** pgvector + an embeddings provider (store vectors, cosine search);
  retention/ACL bound to identity.

### Approvals — ✅ built & verified
Durable pause, human resolves with full context, run resumes (replay-memoized),
complete audit. Verified over CLI, MCP, and HTTP, including across process
restarts on Postgres. Matches the vision.

### Events, jobs, schedules — 🟡 partial
- **Built:** inbound webhooks, cron triggers, delayed jobs, **retries + backoff**.
- **Gap:** no real **dead-letter queue** (exhausted jobs just become `failed`);
  cron is invocation-driven (`rya schedules run` / `jobs run --due`), not a hosted scheduler.
- **Takes (S–M):** a DLQ table + a hosted scheduler (cron → webhook POST).

### Channels — 🟡 partial
- **Built:** webhook in; **real Slack + email (Resend) + generic-webhook out**,
  mock fallback ([providers/channels.py](../src/rya/providers/channels.py));
  real outbound delivery verified against a live local HTTP server.
- **Gap:** WhatsApp; inbound channel adapters (Slack Events API, inbound email)
  beyond the raw webhook.
- **Takes (M each):** per-channel inbound adapter + signature verification.

### Model gateway — 🟡 partial
- **Built:** registry with permissions/versions; **real Anthropic + OpenAI over
  stdlib HTTP** (wiring proven via real 401s), per-call trace, per-call usage
  capture, per-workspace isolation ([providers/llm.py](../src/rya/providers/llm.py)).
- **Gap:** **custom ML-model hosting** (SageMaker-style) is mocked; no model
  **fallback/routing**; no cost accounting (token→$).
- **Takes (M):** fallback chain + cost table from captured usage; (L) real
  custom-model serving.

### Observability — 🟡 partial
- **Built:** forensic per-run trace (events, LLM/tool/model/memory/approval/channel/
  retry/status), structured JSON logs, **secret redaction vault**.
- **Gap:** no Langfuse/OTel export; no queryable trace UI; no eval harness; no
  cost/token dashboards.
- **Takes (M):** Langfuse/OTel exporter (spans already exist) + an eval runner.

---

## 2. Inside the agent runtime — 🟡 partial / ❌

- **Vision:** one container, many identity-bound sessions; edge token handshake →
  `Identity{...}`; in-process MCP for read tools (RLS Postgres, sub-ms); mutating
  tools cross to single-purpose Lambdas that re-verify the caller JWT; every
  dispatch a span.
- **Built:** one process; runs are identity-light (workspace-scoped); MCP server
  exists but as an **operator** surface, not an in-process per-session tool bus;
  spans on every `ctx` step ✅.
- **Gap:** the entire "JWT at edge → identity-bound session → RLS-on-the-DB-as-the-
  user → Lambda mutators re-validating JWT" security model.
- **Takes (XL):** see §3 — this is the enterprise security architecture.

---

## 3. Control plane / data plane — 🟡 partial (still the big one)

- **Vision:** stateless control plane (registry, manifests, permissions, secrets
  metadata, schedules) over Postgres; data plane workers scale horizontally; RLS
  on **every** table; small Redis cache; **mutating tool calls go to single-purpose
  Lambdas that re-validate the original user's JWT before any DB write** — the
  agent holds no privileged DB credentials; every read/write scoped on the DB to
  the requesting identity.
- **Built:** a FastAPI **control-plane shape** ([api/app.py](../src/rya/api/app.py));
  ~~single worker is currently *both* planes~~ **the plane split is now deployed** —
  `docker-compose.yml` pins `RYA_API_INLINE_WORKER: "0"` on the api and runs
  dedicated `worker` / `worker-pinned` services, and
  [deploy/aws/template.yaml](../deploy/aws/template.yaml) runs a separate
  `WorkerService` (its own task definition, no ALB target, no inbound port). In
  multi-tenant mode the api **refuses** to run handler code at all — the inline
  sweeper/jobs loops never start and `RYA_API_INLINE_WORKER=1` is logged and
  ignored (D13). A bare single-tenant `rya serve` keeps its inline loops on by
  choice, because there that process *is* the whole deployment. Plus **Postgres
  RLS for multi-tenant isolation** (non-superuser `rya_app` role) — verified that
  an unfiltered `SELECT *` only returns the caller's tenant; token + API-key auth.
- **Gap:** RLS is keyed to **workspace**, not to a **per-request user JWT** (the
  per-user policies on `rya_runs`/`rya_conversations` are the exception, not the
  rule). No Cognito-issued identity in the request path, no API Gateway in front,
  no Redis. The **mutator Lambda is a stub that returns 501** with
  `E_NOT_IMPLEMENTED` — it fails closed rather than answering "yes" to
  everything, but nothing may route to it. ~~no plane separation in deployment~~
  **Closed** (above).
- **Takes (XL):** carrying the Cognito identity through the request path (the pool
  and `RYA_JWKS_URL` are provisioned; what is missing is per-request user identity
  driving RLS); API Gateway + one Lambda per mutating operation that re-validates
  the JWT and runs the write as a DB role whose RLS reads the JWT claim
  (`current_setting` from the verified token); Redis read-through cache. ~~split
  control/data planes~~ — done. **Needs an AWS account and is multi-session.**
  *(The hardest, most differentiating property — and the one our current
  workspace-RLS only partially models.)*

---

## 4. Deployment model — 🟡 partial

- **Vision:** single-tenant in the customer's cloud; CloudFormation/SAM; Cognito,
  API Gateway, ECS Fargate (Graviton) behind ALB/CloudFront, RDS Postgres w/ RLS,
  ElastiCache, NAT w/ static egress, Secrets Manager, self-hosted Langfuse. Two
  commands; no shared blast radius.
- **Built:** a **self-contained agent container** (`rya deploy` generates the
  Dockerfile baking the agent in, plus `fly.toml`/`render.yaml`), state external
  via `RYA_DATABASE_URL`. **Verified end-to-end:** built the image, ran it +
  Postgres on a Docker network, `store=postgres`, signed webhook → run → token
  approve → completed, run row durable in the external Postgres.
  **The agent no longer has to be baked in.** `rya publish` uploads a
  content-hashed bundle to a *running* deployment over HTTP and the platform
  records an immutable version, so a client repo needs neither database nor bucket
  credentials. `docker-compose.yml` runs the api and the workers as separate
  containers sharing a **MinIO** archive store (`RYA_BUNDLES_S3_ENDPOINT` forces
  path-style addressing), with `worker-pinned` — behind the `pinned` profile —
  serving whatever `prod` currently points at. Verified locally; **not** verified
  on the AWS stack, which provisions a `FilesBucket` but **no bundle bucket** and
  sets no `RYA_BUNDLES_S3_*` on either task.
- **Gap:** no CloudFormation/SAM; none of the AWS topology (Cognito/API GW/Fargate/
  ElastiCache/NAT/Secrets Manager/Langfuse).
- **Takes (L–XL):** author + test the IaC in a real AWS account.

---

## 5. The developer surface — ✅ mostly true

- **CLI** — ✅ `create/dev/deploy/runs trace/approvals/...`, `--json` everywhere,
  semantic exit codes, `--non-interactive`. Verified. It is now **two surfaces**
  (PLATFORM_DESIGN D16): a thin **client CLI** ([cli/client.py](../src/rya/cli/client.py)
  — `create`, `init`, `check`, `bundle`, `publish`, `login`/`logout`/`whoami`,
  `skills`) shipped in the `rya` wheel, whose import closure is SDK-only; and the
  **operator CLI** ([cli/main.py](../src/rya/cli/main.py)) shipped in `rya-server`,
  a strict superset that additionally carries `serve`, `worker`, `dev`, `deploy`,
  `versions`, `envs`, `gate`, `quotas`, `workspaces`, `keys`, `connections`,
  `runs`, `approvals`, `secrets`, `jobs`, `schedules`. `publish` and `check` are
  *defined* in the client CLI and re-registered in the operator one, so they do not
  vanish when a developer dev-links a local checkout.
- **MCP server** — ✅ 25 `rya_*` tools incl. `rya_context`. Verified.
- **Skills** — ✅ two progressive-disclosure modules (`rya`, `rya-ops`).
- **SDK** — ✅ Python (typed); ✅ TS **client** ([clients/typescript](../clients/typescript)
  — typed `RyaClient`, strict `tsc` clean, runtime-proven against a live `rya
  serve`). There is no TS *runtime* — you cannot author an agent in TypeScript,
  and you cannot **publish** from TypeScript either: the client can list, promote,
  roll back and retire versions, but computing a bundle hash that folds in the
  Python SDK version is not something it can reproduce (see §1's SDK row).
- **REST API** — 🟡 control-plane shape exists; not a full partner API.
- **First five minutes** — ✅ `uvx rya create … → rya dev → event → trace`
  verified from the built wheel (not yet published to PyPI).

This section of the vision is the closest to reality. The main caveats: there is
a TS *client* but no TS *runtime*, REST is partial, and `uvx rya` works from the
artifact but the package isn't published.

---

## 6. Engagement model & "what we deliver" — 💼 + ❌ claim to fix

- The phased Pilot→Production→Distribution model is GTM, not code — nothing to
  verify, but note it implies booking/attribution/revenue/currency write-paths
  and customer-specific ML that **do not exist in this repo**.
- **"We have shipped this pattern in production, under real traffic and real
  liability."** This is **not true of this repository** (days old, no production
  traffic). If it refers to a *different* system (e.g. `openclaw` / the `platform`
  repo), the doc should say so; as written next to the `rya` codebase it
  overclaims.

---

## Claims to correct before this doc goes external

> **Re-audited 2026-07-29.** Five of the seven items below were written against the
> pre-Phase-A/B state and were then silently invalidated by the roadmap at the
> bottom of this same document — which was updated in place while these sections
> were not. Each is corrected against the code rather than deleted, so the record
> of what was true when stays legible. Each item now carries its own verdict.

1. ~~"Runs on a container backplane … scales horizontally on CPU."~~ **Partly
   closed** — Phase B/5 built an atomic `FOR UPDATE SKIP LOCKED` claim queue and a
   horizontally scalable `rya worker`. What remains unbuilt is the *deployed*
   backplane, not the concurrency primitive.
2. **Still true, in part.** Per-user JWT and per-user RLS *are* built (see §1 and
   Phase B/6, C/9). What is not built is the **Cognito + API-Gateway +
   per-mutator-Lambda** topology: `deploy/aws/template.yaml` declares it and is
   cfn-lint clean, but `MutatorFunction` is a **stub** whose body returns
   `{"ok": true, "pattern": "single-purpose-mutator"}`.
3. ~~"CloudFormation and SAM templates; every deploy is two commands."~~
   **Half closed** — the SAM template exists and lints (Phase C/10). It has never
   been deployed; `sam deploy` needs a billable AWS account. So: authored, not
   proven.
4. ~~"Vector retrieval." → substring search.~~ **Closed, with a caveat worth
   keeping.** `ctx.memory.search` and `ctx.knowledge.search` embed and rank by
   cosine over real vectors ([providers/embeddings.py](../src/rya/providers/embeddings.py)).
   The caveat: the *offline default* is a deterministic hashing vectorizer, so
   cosine there reflects **lexical overlap, not semantics**; true semantic
   retrieval needs `OPENAI_API_KEY` (`text-embedding-3-small`). There is also a
   lexical fallback scoring 0.1 when vectors are absent or length-mismatched.
5. ~~"Self-hosted Langfuse for traces." → in-store traces only.~~ **Closed** —
   Phase A/2 shipped a Langfuse + generic-webhook exporter
   ([observability/export.py](../src/rya/observability/export.py)), verified against
   a live local server.
6. **Still true.** "Shipped … in production, under real traffic" is not true of
   this repository.
7. **Still true.** The five `./diagrams/*.png` are referenced but absent.

None of this means the vision is wrong — it means the doc currently mixes *built*
and *intended* without a line between them. Drawing that line (or splitting into
"Platform (today)" vs "Reference architecture (target)") makes it honest.

---

## Suggested roadmap (rough sizing)

**Phase A — close the cheap, real gaps:**
1. ✅ Real vector memory — embeddings provider (OpenAI + deterministic mock) +
   cosine ranking in `ctx.memory.search` ([providers/embeddings.py](../src/rya/providers/embeddings.py)).
   *(pgvector index is a later scaling step; retrieval itself is real now.)*
2. ✅ Trace export — Langfuse + generic-webhook exporter, fired on terminal runs
   ([observability/export.py](../src/rya/observability/export.py)); verified a real
   trace POST to a live local server.
3. ✅ Dead-letter queue (`rya jobs dlq` / `rya jobs retry`), model **fallback**
   (`model.fallback` on provider error), and **token/cost accounting**
   ([observability/usage.py](../src/rya/observability/usage.py); cost only when
   `RYA_PRICE_<MODEL>_IN/_OUT` is configured — no hard-coded prices).
4. ⏳ Publish to PyPI so `uvx rya` resolves. **Needs your go-ahead** —
   outward/irreversible. Wheel is built and `uvx`-proven; one `uv publish`.

**Phase B — make it a real platform** (first cuts built 2026-06-18):
5. ✅ Atomic job **claim queue** (Postgres `FOR UPDATE SKIP LOCKED`), horizontal
   `rya worker`, and per-run **timeouts** (`timeout_seconds`, → `E_TIMEOUT`).
6. ✅ Per-user **JWT identity** ([auth.py](../src/rya/auth.py)): HS256 (stdlib) +
   RS256/JWKS (optional [auth] extra); `ctx.identity`; per-user memory scope; the
   server enforces JWT when configured. *(Rich permission graph still flat levels.)*
7. ✅ Real tool execution — `@agent.tool` async handlers + HTTP tools
   (manifest `url:`), dispatched ahead of the mock registry; plus a real
   **Slack inbound adapter** (`/slack/events`, signature-verified, handshake + events).
8. ✅ **TypeScript client SDK** ([clients/typescript](../clients/typescript)) —
   typed `RyaClient`, strict `tsc` clean, **runtime-proven driving a live Python
   `rya serve`** (trigger → approve → completed).

   *First cuts: single-tenant JWT (not yet Cognito/per-mutator-Lambda), one inbound
   channel (Slack), client SDK (not a TS runtime). The enterprise versions are Phase C.*

**Phase C — the enterprise architecture (needs AWS to deploy):**
9. 🟡 **Per-user RLS built + verified** — `rya_runs` carries `owner`; RLS scopes
   a user to their own runs within a workspace via `app.user_id` (set from the
   verified JWT); proven on real Postgres that an unfiltered `SELECT *` only
   returns the caller's rows ([test_tenancy.py](../tests/test_tenancy.py)). Cognito +
   API Gateway + the mutator Lambda are **defined in IaC** (below) but the
   in-runtime mutator wiring is still single-process. XL remainder.
10. ✅ **CloudFormation/SAM single-tenant template authored + cfn-lint clean**
    ([deploy/aws/template.yaml](../deploy/aws/template.yaml)): Cognito, ECS Fargate
    + ALB, RDS Postgres, ElastiCache, Secrets Manager, single-purpose mutator
    Lambda + Cognito-JWT HTTP API. **Not deployed** — `sam deploy` needs your AWS
    account (billable). Langfuse export already exists (Phase A).
11. 🟡 Control/data-plane: `rya serve` (control+data) and `rya worker` (data) are
    separately deployable today; the IaC runs the runtime as a Fargate service.
    A hard plane split (separate services) is a deploy-topology change.

**Sizing key:** S <1 day · M a few days · L 1–2 weeks · XL weeks+. Phase C
requires a real cloud account and is where most of the "production, real traffic"
hardening actually lives.

---

## One-paragraph honest summary

What exists is a **genuinely working agent runtime and an excellent coding-agent
developer surface**: manifest, SDK, durable approvals, retries, real LLM and
channel seams, Postgres persistence with multi-tenant RLS, a token-authed
signed-webhook server, and a container deploy proven against external Postgres —
all test-covered. What the vision adds on top is the **enterprise security and
cloud-deployment architecture** and the **production track record**.

*Updated 2026-07-29:* that enterprise list has thinned since this paragraph was
written. **Per-user JWT identity and RLS-as-the-user are built and verified**
(auth.py, `ctx.identity`, `app.user_id` policies on `rya_runs`/`rya_conversations`),
and the **CloudFormation single-tenant template is authored and cfn-lint clean**.
What genuinely remains unbuilt is narrower: **Lambda-isolated mutators** (declared
in IaC, but `MutatorFunction` is a stub), an **actual cloud deployment** (`sam
deploy` needs a billable account), and the **production track record**. The
developer-surface half of the doc is real; the enterprise half is now roughly half
real and half roadmap.
