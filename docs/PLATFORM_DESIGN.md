# Rya as a Platform — High-Level Design

**Status:** proposal / RFC · **Date:** 2026-07-27 · **Scope:** architecture only, no implementation

Rya today is a *library you build an agent inside of*. This document designs the
shift to Rya as a *platform your agent runs on* — the Trigger.dev / Prefect /
Temporal shape: a deployable runtime platform plus a thin client SDK, where the
client code lives in its own repository, on its own release train, and is
executed by the platform regardless of where it was written.

### Settled decisions

Recorded as they are agreed, so a reader can tell what is decided from what is
still up for debate.

| # | Decision | Why it matters | Detail |
|---|---|---|---|
| **D1** | Split the platform/SDK boundary at the **policy** layer, not the store layer | makes control-plane parity across every execution topology structural instead of a permanent testing problem | §3, §7, §11, §14 phase 0 |
| **D2** | A run's config/secrets are **platform-delivered**, never read ambiently from the worker's process | kills the "works on my machine" bug class — which is live in this codebase today | §7, §9 |
| **D3** | Push tool implementations toward **platform-resolved `url:` tools** wherever a tool is a pure HTTP call | collapses egress parity, thins workers, removes round trips | §7 |
| **D4** | **Journal-diff conformance** is the parity test; pool binding is **explicit, with no fallback** | parity becomes a CI diff; a prod run can never execute on a dev laptop | §5.2, §11 |

Open questions are collected in §15.

---

## 1. Where we are today

```
chatstudyabroad/rya-agent/            rya (the whole product)
  pyproject.toml ──── git pin ──────► plexe-ai/rya @ track-a-core
  rya.agent.yaml                        src/rya/{sdk,runtime,api,tools,...}
  src/agent.py  (3k lines, 26 tools)    `rya serve` = API + console + engine + MCP
  Dockerfile  ──► uv sync --extra deploy ──► CMD rya serve   (one process, one project)
```

The agent is not *deployed to* Rya; it is *compiled into* a Rya. The image
contains the runtime and the client code, `rya serve` is rooted at a directory
that must contain `rya.agent.yaml`, and `runtime.load_agent()` imports the
entrypoint from the local filesystem into the server process.

Consequences we are already paying for:

| Symptom | Root cause |
|---|---|
| `rya = { git = ..., branch = "track-a-core" }` | no released client contract; the client pins the *whole product* |
| A runtime fix requires rebuilding + redeploying every client image | runtime and client share a build artifact |
| One `rya serve` = one agent project | the process *is* the deployment unit |
| Client code runs in the control-plane process | no isolation boundary; cannot host untrusted or multi-tenant code |
| Each new client would fork the same Dockerfile/compose/IaC | no deployment pipeline, only a container recipe |
| A run cannot be resumed by a differently-versioned process | code version is a property of the image, not of the run |

What we *do* already have, and should not rebuild — most of a control plane:

- durable runs with journal + replay-based pause/resume (`runtime/engine.py`, `sdk/context.py`)
- a lease-based, retrying, dead-lettering job queue explicitly designed for
  **external workers in any language** (`queue.py`) with a TS worker loop already
  written (`clients/typescript`)
- durable, resumable, reclaimable chat turns over that queue (`turns.py`)
- multi-tenancy: workspaces, hashed API keys, Postgres RLS (`tenancy.py`, `store_postgres.py`)
- governance: permission tiers, kill switches, server-side arg pins, scoped
  credentials, egress firewall + grounding gate (`guard.py`), secret sealing (`seal.py`)
- observability: traces, token/cost usage, Langfuse/OTLP export
- a remote-control client and credential store (`cloud.py`), REST/WS/SSE/MCP surface

**The gap is not the control plane. It is the execution plane and the client
contract.** Roughly: we have ~70% of a platform and 0% of a deployment pipeline.

---

## 2. Target model

```
┌── client repos (independent, any language, any release train) ────────────────┐
│  chatstudyabroad/rya-agent      acme/support-agent      internal/ops-agent    │
│  depends on: rya-sdk ^1.x  (thin, published, semver'd)                        │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │  rya deploy  (bundle + manifest → version)
                               ▼
┌── Rya Platform (k8s; SaaS, on-prem, or single-node) ──────────────────────────┐
│  CONTROL PLANE   API · dispatcher · scheduler · approvals · LLM gateway ·     │
│                  guard/egress · vault · turn streams · console · MCP · evals  │
│  EXECUTION PLANE warm pools / per-run pods running *client* deployment images │
│  STATE           Postgres (runs, journal, queue, memory, tenancy) · Redis ·   │
│                  object store (bundles, artifacts) · registry · KMS           │
└───────────────────────────────────────────────────────────────────────────────┘
```

