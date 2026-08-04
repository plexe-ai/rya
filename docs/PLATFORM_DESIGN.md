# Rya as a Platform — High-Level Design

**Status:** proposal / RFC · **Date:** 2026-07-29 · **Scope:** architecture only, no implementation

Rya today is a *library you build an agent inside of*. This document designs the
shift to Rya as a *platform your agent runs on*: the platform ships as one
deployment, the client's agent ships as a versioned bundle that loads into it,
and the two move on independent release trains.

An earlier draft designed a distributed platform — a control plane in our cloud,
workers dialing out from customer networks, and a versioned wire protocol between
them. **That premise was wrong.** The two halves are always co-located. The
distributed design and the ~36-operation protocol it required are superseded;
§13 records what was dropped and why. The full prior draft is preserved in
`PLATFORM_DESIGN.superseded.md`.

---

## 1. Where we are today

```
chatstudyabroad/rya-agent/            rya (the whole product)
  pyproject.toml ──── git pin ──────► plexe-ai/rya @ track-a-core
  rya.agent.yaml                        src/rya/{sdk,runtime,api,tools,...}
  src/agent.py                          `rya serve` = API + console + engine + MCP
  Dockerfile  ──► uv sync --extra deploy ──► CMD rya serve
```

The agent is not *deployed to* Rya; it is *compiled into* a Rya. The image
contains the runtime and the client code, and `runtime.load_agent()`
(`runtime/engine.py:70-91`) imports the entrypoint from the local filesystem into
the server process.

| Symptom | Root cause |
|---|---|
| `rya = { git = ..., branch = "track-a-core" }` | no released client contract; the client pins the *whole product* |
| A runtime fix requires rebuilding and redeploying every client image | runtime and client share a build artifact |
| One `rya serve` = one agent project | the process *is* the deployment unit |
| Client code runs in the API process | no isolation boundary between tenants |
| A run cannot be resumed by a differently-versioned process | code version is a property of the image, not of the run |

What already exists and should not be rebuilt: durable runs with journal and
replay-based pause/resume (`runtime/engine.py`, `sdk/context.py`), a lease-based
retrying job queue with DLQ (`queue.py`), durable resumable turn streams
(`turns.py`), multi-tenancy with workspaces and Postgres RLS (`tenancy.py`,
`store_postgres.py`), governance — permission tiers, kill switches, server-side
arg pins, scoped credentials, egress firewall, grounding gate (`guard.py`) —
secret sealing (`seal.py`), and traces/usage/Langfuse export.

**The gap is the deployment pipeline and the client contract, not the runtime.**
The whole codebase is 12,970 lines across 53 files; the missing piece is not
large, but it is entirely greenfield.

---

## 2. Target model

**One deployment, everywhere.** The platform ships and runs as a unit — in our
cloud, a customer's cloud, or on a laptop. Same artifact, same topology, no
exceptions.

**Control plane and execution plane are responsibilities, not deployables.** The
split between what *decides and remembers* and what *executes* is the heart of
this design (§7). What changes from the earlier draft is that the two are always
co-located in one deployment, so the boundary between them is a code boundary
rather than a network one.

Concretely the platform runs as **two processes**, both platform code, both
against the same Postgres — `api` and `worker`, the names the CLI already uses.

```
ONE DEPLOYMENT  (our infra · customer infra · a laptop)

  api              REST/WS/SSE · auth · scheduler · policy · guard · vault ·
  (control plane)  LLM gateway · approvals · console · MCP
                        │
                        │  enqueue
                        ▼
                   Postgres  ── rya_queue · runs/journal · memory · tenancy (RLS)
                        ▲
                        │  claim / lease / heartbeat / complete   (queue.py)
                        │
  worker           one process per (workspace, agent, version).
  (execution plane │  Platform code. Holds ctx, the journal, replay, and a
   instance)       │  tenant-scoped DB session.
                   └── CLIENT BUNDLE  on_event / job / cron + @agent.tool bodies
                                      client-versioned; loaded into the worker
```

The `api` process **is** the control plane. A `worker` process is one
execution-plane instance — and because it also runs platform code, it carries the
parts of the control plane that must be local to the run: the journal, replay,
and `ctx`.

**This shape already exists in the repo.** `docker-compose.yml:104-136` is the
api (the `rya` service — image `CMD` is `rya serve`, with
`RYA_API_INLINE_WORKER: "0"` so it executes no handler code), `:146-158` is the
queue-consuming worker (`rya worker --interval 2 --concurrency 4`, scaled by
raising its replica count), `:179-192` is `worker-pinned` (profile `pinned`, `rya worker --env
prod` — one content-hashed bundle per process, so a re-publish needs a restart),
and `:82-102` is `minio`, the archive store both sides share. The change is not a
new architecture — it is `rya worker` becoming per-tenant and loading a
*versioned bundle* instead of a mounted directory.

A worker that loads a versioned bundle **now exists**: `--version` / `--env` pins
it, and `worker.resolve_bundle_root` fetches the archive, unpacks it into a
content-addressed cache and re-verifies the hash before importing anything. What
is still missing is the *scheduling* of those processes — one per (workspace,
agent, version), started and stopped on demand rather than declared in a compose
file or an ECS `DesiredCount`. See §6.