Three nouns, borrowed deliberately from the prior art:

- **Deployment** — an immutable, versioned artifact built from a client repo:
  code bundle + `rya.agent.yaml` + lockfile + SDK protocol version.
- **Environment** — `dev` / `staging` / `prod` within a project; each holds one
  *current* deployment version plus any older versions still pinned by live runs.
- **Worker pool** — where deployments execute. `managed` (platform-run, the
  default) or `self-hosted` (client-run pods that dial out). Same protocol.

The promise, stated as a contract: **a client repo needs the SDK and a deploy
token, nothing else. It never imports the runtime, never runs a server, never
knows whether it is executing on a laptop, an on-prem cluster, or our cloud.**

---

## 3. Principles

1. **The client SDK is a contract, not a dependency on our codebase.** Thin,
   published, semver'd, with a versioned wire protocol underneath it.
2. **Governance stays platform-side, and the seam is the policy layer. (D1)**
   Permissions, pins, scoped credentials, the Action Guard, kill switches, cost
   metering and the model keys are enforced by the platform, not by code the
   client controls. The split is made at the *policy* layer, not the *store*
   layer: a worker cannot make a governance decision because it does not carry
   the code to make one. Hand a worker a database connection and let it run its
   own policy, and parity is a testing problem forever; make every policy
   decision an RPC, and parity is structural. Note this is strictly *better*
   than today, where `ctx` **is** the policy engine, in-process, at whatever
   version the client happened to pin.
3. **Run inputs are declared, not ambient. (D2)** Config, secrets and
   connections are delivered to a run by the platform from per-environment
   state. Handler code reading `os.environ` is a readiness-gate finding, not a
   style preference (§7).
4. **One protocol for every execution topology.** Managed pods, self-hosted
   pools, and a laptop are the same worker speaking the same protocol at
   different addresses.
5. **Durability semantics do not change.** Journal + deterministic replay
   remains the model; we are moving the journal behind an RPC, not replacing it.
6. **A fast local loop matters — but it must not become a second control
   plane.** The zero-config, no-keys local loop is a real differentiator, and it
   is also the one thing in tension with D1. Unresolved form in §15.
7. **Every deployment is immutable and pinned per run.** Version identity is
   platform state, not an image tag someone overwrote.

**Non-goals for v1:** running arbitrary untrusted third-party code (we are
multi-tenant across *our* customers, not a public code-execution service);
replacing Postgres as the state substrate; a visual builder; auto-migrating
in-flight runs across incompatible code versions.

---

## 4. The central decision: who executes client code

Everything else follows from this. Three coherent answers exist in the market.

| | **A. Platform-hosted** (Trigger.dev) | **B. Client-hosted workers** (Prefect/Temporal) | **C. Hybrid** |
|---|---|---|---|
| Where code runs | platform's cluster | client's infra | either, per pool |
| Deploy | upload → platform builds image | client builds + runs workers | both |
| Ops burden | platform | client | choice |
| Client-network data access | needs egress/peering | native | choice |
| Code/secrets leave client network | yes | no | choice |
| Build service, registry, sandboxing needed | yes | no | yes (later) |
| Distance from today's code | large | **small** — `queue.py` is already this | — |

**Recommendation: C, sequenced B → A.**

B is close to shipping: `queue.py` already gives external workers claim / lease /
heartbeat / complete / fail / retry / DLQ / concurrency caps, and the TS client
already implements the worker loop. Standing up a *code-executing* worker on top
of that unblocks the repo split — the highest-value outcome — in weeks rather
than a quarter. A is then a strictly additive statement: "the platform operates
the worker pool for you", reusing the identical protocol, plus a build service.

This also matches the on-prem story you want: an on-prem install is the same
platform with a managed pool inside the customer's own cluster.

### 4.1 The pool model

The hybrid works because **worker pool** is a first-class platform object and the
dispatcher is deliberately blind to what kind it is.

```
                    ┌────────────── CONTROL PLANE (one deployment) ───────────────┐
   trigger ────────►│ runs · journal · approvals · policy · LLM gateway · vault · │
                    │ traces · dispatcher                                         │
                    └───┬─────────────────┬─────────────────────┬─────────────────┘
  pool binding,         │                 │                     │
  per environment  ┌────▼─────┐    ┌──────▼──────┐      ┌───────▼────────┐
                   │ managed  │    │ self-hosted │      │ dev            │
                   │ our k8s  │    │ their k8s   │      │ one laptop     │
                   │ our build│    │ their build │      │ local files    │
                   └──────────┘    └─────────────┘      └────────────────┘
       all three: same SDK worker loop, same protocol, all dial OUT
```

A pool is a token, a set of registered workers, and lease health. Each worker
advertises `{protocol version, SDK version, bundle digest, runtime + arch,
registered handler ids}` at registration. The three kinds differ in only four
ways: who operates the process, where the code artifact comes from (registry
image vs. local files), worker lifetime and identity, and network position.
Nothing about run semantics differs.

### 4.2 Routing (D4)

An environment binds to exactly one pool, and **there is no implicit fallback
between pools**. A saturated managed pool must never spill a `prod` run onto a
developer's laptop — that is a safety property, not a scheduling default. Dev
needs a finer routing key still: a dev pool is scoped to a single developer
identity, otherwise two engineers running local workers against the same project
steal each other's runs, and one person's approval resumes inside another's
uncommitted code.

### 4.3 Why both kinds exist

Not philosophy — your own repo is the argument. `rya-agent/src/students_store.py`
reads the same Postgres the Next.js app owns, and the Crizac client talks to an
internal CRM. That is a VPC-local dependency, which is the canonical reason a
client must host the worker: the data cannot move. Add data residency,
compliance, egress to internal systems, special hardware. Managed stays the
default because nobody wants to run ops and because we control the isolation
posture there.

Be precise about what the promise survives. With a self-hosted pool the
*orchestration, durable state, governance decisions and observability* are still
the platform's; only the compute is borrowed, and the user-visible contract
(deploy a version, get runs, approvals, traces) is identical. What genuinely
weakens: a client can operate their compute badly — wrong image, no autoscale,
blocked egress — and the platform can only *detect* that, not prevent it. Hence
capability advertisement (§5.2) and per-pool health surfaced in the console.

---

## 5. Component architecture

### 5.1 Control plane (stateless, horizontally scaled)

| Service | Responsibility | Today |
|---|---|---|
| **API gateway** | REST/WS/SSE, auth (API key / JWT / OAuth), tenant resolution, rate limits | `api/app.py`, `tenancy.py` |
| **Run service** | run lifecycle, the journal, step commit + memoized replay, approvals | `runtime/engine.py`, `approvals/` |
| **Dispatcher** | matches ready runs/jobs to workers by (environment, version, pool, concurrency key) | `queue.py` (extend) |
| **Scheduler** | cron, delayed jobs, lease reaping, turn reclaim sweeps | engine cron + sweeper |
| **LLM gateway** | all model calls; routes, fallbacks, streaming, token/cost metering, prompt/response tracing | `providers/`, `observability/usage` |
| **Policy service** | tool permission resolution, kill switches, arg pinning, scoped-credential authorization | `sdk/context.py` (extract) |
| **Guard / egress proxy** | outbound allowlist for tool + worker traffic, grounding gate | `guard.py` (promote to a proxy) |
| **Vault** | connection secrets, per-user credentials, envelope encryption via KMS | `seal.py`, `ctx.connections` |
| **Stream service** | durable turn buffers, fan-out to UI clients, resume by `Last-Event-ID` | `turns.py` |
| **Build service** | bundle → image → registry → version record (phase 3+) | — |
| **Console / MCP** | operator UI, remote MCP for coding agents | `console/`, `mcp/` |

Splitting these into separate deployables is a scaling decision, not an
architectural one — they can start as one binary with feature flags (as `rya
serve` is today) and be pulled apart along these seams.

### 5.2 Execution plane

- **Managed pool:** per `(environment, deployment version)` a k8s Deployment of
  worker pods running the client's image; HPA on queue depth for that version.
  Old versions scale to zero but are retained while runs are pinned to them.
- **Isolated pool:** one Job/pod per run for heavier isolation tiers; slower cold
  start, stronger blast-radius containment. Selectable per project.