**These are not microservices, and the distinction is load-bearing.** They share
one database, there is no service-to-service call between them (they coordinate
through a queue table, D4), and they ship and version together as one platform
artifact. The accurate pattern is `web` + `worker` — one deployable, two run
modes, as in Rails + Sidekiq or a Procfile. Calling them microservices invites
an HTTP API between them, separate datastores, independent versioning and service
discovery: every one of those is work D1, D4 and D5 exist to avoid.

One way this differs from the classic `web`/`worker` split: `worker` is not a
homogeneous fleet where any process runs any job. It is a **process template
instantiated per (workspace, agent, version)** — which is what carries tenant
isolation (D13) and version pinning (D12), and why §6 exists.

Three nouns:

- **Deployment (of an agent)** — an immutable, content-hashed bundle: source +
  `rya.agent.yaml` + lockfile + SDK version.
- **Environment** — `dev` / `staging` / `prod` within a project; holds one
  *current* bundle version plus any older versions still pinned by live runs.
- **Worker process** — where a bundle executes, one per (workspace, agent,
  version).

The contract: **a client repo needs `rya-sdk` and a deploy token. It never
imports the runtime, never runs a server, and never knows which deployment it is
running in.**

---

## 3. The one boundary

The split is **platform code vs. client code** — not api vs. worker. The
codebase already draws this line and we adopt it as-is.

`sdk/agent.py:56-59` states that a leaf tool handler "may do real IO (HTTP, DB,
read env for secrets) but must not call journaled `ctx` operations," and
`runtime/engine.py:451-453` invokes it as `fn(input)` — one argument, no `ctx`.

| | Owned by | Gets `ctx`? |
|---|---|---|
| Engine, journal, replay, policy, guard, vault, LLM gateway, store | **platform** | owns it |
| `on_event` / `job` / `cron` bodies | **client** | yes |
| `@agent.tool` bodies | **client** | no — `fn(input)` |

Two properties follow, and they are the ones that matter:

1. **No client-versioned code holds a store handle or makes a policy decision.**
   The process that touches state is platform code, at the platform's version,
   with the RLS session pinned to one workspace. Governance cannot be forked,
   lagged, or pinned by a client.
2. **The runtime and the bundle version independently.** The runtime is upgraded
   by deploying a new platform image; the bundle by `rya deploy`. This is what
   removes the git pin.

Strictly better than today, where `ctx` **is** the policy engine, in-process, at
whatever version the client happened to pin.

---

## 4. Decisions

| # | Decision | Why |
|---|---|---|
| **D1** | **Control plane and execution plane are always co-located** — one deployment, one topology, in our infra, a customer's, or a laptop | removes the entire distributed-systems surface: no wire protocol, no pool kinds, no dial-out, no firewall traversal, no journal snapshots on a wire, no cross-network latency budget |
| **D2** | **The boundary is platform code vs. client code**, drawn at the leaf-tool rule already enforced in `sdk/agent.py:56-59` | adopts a line the codebase already has instead of inventing one |
| **D3** | **The worker is platform-versioned, one process per (workspace, agent, version); the bundle is client-versioned and loads into it** | solves §1's git pin. Per-*version* because a version-pinned run needs a process on that version, and one process cannot hold two — `load_agent` mutates `sys.path` and never unloads (`runtime/engine.py:79-84`) |
| **D4** | **The queue is the dispatch mechanism. A worker is a queue consumer.** No api→worker interface exists | `queue.py` already provides idempotent enqueue, claim over `FOR UPDATE SKIP LOCKED`, lease, heartbeat with `cancelRequested`, holder verification, backoff, DLQ and `concurrency_key`/`concurrency_limit`. Backpressure is a concurrency limit; approval resume is a re-enqueue. Nothing new to build, nothing new to version |
| **D5** | **No wire protocol in v1** | with D1 and D4 there is nothing to carry. Deletes the protocol schema, transport choice, protocol semver and cross-SDK conformance suite — which the prior draft correctly called the single largest ongoing cost of the split |
| **D6** | **All streaming goes through the durable turn buffer.** No in-process callbacks survive | `on_token`/`on_trace`/`on_ui` are raw Python closures today (`api/app.py:510-523`), handed into the provider's SSE parser (`providers/llm.py:226`). The api process holds the browser socket; the worker executes the handler — so they no longer share a process. `turns.py:59-66` already collapses all three into `store.stream_append`, and `api/app.py:668-715` already tails it by sequence with `Last-Event-ID` resume |
| **D7** | **Governance decisions are platform-side, always** — permissions, pins, kill switches, scoped credentials, guard verdicts, grounding, approvals | now enforced by *where the code lives* rather than by an RPC boundary: cheaper, equally binding |
| **D8** | **Run inputs are declared, not ambient** — config, secrets and connections come from per-environment platform state | kills a live bug class: 86 `os.environ` reads across 20 files, 22 in `providers/llm.py` alone, called from inside `ctx.llm.respond`/`run` and bypassing `ctx._env` entirely. `providers/llm.py:50-54` silently resolves to the mock provider when no key is present |
| **D9** | **Journal steps are content-keyed, and replay fails closed on drift** | `sdk/context.py:217-219` matches on a bare ordinal and compares nothing — not the input hash, not even `kind`. Safe only while code and journal ship together, which D3 ends |
| **D10** | **The journal becomes an append-only table; billable facts are journaled, not traced** | `store_postgres.py:262-276` rewrites the entire run as one JSONB blob per step, and there is no `rya_journal` table among the 11 that exist. A commit path needs an append; billing needs a ledger; `observability/usage.py:31` derives money from `run["trace"]` and `:27` falls back to `os.environ` for prices |
| **D11** | **One environment-invariant manifest per agent; `environment:` is deleted** | §9 promotes one content-hashed bundle *between* environments, which an environment-specific manifest makes impossible. The field is inert today and actively misleading — a production container declares itself `local` and nothing notices |
| **D12** | **Deployments are immutable, content-hashed, and pinned per run**; a run whose version was deleted fails closed with a stable `E_*` code | replay is only sound against the code that wrote the journal. `agentVersion` exists (`runtime/engine.py:146`) but is just the author-typed `manifest.version` string — no hash, no immutability, no uniqueness |
| **D13** | **Multi-tenant deployment; process isolation per tenant. Node-level isolation is an accepted residual: the threat model is a buggy tenant, not a hostile one** | separate processes plus RLS contain a misbehaving agent. They do not contain a kernel-level escape. Stated as an accepted limit rather than implied to be solved — see §8 |
| **D14** | **Two named product surfaces: the agent platform and the durable job API.** `/queue/*` stays SDK-free for foreign code. **A queue job is not a governed run** | RWAP's workers run TypeScript DAGs with no `RuntimeContext`, no journal and nothing to authorize. Merging them would push a live integration toward its trigger.dev alternative |
| **D15** | **Apache-2.0 for both halves**, copyright kept consolidated | matches Temporal, Prefect and Trigger.dev; the moat is the operated service, and a permissive licence keeps the self-hosted tier free of a procurement conversation |
| **D16** | **`rya` on PyPI stays the client SDK; the platform ships as `rya-server`** | `uvx rya create` survives verbatim. Operators deploy an image, so the platform's PyPI name is near-cosmetic. Reserve both names before the split lands |