- **Self-hosted pool:** the same worker image run by the client, registered with
  a pool token, dialing *out* to the control plane. No inbound ports, so it works
  behind a customer firewall — and the client's data never leaves their network.
- **Isolation:** namespace per workspace, default-deny NetworkPolicy with egress
  only via the guard proxy, non-root/read-only rootfs, per-tenant node pools for
  higher tiers, gVisor/Kata where the tenancy boundary is untrusted.
- **Fairness:** the queue's existing `concurrency_key` / `concurrency_limit`
  becomes the tenant + per-agent fairness primitive; the dispatcher must never
  let one workspace starve another.
- **Admission on advertised capability:** the platform refuses to dispatch to a
  worker whose registered handler set does not cover the tools its manifest
  version declares — so "the prod image is missing a handler" surfaces at
  registration rather than mid-run. Every run records the worker fingerprint and
  bundle digest, so a trace answers both *what actually executed* and *whether
  this code was ever deployed*.

### 5.3 State

Postgres stays primary (runs, journal, queue, memory, tenancy, RLS) — it already
carries the durability proofs. Redis for hot reads, dispatcher signalling, and
stream fan-out. Object store for deployment bundles, large payloads, and file
artifacts (`files_s3.py` exists). Container registry for built images. KMS for
envelope-encrypting vault material.

---

## 6. The client SDK

**Package split.** `rya-sdk` (new, thin) vs `rya` (platform, keeps the server
extras). The SDK depends on pydantic + an HTTP/WS transport and *nothing else* —
no FastAPI, no psycopg, no Anthropic SDK. Anything a client repo needs to write
an agent moves into it; everything that runs a server stays out.

**Surface** — deliberately the surface we already have, so client code barely
changes:

- Declaration: `define_agent()`, `@agent.on_event`, `@agent.job`, `@agent.cron`,
  `@agent.tool`, `@agent.repair`.
- Execution context: the same `ctx.*` sub-interfaces (`llm`, `tools`, `memory`,
  `knowledge`, `approvals`, `sessions`, `connections`, `channels`, `jobs`,
  `cron`, `secrets`, `logs`, `traces`, `guard`, `emit_ui`) — but every call is
  now a protocol operation instead of a direct store write.
- Worker entrypoint: the loop that registers, claims, executes, heartbeats, and
  reports. This is the piece the client's container actually runs.
- CLI: `rya dev`, `rya deploy`, `rya runs|approvals|logs|whoami` (remote-first;
  `cloud.py` already stores the connection).

**Languages.** Python first (extract from `sdk/`). TypeScript second — the
`clients/typescript` client and its `createQueueWorker` are already two-thirds of
a TS worker. A third client in a different language is the real proof the seam
is a protocol and not a Python coupling.

**Manifest.** `rya.agent.yaml` stays declarative YAML and travels with the
bundle. It is tempting to define tasks in code as Trigger.dev does, but Rya's
value is that governance is *declared, reviewable, diffable, and enforced by the
platform against an immutable version* — a code-defined manifest is one the
client process can lie about.

---

## 7. Wire protocol and durability

Today `RuntimeContext._step / _astep` journals every side effect straight to the
store, and replay after a pause returns memoized results. That contract is
preserved; only the transport changes.

**Session shape.** A worker holds one long-lived bidirectional channel to the
run service (WebSocket or gRPC), multiplexed across runs. Per run:

1. Dispatcher assigns a run; worker receives the trigger **plus the full journal
   snapshot** for that run.
2. The handler executes locally. Memoized steps resolve **from the local
   snapshot — zero round trips** (this is what keeps replay cheap).
3. Each *new* step: `authorize → execute → commit`. Authorization (permissions,
   pins, scoped credentials, guard) is a platform decision; execution is local
   for `@agent.tool` handlers and platform-side for `ctx.llm`, memory, channels,
   and `url:`-backed HTTP tools; the commit journals the result.
4. Token/trace/UI frames stream through the platform to the turn buffer, so the
   worker never needs a path to end users.
5. `ctx.approvals.request` → the platform records the pause and the SDK unwinds
   the coroutine, exactly as today. The worker completes with
   `waiting_approval` and releases its slot. On approval, the run is
   re-dispatched to *any* worker on the pinned version and replays.