**One decision deliberately left open — see §12.**

---

## 5. The two process roles

### 5.1 `api` — the control plane

| Service | Responsibility | Today |
|---|---|---|
| API gateway | REST/WS/SSE, auth, tenant resolution, rate limits | `api/app.py`, `tenancy.py` |
| Run service | run lifecycle, approvals, enqueue | `runtime/engine.py`, `approvals/` |
| Scheduler | cron, delayed jobs, lease reaping, turn reclaim | engine cron + sweeper |
| LLM gateway | all model calls; routes, fallbacks, streaming, token and cost metering | `providers/`, `observability/usage` |
| Policy service | permission resolution, kill switches, arg pinning, scoped-credential authorization | extract from `sdk/context.py`; kill switches need carving out of `load_memory("_runtime_config")` (`sdk/context.py:406`), an ordinary memory scope with no schema distinction from user data |
| Guard | egress allowlist, grounding gate, id-secrecy scrub | `guard.py` — a rewrite: policy loads from `cwd`/`RYA_GUARD_PATH` by file mtime (`:40-44`, `:52-61`) |
| Vault | connection secrets, per-user credentials, envelope encryption | `seal.py`, `ctx.connections` |
| Stream service | durable turn buffers, fan-out, resume by `Last-Event-ID` | `turns.py`, `api/app.py:668-715` |
| Build service | bundle → version record → runnable artifact | `bundles.py` + `POST /agents/{id}/versions` (`api/app.py`), driven by `rya publish`. Verify-rebuild-record-promote, with no import of the bundle — so readiness stays unattested on this path (see §9) |
| Console / MCP | operator UI, remote MCP | `console/`, `mcp/` |

There is **no dispatcher**. The api process enqueues; workers claim (D4).

Note the api process **must stop executing handler code**: `_sweeper_loop` and
`_jobs_loop` (`api/app.py:281-292`, `:315-326`) currently iterate
`tenancy.list_workspaces()` and build an engine per workspace, so the API process
runs every tenant's code today. Severing this is a precondition for D13.

### 5.2 `worker` — one execution-plane instance

Platform code, one process per (workspace, agent, version). It:

- loads a pinned bundle version and reports its content hash,
- owns `ctx`, the journal, and replay,
- holds a **tenant-scoped** DB session — RLS pinned to one workspace, never a
  general handle,
- claims work from the queue, heartbeats its lease, and reports completion,
- executes `on_event` / `job` / `cron` bodies and `@agent.tool` bodies,
- emits every frame to the durable turn buffer rather than to a callback (D6),
- refuses to start if its registered handler set does not cover the tools its
  manifest version declares, so "the image is missing a handler" surfaces at
  startup rather than mid-run.