**What the worker is not allowed to decide. (D1)** The protocol is drawn so that
exactly one implementation of each governed operation exists, and it is
server-side: permission resolution, pin resolution, scoped-credential
authorization, guard verdict, grounding gate, journal commit, approval pause.
The worker asks; it never rules. This is what makes control-plane parity
structural across managed pods, self-hosted pools and laptops — they are the
same server process, differing only by an environment row (§11).

**Platform-delivered inputs. (D2)** Today `ctx._env` is `load_env(project_root)`
— a `.env` file next to the code — and `ctx.secrets.get` reads from it
(`sdk/context.py:183,1540`). In the new model config, secrets and connections are
resolved from per-environment platform state and delivered over the protocol; the
worker's own process environment is not an input to a run. This turns "what is
different between dev and prod" from a property of somebody's shell into a
diffable, reviewable, access-controlled object. It is a v1 requirement rather than
a nicety, because it is the parity bug class most likely to bite (§11).

**Pure tools resolve platform-side. (D3)** Where a tool is nothing but an
outbound HTTP call, declare it as a manifest `url:` tool so the platform performs
it — already supported by `_Tools.call`'s resolution order. Three wins at once:
egress happens in the platform in *every* topology so egress parity is free rather
than emulated; the worker gets thinner; and the round trip disappears. The
direction of travel is therefore "as few `@agent.tool` handlers as the logic
actually requires" — client-side handlers earn their place by needing local
libraries, VPC-local data, or real computation, not merely by existing.

**The governed model loop stays worker-driven.** The worker owns the tool
handler code, so it runs the `ctx.llm.run` loop and calls the LLM gateway per
turn; the platform authorizes and journals each tool call and meters each model
call. The alternative — platform drives the loop and calls back into the worker
per tool — doubles the round trips and complicates streaming, for no governance
gain, since authorization already happens platform-side either way.

**Latency budget.** Agents are chatty; a naive step-per-HTTP-request design will
feel slow. Mitigations, in priority order: journal prefetch (above), persistent
multiplexed channel, pipelined/batched commits for non-authorizing steps,
co-location of managed pools with the control plane, and D3 (every tool that
resolves platform-side is a round trip that never happens).

**Protocol versioning.** The protocol gets its own semver, negotiated at worker
registration (SDK declares min/max, platform advertises capabilities). SDK
package versions and platform versions move independently; the protocol version
is the only compatibility surface, and it needs a contract test suite run
against every supported SDK on every platform release. This is the single
largest ongoing cost of the split, and it must be budgeted for explicitly.

**Journal/version coupling.** Replay is only sound against the code that wrote
the journal. Therefore: runs pin `agentVersion` (already stored today), the
dispatcher routes to that version, and pinned versions are retained until their
runs terminate. A run whose version has been deleted fails closed with a stable
`E_*` code rather than replaying against different code. This also closes the
"no in-flight version migration" gap in `VISION_GAP.md`.

---

## 8. Deployment lifecycle

```
rya deploy
  ├─ validate manifest + readiness gate locally (deploy --check already exists)
  ├─ bundle: source + lockfile + manifest + SDK/protocol version
  ├─ upload to object store; platform records a version (immutable, content-hashed)
  ├─ build: platform builds an image (managed pools) OR client builds it (self-hosted)
  ├─ promote: set the environment's current version
  └─ roll out: scale up new version, drain old, keep pinned versions alive
```

Properties worth being explicit about: deploys are **atomic per environment**
(the current-version pointer flips once, new runs go to the new version, in-flight
runs finish on theirs); **rollback is a pointer flip** to a retained version; the
**readiness gate** (`readiness.py`) becomes a server-side admission check, not
just a client-side courtesy — the platform can refuse to promote a version with
ungated actions or missing evals; and **evals** (`evals.yaml`) can run as a
promotion gate between staging and prod.

---

## 9. Security and multi-tenancy

Mostly a promotion of things that exist from process-level to cluster-level:

- **Tenant isolation** — workspace-scoped API keys and Postgres RLS stay as the
  data-layer backstop; namespaces, network policies, and (per tier) node pools
  become the compute-layer backstop.
- **Identity flow-through** — the per-user JWT that turns on per-user RLS today
  must propagate into the worker session so a run executes under the end user's
  identity, and be restored on replay (the engine already restores run identity).
- **Secrets never reach client code** — model keys live in the LLM gateway;
  connection secrets stay in the vault and are used platform-side when
  authorizing a tool call. A worker receives capability handles, not credentials.
  Where a client tool genuinely needs a credential, it is leased short-lived and
  scoped, and redaction (`_seed_secret`/`_redact`) applies to everything journaled.