Because it is platform code, `ctx` stays a **local API**. All 36 journaled
operations (`sdk/context.py` routes 36 `_step`/`_astep` call sites through 36
distinct kinds, a clean 1:1) remain in-process function calls. No prefetch, no
batching, no head-of-line blocking, no snapshot shipping, and the sync surfaces
(`ctx.logs.*`, `ctx.emit_ui`, `ctx.traces.event`, `ctx.guard.*`) keep their
signatures.

### 5.3 State

Postgres stays primary — runs, journal, queue, memory, tenancy, RLS. Object store
for bundles, large payloads and file artifacts — **two arms, not one**. Bundle
archives have their own (`bundles.py`'s `BundleStore`: either a local
content-addressed directory or S3, resolved once and shared by the publishing api
and the reading worker, since separate containers cannot share a container-local
`.rya/bundles`); file artifacts keep `files_s3.py`. Both now honour an endpoint
override — `RYA_BUNDLES_S3_ENDPOINT` and `RYA_FILES_S3_ENDPOINT` — which also
forces **path-style addressing**, because MinIO, Ceph and R2 do not serve
virtual-host buckets and botocore exposes no environment variable for the
addressing style. The
`open_store()` seam (`store.py:36-58`) survives *inside the platform*: hermetic
tests select `FileStore`, deployments select Postgres.

---

## 6. Worker lifecycle

The topology says what runs; this says when. Idle cost is the constraint — the
hosted product has N workspaces × M agents × V live versions.

- **Start.** A worker is started when its (workspace, agent, version)
  has queued work and no healthy process is claiming it, or when a version is
  promoted and pre-warmed. It reports its bundle hash and handler set at start
  and fails closed on a manifest mismatch.
- **Scale.** Horizontally by queue depth for that key. Concurrency is bounded by
  `concurrency_key`/`concurrency_limit`, which is also the fairness primitive —
  one workspace must not starve another.
- **Scale to zero.** A process with no claimed work and an empty queue for its
  key exits after an idle window. Cold start is then on the critical path for the
  next run, so **cold-start time is a tracked number with a target**; pre-warming
  the current version of each production environment is the mitigation.
- **Death mid-run.** The lease expires (`queue.py:41`, 60s default) and the run is
  reclaimed and re-queued. Replay from the journal makes this safe — which is
  exactly why D9's drift detection is load-bearing rather than cosmetic.
- **Version retirement.** A version's processes may scale to zero but the version
  is retained while any run is pinned to it (D12). Retiring a version with live
  runs fails closed.
- **Cancellation and kill switches** propagate through the existing
  `cancelRequested` flag on heartbeat — cooperative, bounded by the heartbeat
  interval, with no new mechanism.

**What exists and what does not.** Worker registration, capability advertisement
and bundle digests all exist now — `worker.py` registers a process against its
`(workspace, agent, version)` key with its handler set, heartbeats, deregisters
with a reason, and is listable over `GET /workers`; the bare `worker_id` that
`queue.claim` took is now a minted, registered identity; `preflight` fails closed
on a handler-set hole or a version/manifest disagreement; a process reports its
cold start against `COLD_START_TARGET_MS`, and `--idle-exit` makes it leave when
its own claimable queue depth is zero.

~~What remains greenfield is **scheduling**~~ — **built in Phase 3 of
[MULTITENANT_PLAN](MULTITENANT_PLAN.md)** (D25/D26). `rya supervisor` watches
claimable depth per key and the worker registry, and starts, scales, pre-warms and
reaps through a pluggable `ExecutionDriver`, so **scale-to-zero is two-way**: a key
that idled out comes back when work arrives instead of staying unserved until
someone notices. `maxWorkers` is enforced when scheduling rather than only at
registration, which is the difference between a cap and a receipt.

Two things that paragraph assumed have also changed. `lastHeartbeatAt` used to be
written and never read, so a SIGKILLed worker stayed `alive` forever — a signal no
scheduler could act on, and one that leaked a `maxWorkers` slot per crash; liveness
is now derived from heartbeat age (`store.worker_liveness`). And
`COLD_START_TARGET_MS` is no longer one global number: it is a per-driver property,
because a Fargate task is tens of seconds and a warm Kubernetes pool is hundreds of
milliseconds.

What the supervisor deliberately does **not** settle is fairness *within* one
tenant, which only arises once D27's claimer scope widens past one agent-version.

---

## 7. Division of responsibility

The rule: **the platform decides and remembers; the bundle supplies behaviour.**

| Concern | Platform | Client bundle |
|---|---|---|
| Run lifecycle, journal, replay | owns | — |
| `on_event` / `job` / `cron` bodies | invokes | **provides** |
| `@agent.tool` bodies | invokes as `fn(input)` | **provides** |
| Tool permission, kill switches | resolves, fail-closed | — |
| Arg pinning | resolves from trusted state, overwrites caller args | receives pinned input |
| Scoped credential authorization | intersects tool ∩ connection ∩ user scopes | — |
| Model calls, routes, meters | performs all | requests via `ctx.llm` |
| Memory / knowledge / sessions | owns | requests via `ctx` |
| Approvals | records the pause, resolves, re-enqueues | raises the pause |
| Guard — egress, grounding, scrub | decides and enforces | subject to it |
| Secrets, vault, redaction | holds, redacts before persisting | never holds long-lived platform-issued credentials |
| Per-environment config | owns, versions, delivers per run | receives it; never reads ambient env |
| Deployment versions, rollout | builds, pins, promotes, retains | declares its version |
| Queueing, concurrency, retries | enqueues and bounds | claims and executes |
| Identity (JWT verify, RLS scoping) | verifies, scopes, restores on replay | — |
| Frames to end users | sole path, via the turn buffer | emits into the buffer |

**One row was not true of today's code; it has since been closed:**

- ~~**There are two tool-execution paths and only one is governed.**~~ **Closed.**
  `Engine._execute_action` used to re-implement dispatch with no permission check,
  no arg pins and no `guard.scrub`, and with a credential lookup that omitted
  `owner` so it was not per-user scoped. It now delegates to
  `ctx.tools.call_approved` / `ctx.channels.send_approved`, which route through
  `_Tools.prepare(..., approved=True)` — the same permission resolution (a kill
  switch flipped while the approval was pending still wins), the same pins, the
  same scope intersection, the same scrub. `prepare()` refuses an
  `approval_required` tool with `E_TOOL_PERMISSION_DENIED` unless `approved=True`,
  and `ctx.tools.call` never passes it — so `README.md` and `docs/DEEP_DIVE.md` no
  longer overclaim. §11.1 was the work; it is done.

**One row is still not true of today's code:**

- **`guard.scrub` runs inside the journaled closure** (`sdk/context.py`), so
  "execute" and "scrub before commit" are the same expression. There is no commit
  seam to hook.

### 7.1 Credentials

Three layers, in order of preference:

1. **Declared `url:` tools.** The platform performs the call and attaches the
   bearer after `check_egress`; the credential never reaches handler code. The
   strict form, preferred wherever a tool is a pure HTTP call.
2. **Scoped hand-off.** `ctx.connections.secret()` (`sdk/context.py:1368-1404`)
   returns the raw bearer to handler code, enforcing scope intersection and
   seeding the redaction vault, deliberately not journaled so a replay re-resolves
   live. Under D1 this moves between platform and client code inside one process
   rather than across a trust boundary, so it stays legal — but leaf handlers get
   no `ctx`, so the SDK needs a way to hand a leaf its credential without making
   it ambient for the whole turn.
3. **Nothing else.** No long-lived platform-issued credential is written into a
   bundle or a bundle's environment.

---

## 8. Security and multi-tenancy

- **Tenant isolation** — workspace-scoped API keys and Postgres RLS remain the
  boundary. `tenancy.py` is complete and directly reusable: a non-superuser
  `rya_app` role (`:124`), `FORCE ROW LEVEL SECURITY` (`:132-133`), and per-user
  policies on runs, sessions and connections (`:148-171`).
- **Process isolation** — one worker per (workspace, agent, version),
  with resource limits. The api process never executes client code.
- **Node isolation is an accepted residual (D13).** Workers share a
  kernel. This design contains a **buggy** tenant — a runaway loop, a memory leak,
  a crash — and does **not** contain a **hostile** one. Per-tenant node pools or
  gVisor/Kata are the answer if that threat model changes; deliberately not in v1.

  **Superseded for one posture, and only one.** MULTITENANT_DESIGN D17 changes the
  threat model to a hostile tenant, and Phase 4 built three of the four boundaries that
  answer it: no credentials in the tenant process (D18), a gVisor sandbox (D23), and
  egress enforced by the network (D24). Phase 5 added the fourth — where the broker runs
  (D32) — and found no shipped driver satisfied it; Phase 6 built the template host that
  closes it, so both container drivers now launch the pair and the posture is
  launchable. But that posture is **declared, not
  default**: without `RYA_UNTRUSTED_TENANTS=1` a deployment runs exactly what this
  bullet describes, and every self-host does. So both statements are live at once and
  the distinction is not rhetorical — `rya posture` prints which one a given
  deployment is in, and `require_untrusted_posture` refuses to start a deployment that
  claims the stronger one without backing it.
- **Config is state, not ambience (D8)** — per-environment values are stored,
  versioned, access-controlled and audited, then delivered per run.
- **Privileged writes** — the commit path connects as a distinct write-privileged
  Postgres role, separate from the read path, so the runtime cannot perform
  privileged writes even if its policy code is wrong. This requires D10's schema
  decomposition to mean anything, since today every write targets the same
  `rya_runs.data` column.
- **Supply chain** — signed bundles, pinned lockfiles, provenance on the version.

**Two things this design does not claim:**

- **A queue job is not a governed run (D14).** It gets leases, retries,
  dead-lettering and crash reclaim. It does not get permission resolution, pin
  resolution, a guard verdict or an approval gate.
- **A worker is not a security sandbox.** See D13 — and note that since Phase 4 the
  worker is not where tenant code runs at all in the untrusted posture: it claims, and
  a sandboxed fork executes. The *worker* is still not a sandbox; the thing beside it
  is.

**One thing it now does claim, which the prior draft denied:** because D1 makes
the whole platform one deployment, **self-hosting is a residency control.** A
customer running the deployment in Frankfurt keeps the journal, memory,
conversation history and sealed credentials in Frankfurt.

---

## 9. Deployment lifecycle