- **Config is state, not ambience (D2)** — per-environment variables are stored,
  versioned, access-controlled and audited platform-side, then delivered per run.
  A config change becomes a reviewable diff with a blast radius, instead of an
  edit to a `.env` on whichever host happened to run the worker.
- **Egress** — worker egress is default-deny through the guard proxy, so the
  Action Guard becomes a network control rather than an in-process convention.
- **Supply chain** — signed bundles/images, pinned lockfiles, provenance
  recorded on the version. Mandatory before we ever host code we didn't write.

---

## 10. Developer experience

Three tiers, all the same SDK:

1. **Local, no platform** — no keys, no database: today's behaviour, where
   `open_store()` falls back to `FileStore` and `providers/llm.py` resolves
   `auto` → `mock`. Fast, hermetic, offline. It is also the one tier that implies
   a second implementation of the control plane, which is in tension with D1 —
   see §15 for the open form of this question.
2. **Local worker, real platform** — code runs on the developer's machine,
   registered as an ephemeral worker in that developer's own `dev` pool (§4.2).
   Real platform state, real approvals, real governance, live console, hot reload
   on file change, and no tunnels because the worker dials out. This is the tier
   that sells the platform, and the tier that carries the parity guarantee of
   §11. Host-interpreter execution by default (speed, debugger, hot reload) with
   an opt-in container mode when someone wants execution-plane parity on demand.
3. **Deployed** — `rya deploy` to staging/prod; identical code path.

**Naming.** `rya dev` today is neither of tiers 1 or 2 — it loads the manifest,
imports the agent, and prints what is wired (`cli/main.py:245`). Tier 2 is a new
command, so pick names deliberately rather than silently redefining `dev`
(suggestion: today's behaviour becomes `rya inspect`; `rya dev` becomes the
connected local worker, matching what every developer already expects the word to
mean).

Coding-agent ergonomics (CLI `--json`, MCP, skills) carry over unchanged; the MCP
server now points at an environment rather than a directory.

---

## 11. Parity across execution topologies

If a run behaves differently on a laptop, in a self-hosted pod, and in a managed
pod, the hybrid model is a liability rather than a feature. "Parity" is really
three questions with three different answers, and conflating them is the main way
this design could go wrong.

### 11.1 Control-plane parity — structural (D1)

Achieved by *not having a second implementation*. Every governed operation lives
server-side (§7), so a dev worker gets identical answers by construction: it is
the same control-plane process serving dev and prod, differing only by an
environment row. Permissions, pins, credential authorization, guard verdicts,
grounding, journaling and approvals cannot drift between topologies because there
is only one copy of each.

This is an *improvement* on today, not merely a preservation of it. Right now
`ctx` is the policy engine, in-process, at whatever version the client pinned —
`csa-counsellor`'s governance is currently "whatever `track-a-core` does." After
the split, governance version is a platform property that no client can pin,
fork, or lag behind.

The single exception is tier-1 local mode (§10), which genuinely is a second
implementation. If it survives, the rule is: **allowed to be incomplete, never
allowed to be divergent** — unimplemented operations fail loudly instead of
approximating, and the protocol conformance suite runs against both transports.

### 11.2 Execution-plane parity — bounded, never guaranteed

A laptop is not a pod and no design makes it one. Honest list of what differs:
arch and native wheels (arm64 laptop vs. amd64 pod), dependency versions and
stale virtualenvs, resource limits and timeout enforcement, concurrency (one dev
worker vs. N pods — races and ordering differ), clock and locale, process
lifetime (a closed lid exercises lease reclaim in ways prod rarely does), and
above all **egress**: a laptop reaches the whole internet, a pod's egress is
default-deny through the guard proxy, so a tool calling an undeclared host works
in dev and fails in prod.

This codebase already contains a sharper instance of the same bug class.
`crizac_config_from_env()` returns `None` when `CRIZAC_*` is unset *or
incomplete*, and every Crizac-backed tool then silently falls back to the
`data/*.json` seeds. Byte-identical code therefore runs against two different
worlds with no signal at all — which is exactly why D2 makes run inputs
platform-delivered rather than ambient.

The bounding stack, in order of value:

1. **D2** — ambient inputs are the root cause of most "works on my machine";
   remove them from the model entirely.