Two paths run the same pipeline, and which one produced a version is visible in
the ledger:

```
rya deploy --env <name>            (operator; has the store and the archive root)
  ├─ validate manifest + readiness gate LOCALLY, hard gate on the way in
  ├─ bundle: source + lockfile + manifest + SDK version
  ├─ record an immutable, content-hashed version
  ├─ attest readiness against that version id  (gates.attest_readiness)
  ├─ check the promotion gate, then promote
  └─ roll out: start workers on the new version, drain old,
               keep versions alive while runs are pinned to them
               (retention is enforced; STARTING the workers is still manual — §6)

rya publish [--env <name>]         (client repo; no database, no bucket)
  ├─ validate manifest + handler set locally (`rya check`-level only)
  ├─ bundle + pack, POST the archive with ?hash=
  ├─ platform REBUILDS the hash from the received bytes, refuses a mismatch
  │  (E_BUNDLE_MISMATCH) — the content proves the version, not the sidecar
  ├─ record an immutable version; NO readiness attestation is filed
  ├─ check the promotion gate, then promote
  └─ response says so: "attested": false, "notAttested": ["readiness"]
```

Deploys are **atomic per environment** — the current-version pointer flips once,
new runs go to the new version, in-flight runs finish on theirs. **Rollback is a
pointer flip.** **Evals** (`rya.evals.yaml`) can gate promotion between staging
and prod (`rya eval --attest`).

**The readiness *gate* is server-side; the readiness *evidence* is not.** These
are worth separating, because only the first half is done. `gates.py` resolves a
per-environment policy and reads **per-version attestations** — a promotion into
an environment whose gate requires readiness fails closed, server-side, on stored
evidence bound to a bundle hash. That is a real admission check, not a courtesy.
But nothing on the platform ever *produces* that evidence: `check_readiness`
needs a loaded agent, so the only caller that files an attestation is
`rya deploy --env`, which runs the check **locally** in the operator's process
(`cli/main.py`). The HTTP path cannot, because D13 forbids the control plane
importing tenant code, and it says so rather than implying otherwise.

So **which path produced a version matters**, and the ledger is the only place
that records it (`metadata.publishedVia: "http"`). A `prod` gate that requires
readiness is, today, a gate that requires `rya deploy --env` — `rya publish`
cannot satisfy it at all.

Closing this means one of two things, and **neither exists**: run readiness in an
**isolated process** outside the api (a sandboxed subprocess or a short-lived
worker that imports the bundle and reports a signed result), or accept a **signed
client attestation** and downgrade the gate's meaning from "the platform checked"
to "a key we trust says it checked". There is also no out-of-band escape hatch:
`gates.py`'s own failure hint points at `rya attest readiness --version <id>`, a
command that **has never been implemented**.

---

## 10. Developer experience

`rya dev` starts the same deployment locally — an api process and one worker,
mock model route, no keys — and loads the working tree as the bundle.
Real journal, real approvals, real permission and pin resolution, real guard: the
same code that runs in production. `rya dev --check` preserves today's instant
manifest validation (`cli/main.py:244-270`), which CI and tight edit loops depend
on.

Two processes on a laptop is a small cost accepted for topology parity: the local
shape is the production shape, so `rya dev` exercises the queue hand-off and the
turn buffer rather than a simplified path that only works locally.

`rya deploy` targets staging or prod. Identical code path; the only difference is
which deployment the bundle lands in.

**Mock models become configuration, not a fallback.** Today `providers/llm.py:50-54`
resolves to the mock provider from the *absence* of an API key — the same code
silently talks to a different world depending on ambient environment. Under D8 it
becomes an explicit per-environment model route.

Coding-agent ergonomics (CLI `--json`, MCP, skills) carry over unchanged; the MCP
server points at an environment rather than a directory.

---

## 11. What must be built

Ordered so each item ships independently and the early ones are worth doing even
if the rest never happens.

1. ✅ **Close the ungoverned approval path.** `Engine._execute_action` now routes
   through `ctx.tools.call_approved` / `ctx.channels.send_approved`, i.e. the same
   `prepare()` policy resolution as `ctx.tools.call`. Security fix; the README's
   overclaim is no longer one (§7).
2. **Carve kill-switch state** out of the generic `_runtime_config` memory scope
   into privileged storage the bundle cannot write.
3. **Content-key journal steps (D9)** and fail closed on drift.
4. **Append-only journal table (D10)**, plus a durable meter for billable facts.
5. **Land D8** at its real scope — 86 `os.environ` reads across 20 files,
   including the model-call path that bypasses `ctx._env` entirely.
6. **Rebuild `/ws` and `/events/stream` on the durable turn buffer (D6).** Both
   pass raw Python closures into the engine today (`api/app.py:510-523`, `:584-592`)
   and cannot survive the role split. `turns.py`'s chat-turn path already does it
   correctly; these two endpoints get re-pointed at the same buffer.
7. **Sever handler execution from the api process.** `_sweeper_loop` and `_jobs_loop`
   (`api/app.py:281-292`, `:315-326`) run every tenant's code in the API process.
   Required before D13 means anything.
8. ✅ **Worker + bundle loading** — `worker.py`. `rya worker --version <id>` or
   `--env <name>` resolves a pinned version, fetches the archive from the local
   directory or the object store, unpacks it into a content-addressed cache and
   **re-verifies the hash before importing anything** — on a cache hit too, since
   the unpacked tree is a mutable directory. `check_handler_set` refuses to start
   when the manifest declares a tool the bundle cannot serve
   (`E_HANDLER_SET_INCOMPLETE`), and preflight fails closed if the version record
   and the loaded manifest name different agents (`E_BUNDLE_MISMATCH`). Workers
   register, heartbeat with stats, deregister with a reason, and are listable over
   `GET /workers`; claiming is version-pinned, so a turn pinned to another version
   is not this worker's work. The legacy working-tree mode is retained for `rya
   dev` and single-tenant `rya serve`.
9. **Worker lifecycle (§6)** — start on demand, scale on queue depth, scale to
   zero with a cold-start target, reclaim on lease expiry, retain pinned versions.
10. 🟡 **Deployment pipeline (D11, D12)** — built: content-hashed bundles with an
    SDK-version-folded digest and `.ryaignore` (`bundles.py`), immutable versions,
    environments, promote, rollback, retire, version-pinned claiming, and retention
    while runs are pinned (`deployments.py`), plus promotion gates on per-version
    attestations (`gates.py`) — over **both** publish paths, `rya deploy --env`
    locally and `rya publish` over HTTP. Remaining: **server-side readiness
    evidence** (§9 — the gate is server-side, the evidence is not, and
    `rya attest readiness` does not exist) and the §6 lifecycle, which is item 9.
11. ✅ **Package split (D16)** — thin `rya-sdk` published; platform as `rya-server`.
    `packaging/{sdk,server}/pyproject.toml` build the two distributions from one
    tree with no module relocated; `packaging/surface.py` declares the SDK surface
    in code, including the deferred exceptions and allowed edges; and
    `tests/test_sdk_surface.py` walks the real import graph and fails when an SDK
    module reaches platform code or when packaging drifts from the declaration.
    `docs/PACKAGING.md` records the split. Both distributions own the `rya` console
    script (`cli.client:app` vs. `cli.main:app`), so `uvx rya create` survives
    verbatim. *Not* done: actually uploading either wheel to PyPI.
12. **Hosted operation (D13)** — per-workspace scheduling, quotas, fairness,
    console grown to workspace → project → environment → version → runs.
13. **TypeScript SDK**, and a second client repo built by someone who has never
    opened the `rya` codebase. If that is not possible, the boundary leaked.

Two things to fix in the reference clients while doing this:
`examples/loan-renewal/src/agent.py:58` reads `RYA_DATABASE_URL` to open **the
platform's own store** from inside leaf tools, and runs schema DDL from a leaf
tool behind a `global _ready` flag (`:87-95`) — N workers means N racing
first-call migrations. `docker-compose.yml`'s `x-rya-env` block (**15** ambient
variables now, up from 9 when this was written — the additions are the bundle
store and its object-store credentials: `RYA_BUNDLES_S3_BUCKET`, `_S3_ENDPOINT`,
`_S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, which the api and the
workers must share or a published version resolves to an archive the worker cannot
read) and `deploy/aws/template.yaml` (which calls itself a "reference
posture" at `:5` and whose `MutatorFunction` at `:354-367` is a stub) both need
reworking under D8.

---

## 12. Risks and the one open decision

1. **Client code runs in a platform-operated process.** Contained per D13 to a
   buggy-tenant threat model. If a hostile-tenant requirement appears, node
   isolation is the answer and it is not in v1.

   **That requirement appeared, and the answer is built and now exercised** —
   MULTITENANT_DESIGN D17, Phase 4, and Phase 6. gVisor has been run
   (`scripts/verify_gvisor.sh`): D23's third-party-wheel criterion holds, and running it
   found the isolation probe matching a kernel string no real sentry emits — which had
   it *refuting* genuine sandboxes rather than merely failing to confirm them. What
   keeps this risk live rather than closed is that it is a *declared* posture, so the
   default deployment is still what this bullet describes. What the platform will not do
   is *claim* the stronger posture without evidence: an unverifiable isolation probe
   fails the launch gate.
2. **Bundle/journal drift.** Mitigated by D9 and D12 — content-keyed steps,
   version pinning, retention, fail closed.
3. **The process lifecycle is still greenfield, and it is the scheduling half.**
   The deployment pipeline is built (§11 item 10) and so are worker registration,
   capability advertisement and bundle digests (§11 item 8) — that part of this
   risk is closed. What is not built is the **supervisor**: starting a process per
   (workspace, agent, version) on demand, scaling it on that key's queue depth,
   and scaling to zero against a tracked cold-start target. **Do not read the
   shipped pipeline as evidence the lifecycle is nearly done** — publishing a
   version and *operating* a fleet of version-pinned processes across N workspaces
   × M agents × V versions are different problems, and the second one is the one
   the hosted product's margin lives in. It should not be underestimated because
   the architecture got simpler, or because the pipeline in front of it landed.
4. **Cold start.** Scale-to-zero trades idle cost for latency on the first run of
   an idle key. Track it; pre-warm production environments.
5. **Two product surfaces to maintain (D14).** Every future feature needs a "does
   this apply to queue jobs?" answer, permanently.
6. **The policy model does not get richer.** Four enum permission levels plus pins
   is what is being relocated. A platform sold on governance will meet that
   ceiling; the agent → tool → scope → user graph remains unbuilt.
7. **Config authorship.** Once egress allowlists and model routes leave the
   manifest (D11), they leave the client's pull request. Platform config needs
   versioning, diffs and an audit trail from day one — for a governance product,
   "who reviewed this allowlist change" is a feature, not a residual.

**Open decision — the local substrate.** Does `rya dev` run on `FileStore` or a
containerised Postgres? Postgres gives execution-plane parity, because RLS and
concurrency semantics are not journal-visible and so cannot be caught by testing
the journal. `FileStore` keeps the "no keys, no database, one command" quickstart
that `README.md:55` sells. **Recommendation: `FileStore` by default, Postgres
mandatory in CI and staging** — parity where drift is actually caught, without
taxing every first run. Decide it on funnel data, not on parity theory.

---

## 13. What this supersedes, and why

Recorded so the prior reasoning is not lost. Full text in
`PLATFORM_DESIGN.superseded.md`.

| Dropped | Why it no longer applies |
|---|---|
| **The south-bound wire protocol** (old D8: WS+JSON; old D21: JSON Schema) | D1 co-locates the roles and D4 makes the queue the hand-off, so nothing crosses a network. Removes protocol semver, the schema contract, and the cross-SDK conformance suite the prior draft called "the single largest ongoing cost of the split" |
| **~30 of 36 `ctx` operations becoming RPCs** | `ctx` stays a local API inside platform-versioned code |
| **A dispatcher service** | the queue is the dispatch mechanism (D4). `queue.py` already has claim, lease, heartbeat, backoff, DLQ and concurrency caps |
| **Three pool kinds** — managed, self-hosted, dev (old §4.1, §5.2) | there is one topology |
| **The A / B / C "who executes client code" decision** (old §4) | it asked about crossing an organisational boundary that does not exist here |
| **The Temporal / Prefect prior-art argument** (old §4.4) | both cut at the store because their customers run workers against the vendor's server. Trigger.dev — which operates supervisor and task containers together — is the closer analogue |
| **Journal snapshot shipping and payload spill/proxying** (old §7, old D15) | nothing travels |
| **Dial-out-only workers with no inbound surface** (old invariant 5) | irrelevant when co-located |
| **Credential leasing with TTL and refresh** (old D7 layer 2) | it existed to cross a network trust boundary. Scope intersection survives; the lease machinery does not |
| **Per-attempt re-authorization as a protocol change** (old D13) | re-authorizing a retry is a local function call. Keep the semantics if wanted; the round-trip cost is gone |
| **The chattiness / latency risk** (old risk #1) | in-process calls |
| **"Self-hosting a worker is not a residency control"** (old D17, old §9) | reversed. Self-hosting the deployment *is* a residency control, because the deployment is the whole platform |
| **"Multi-tenant control plane, per-customer execution pools"** as a network topology (old D9) | the multi-tenant claim survives; the pool topology does not. §8 restates it as process isolation with node isolation an accepted residual |
| **Mounted-`/project` content hashing as a special case** (old D20) | subsumed by D12 — every bundle is content-hashed, mounted or not |

---

## 14. Appendix — where today's modules land

| Today | Platform | SDK | Notes |
|---|---|---|---|
| `sdk/agent.py` | — | ✅ | decorators are pure declaration |
| `sdk/context.py` | ✅ the whole `ctx` implementation | the *interface* only | no longer a surgical split — `ctx` stays platform code; the SDK ships type stubs |
| `runtime/engine.py` | ✅ | — | `_execute_action` folded into the governed path (§7) |
| `manifest/` | ✅ validation/admission | ✅ authoring/validation | shared schema, versioned |
| `providers/` | ✅ | — | keys never leave the platform; the mock model becomes an explicit route |
| `load_env()` + `ctx.secrets` | ✅ per-environment config service | — | D8 |
| `tools/registry.py` | ✅ permissions/registry | handler registration | |
| `seal.py`, `tenancy.py`, `auth.py` | ✅ | — | tenancy/RLS complete and directly reusable |
| `guard.py` | ✅ | — | a rewrite: cwd+mtime policy loading has to go |
| `store.py`, `store_postgres.py` | ✅ | — | the `open_store` seam lives inside the platform |
| `queue.py` | ✅ | — | becomes the api↔runtime hand-off (D4); mechanics reusable as-is |
| `turns.py` | ✅ | — | becomes the *only* streaming path (D6) |
| `api/`, `console/`, `mcp/` | ✅ | — | |
| `evals.py`, `readiness.py` | ✅ promotion gates | ✅ local `--check` | both read the local project today, so server-side versions rewrite their inputs |
| `cli/` | operator subset (`rya-server`) | client subset (`rya`) | D16 |
| `cloud.py` | — | ✅ | already the client-side connection store |
| `clients/typescript` | — | ✅ grows into the TS SDK | |