2. **D3** — moving pure tools platform-side means egress happens in the platform
   in every topology, so the highest-risk divergence is designed out rather than
   emulated.
3. **Same image where it matters** — a self-hosted pool runs the image a managed
   pool would, giving byte-identical dependency parity; local dev gets an opt-in
   container mode for the same check on demand.
4. **Admission on advertised capability** (§5.2) — catches missing handlers and
   version drift at registration.
5. **Explicit pool binding, no fallback** (§4.2) — bounds *which* topology can
   ever run a given environment's work.

### 11.3 The parity oracle: diff the journals (D4)

The strongest mechanism is automated and reuses what already exists. Take a
recorded trigger set (`rya.evals.yaml`, plus the Langfuse datasets already used
against `csa-counsellor`), run it against a dev worker and against a managed pool
on the same version, and **diff the journals** — step sequence, tool calls,
permission verdicts, pins applied, guard decisions. Not the model's prose, which
is nondeterministic; the step skeleton, with a temperature-0 route or replayed
model responses pinning the loop.

The journal already is the durability substrate, and it turns out to be a
near-perfect equivalence oracle. Parity becomes a CI diff instead of an argument.

### 11.4 The claim we actually make

Not "dev equals prod" — that would be a guarantee quietly broken by every arch
mismatch. The defensible claim is a chain of custody:

> **Nothing reaches prod without having executed on the prod-shaped execution
> plane.**

Dev buys iteration speed and gets control-plane parity by construction. Staging,
on a real pool, is where execution-plane parity is *established*, gated by the
readiness check and evals as admission control (§8). Prod is a pointer flip to a
version that has already run there.

---

## 12. Observability and operations

Per-run: trace, journal, token/cost usage, guard decisions, tool authorizations —
already built, now aggregated across deployments and versions. Per-platform: queue
depth and age by pool, dispatch latency, worker fleet health, cold-start times,
step-RPC latency percentiles, replay counts, DLQ volume. Export via OTLP; the
Langfuse integration stays. The console grows from "one project" to "workspace →
project → environment → version → runs".

---

## 13. Repo and release topology

| Repo | Contents | Consumers |
|---|---|---|
| `rya` (platform) | control plane, execution plane, dispatcher, gateway, console, helm chart, IaC | operators |
| `rya-sdk-python` | declaration API + `ctx` + worker loop + CLI | client repos |
| `rya-sdk-ts` | same, TypeScript | client repos |
| `rya-protocol` | protocol schema + conformance suite | all of the above |
| client repos | e.g. `chatstudyabroad/rya-agent` | product teams |

A separate `rya-protocol` repo (or a versioned directory the others vendor) is
what stops the protocol from silently becoming "whatever the Python SDK does".

---

## 14. Migration plan

Each phase ends in something shippable; `csa-counsellor` is the reference client
throughout, and "no behaviour change for CSA" is the acceptance test for phases
0–2.

- **Phase 0 — draw the policy boundary (no behaviour change).** The defining task
  of the whole migration, and where D1 is either earned or lost. Extract policy
  decisions (permission resolution, pin resolution, credential authorization,
  guard verdict, grounding) out of `sdk/context.py` into a service boundary, put a
  transport abstraction under `ctx` whose in-process implementation is today's code
  path, and freeze protocol v0 as the exact set of operations `ctx` performs. Also
  land D2 here: config/secrets become a run input delivered by the platform
  instead of `load_env(project_root)`. Nothing observable changes; everything
  afterwards depends on this being drawn in the right place.
- **Phase 1 — remote worker + the parity harness.** A worker process that
  registers, receives runs with journal snapshots, executes handlers, and commits
  steps over protocol v0. Ship the D4 journal-diff harness *with* it, not after —
  it is how we know phase 1 is correct. Proof: `csa-counsellor` running in its own
  container with no runtime imports, against a `rya serve` control plane, passing
  the existing phase tests and evals with a clean journal diff against in-process
  execution. Audit its 26 tools for D3 while here: every one that is a pure Crizac
  HTTP call is a candidate to become a platform-resolved `url:` tool, and several
  already declare their `url:`.
- **Phase 2 — publish the SDK, split the repo.** `rya-sdk` to PyPI (or a private
  index); `chatstudyabroad/rya-agent` swaps the git-branch pin for a semver
  range. This is the phase that pays back the pain we have today.
- **Phase 3 — deployment pipeline.** Bundles, immutable versions, environments,
  promote/rollback, version-pinned dispatch, readiness gate as admission control.
- **Phase 4 — the k8s platform.** Helm chart, managed pools with autoscaling,
  isolation tiers, guard egress proxy, self-hosted pool registration, on-prem
  install path. The existing AWS SAM template becomes one of several targets.
- **Phase 5 — prove independence.** TS SDK to parity and a second client repo
  built by someone who has never opened the `rya` codebase. If that is not
  possible, the seam has leaked and phase 0's protocol was under-specified.

---

## 15. Risks and open questions

**Risks**

1. **Chattiness → latency.** The mitigation stack in §7 must be designed in from
   phase 1, with a p95 step-latency budget as an explicit target, not tuned later.
2. **Two release trains.** Contract tests and protocol semver are mandatory
   overhead. Underinvesting here reproduces the current pin problem with extra steps.
3. **Replay across a version boundary.** Fail closed and retain pinned versions;
   never silently replay a journal against different code.
4. **Losing the simple story.** "One process, no keys, offline" is a real
   acquisition advantage, and D1 puts pressure on it. Single-node mode must remain
   a supported, tested tier whichever way the offline question (§15, open) lands.
5. **Governance leakage.** Every primitive that moves worker-side for performance
   is a governance control we no longer enforce — the inverse of D1, one
   optimisation at a time. Each such move needs a recorded decision, not an
   optimisation PR. D3 pushes in the safe direction: platform-side is both faster
   *and* more governed, which is the rare case where the incentives align.
6. **Multi-tenant code execution** is a security posture we do not have yet.
   Phase 4 should not host untrusted code without sandboxing and signed bundles.

**Open questions**

- **Does the offline tier survive?** It is the one place we would maintain a
  second control-plane implementation, against D1. Three forms: **(a)** keep the
  full local emulation — best DX, permanent double-implementation tax on every new
  `ctx` op; **(b)** drop it — offline becomes "run the platform locally" via the
  existing compose, costing the no-keys quickstart and making unit tests need a
  server; **(c)** the SDK ships a *test harness* rather than a control plane —
  enough to unit-test handler logic ("assert the handler called `course_catalogue`
  with these args"), deliberately incomplete, loud on anything unimplemented, with
  full-agent-offline served by single-node platform-in-a-container. Current
  leaning: (c).
- gRPC (better streaming/typing) vs WebSocket+JSON (trivial polyglot clients)?
- Do memory/knowledge payloads move inline or as object-store references?
- Billing unit: run, step, model token, or worker-second?
- Does the manifest stay one YAML per agent, or gain project/environment overlays?
- Data residency for managed pools in the on-prem/EU cases.
- Do we keep `rya serve`'s mounted-`/project` mode as a supported tier, or is
  single-node "platform + colocated worker"?

---

## 16. Appendix — where today's modules land

| Today | Platform | SDK | Notes |
|---|---|---|---|
| `sdk/agent.py` | — | ✅ | decorators are pure declaration |
| `sdk/context.py` | policy + journaling half | `ctx` surface half | the main surgical split; D1 says cut here, at policy — not at the store |
| `runtime/engine.py` | ✅ | worker-side execution loop only | lifecycle stays platform-side |
| `manifest/` | ✅ validation/admission | ✅ authoring/validation | shared schema, versioned |
| `providers/` | ✅ | — | keys never leave the gateway |
| `load_env()` + `ctx.secrets` | ✅ per-environment config service | delivery only | D2: `.env`-next-to-the-code stops being a run input |
| `tools/registry.py` | ✅ permissions/registry | handler registration | |
| `guard.py`, `seal.py`, `tenancy.py`, `auth.py` | ✅ | — | |
| `store.py`, `store_postgres.py` | ✅ | FileStore for offline dev | |
| `queue.py`, `turns.py` | ✅ | worker client | already the right shape |
| `api/`, `console/`, `mcp/` | ✅ | — | |
| `evals.py`, `readiness.py` | ✅ promotion gates | ✅ local `--check` | eval datasets double as the D4 journal-diff trigger set |
| `cli/` | operator subset | client subset | one binary, remote-aware |
| `cloud.py` | — | ✅ | already the client-side connection store |
| `clients/typescript` | — | ✅ grows into the TS SDK | |
