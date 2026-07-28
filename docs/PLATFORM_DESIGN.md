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
| **D1** | Split the platform/SDK boundary at the **policy** layer, not the store layer | a governed decision must not ship inside a client-versioned artifact. This is a *coupling and versioning* constraint, not a trust one — see §4.4 for how Temporal, Prefect and Trigger.dev all land in the same place | §3, §5.4, §7, §11, §14 phase 0 |
| **D2** | A run's config/secrets are **platform-delivered**, never read ambiently from the worker's process | kills the "works on my machine" bug class — which is live in this codebase today | §7, §9 |
| **D3** | Push tool implementations toward **platform-resolved `url:` tools** wherever a tool is a pure HTTP call | collapses egress parity, thins workers, removes round trips | §7 |
| **D4** | **Journal-diff conformance** is the parity test; pool binding is **explicit, with no fallback** | parity becomes a CI diff; a prod run can never execute on a dev laptop | §5.2, §11 |

Open questions are collected in §15.

### Positioning deltas — six decisions this RFC forces

D1–D4 are architecture and are settled. The six below are **product decisions**
that this RFC currently makes implicitly, by contradicting something the repo,
the docs, or the public site already commits to. They are listed here so they get
decided deliberately rather than absorbed. None of them is mine to settle.

| # | Committed today | What this RFC implies | Status |
|---|---|---|---|
| **P1** | `architecture.md:11-12` — "**the store is the seam** that makes one codebase serve every tier," with `ctx`/`engine` holding the store handle | D1: the worker never touches the store; the seam is policy | direct reversal of the documented central idea — needs `architecture.md` rewritten, not patched |
| **P2** | `site/index.html` — "**We do not run a multi-tenant SaaS.** Each customer gets a dedicated deployment inside their own account"; `deploy/aws/template.yaml:3-9` — "no shared blast radius" | managed multi-tenant is the *default* pool kind (§4.1) | **a live public claim, verbatim contradicted.** Coexistence is possible — single-tenant becomes the on-prem / self-hosted-pool topology — but the marketing claim must change from absolute to tiered |
| **P3** | `architecture.md:96-105` draws OSS-core vs proprietary-cloud; `LICENSE` grants Apache-2.0 (irrevocably) over *all* of `src/rya`, including `guard.py`, `seal.py`, `tenancy.py` — the very modules that diagram labels proprietary. `architecture.md:107` already admits the licence is "a product decision, not yet committed" | §13's repo split cuts by *who executes*, moving guard / seal / policy / store / providers platform-side | the split forces the licensing decision that has been deferred. Note the existing grant is perpetual and irrevocable for today's code; only future platform releases could differ |
| **P4** | `README.md:93-95` — "`rya serve` is the whole product in one process… the hosted instance **is** `rya serve`" | control plane and execution plane are separate deployables | §1 of this RFC uses that exact sentence's subject as its problem statement |
| **P5** | `README.md:61-63`, `langfuse.md:37` — set `ANTHROPIC_API_KEY` / `RYA_DATABASE_URL` / `LANGFUSE_*` and the same code runs everywhere | D2: ambient env is not a run input | the documented mechanism becomes a readiness-gate finding |
| **P6** | `rya` on PyPI is the package an *agent author* installs — `uvx rya create` in `README.md:54` and on the site. **The publish is still pending sign-off** | `rya` becomes the platform package (keeps `api`/`postgres`/`llm`/`mcp`); `rya-sdk` is the client one | name inversion: both packages want the `rya` console script, and `uvx rya create` breaks. **Recommend pausing the pending `uv publish` until this is settled** — a PyPI name is effectively irreversible |

**Two commitments this RFC currently drops in silence** — both need an explicit
verdict rather than omission:

- **The Cognito / API-Gateway / per-mutator-Lambda architecture.**
  `VISION_GAP.md:141-142` calls it "the hardest, most differentiating property,"
  and `deploy/aws/template.yaml` already implements it (cfn-lint clean). This RFC
  answers the same question — how does a governed write get validated — a
  structurally different way (policy service + authorize/commit RPC, §7) and never
  says whether the Lambda-mutator model is superseded, absorbed, or abandoned.
- **RWAP** (`docs/integrations/rwap.md`) — a real external consumer of the bare
  `/queue/*` HTTP API, explicitly designed so "RWAP never adopts Python" and needs
  no SDK. §4 leans on `queue.py` as its foundation and §5.2 adds worker
  registration carrying protocol version, SDK version and bundle digest, without
  stating whether the SDK-free HTTP path survives unchanged. A working integration
  is at silent risk.

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
| Distance from today's code | large | **moderate** — see the correction below | — |

**Recommendation: C, sequenced B → A.**

`queue.py` already gives external workers claim / lease / heartbeat / complete /
fail / retry / DLQ / concurrency caps, and the TS client already implements that
loop. **But an earlier draft of this section claimed the distance to B was
"small," and that was wrong.** `queue.py` is a queue for *opaque background jobs*
whose payloads a caller-supplied handler interprets; it has no relationship to
`RuntimeContext`, the journal, or replay. There is no worker registration, no
capability advertisement, and no "claim a run, receive its journal snapshot,
authorize → execute → commit per new step" flow. The only place a queue-claimed
job maps onto real agent-run execution is `turns.py:_run_turn`, and it does so
**in-process**, calling `engine.run_event(...)` with the store handle the caller
already has. So: the queue *mechanics* are done and genuinely reusable; the
store-less, code-executing worker is greenfield. B is still the right first step
and still far cheaper than A — it just isn't nearly free.

A is then a strictly additive statement: "the platform operates the worker pool
for you", reusing the identical protocol, plus a build service.

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
client must host the worker: the data cannot move. (§5.6 invariant 1 is scoped so
this is explicitly *allowed* — the worker is barred from the **platform's** state,
never from the client's own.) Add data residency,
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

### 4.4 Prior art: where the others cut

Read from their docs rather than from memory, because this decision is the one
most worth borrowing on.

| | Where code runs | Worker → DB? | Who owns state transitions | Local dev |
|---|---|---|---|---|
| **[Temporal](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works)** | the customer's workers, *including on Temporal Cloud* | **never** — explicit in the docs | the Server: workers emit **Commands**, the History Service validates them into Events and persists | a worker on your machine against a server |
| **[Prefect](https://www.prefect.io/how-it-works)** | the customer's infra — "your code and data never leave your infrastructure" | **no** — workers *poll* the API; Prefect makes no inbound request into your network | the control plane: scheduling, state, orchestration rules; it receives "logs and state, never your data" | a worker process locally |
| **[Trigger.dev](https://mintlify.wiki/triggerdotdev/trigger.dev/how-it-works)** | their infra (supervisor + per-task containers) | via API | the platform: queue, run state, CRIU checkpoints, observability | [`trigger dev`](https://trigger.dev/docs/cli-dev-commands) runs tasks in a **local Node process** registered with the webapp |

Three conclusions:

1. **All three cut above the store; none hands the executing process a database.**
   Three independent teams, three different products, same boundary.
2. **Two of the three treat customer-run workers as the default, not an exception
   — and still do not ship state-transition authority to them.** Temporal Cloud's
   workers are *always* the customer's. Prefect sells the hybrid model as the
   enterprise posture. So "customer-hosted" is not a second-class or suspect
   topology; and yet neither concludes "therefore let the worker decide." The
   industry answer is *trusted workers, server-validated transitions* — not
   because workers are hostile, but because it is the only way one orchestrator
   serves many worker versions and topologies at once.
3. **Temporal kept determinism and replay SDK-side, and that is exactly where its
   well-known pain lives.** Non-determinism-on-replay errors, `GetVersion`
   patching, [Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)
   with Build IDs and Deployment Versions, and warnings that once an SDK option is
   enabled you cannot roll back to an SDK that lacks it. An entire product surface
   exists to manage the consequences of SDK-side replay logic. That is empirical
   evidence for putting *less* in the SDK — and it independently confirms §7's
   journal/version coupling risk, since Worker Versioning is precisely the
   mitigation proposed there.

**Where the analogy breaks — in our favour.** None of these three has Rya's
governance layer. Temporal has no notion of "the model may not call this tool", no
egress firewall, no grounding gate, no approval tier a caller cannot bypass. For
them, SDK-side logic is *bookkeeping*: wrong is annoying. For Rya the policy layer
is a *security boundary*: wrong means a student record written against the wrong
counsellor. Rya therefore has strictly more reason to server-side policy than the
three companies that already do.

**A note on framing.** "Untrusted worker" is the wrong justification and should
not appear in this design. The property that matters is not malice but **skew**: a
self-hosted worker is not hostile, it is eight months behind on the SDK; a laptop
is not adversarial, it has a stale virtualenv and a `.env` from March. Trust the
operator completely and every coupling argument above still holds. Corollary worth
keeping: **trust changes the performance envelope, not the authority boundary** —
a co-located managed pool can legitimately earn read-side fast paths (cached or
replica-served memory and knowledge reads) while every decision and every write
stays server-side.

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
| **Policy service** | tool permission resolution, kill switches, arg pinning, scoped-credential authorization | extract from `sdk/context.py` — logic exists and is tested, but inline in the file being thinned. Kill switches also need carving out of `load_memory("_runtime_config")`, an ordinary memory scope with no schema distinction from user data |
| **Guard / egress proxy** | outbound allowlist for tool + worker traffic, grounding gate | `guard.py` — a rewrite, not a promotion: policy loads from `cwd`/`RYA_GUARD_PATH` by file mtime, and `check_egress` is called from `_http_tool` *inside `sdk/context.py`* |
| **Vault** | connection secrets, per-user credentials, envelope encryption via KMS | `seal.py`, `ctx.connections` |
| **Stream service** | durable turn buffers, fan-out to UI clients, resume by `Last-Event-ID` | `turns.py` |
| **Build service** | bundle → image → registry → version record (phase 3+) | — |
| **Console / MCP** | operator UI, remote MCP for coding agents | `console/`, `mcp/` |

Splitting these into separate deployables is a scaling decision, not an
architectural one — they can start as one binary with feature flags (as `rya
serve` is today) and be pulled apart along these seams. §5.4 gives the same
picture cut by *concern* rather than by service, which is the more useful view
when arguing about where a given behaviour belongs.

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

### 5.4 Division of responsibility

The one-line rule: **the worker executes; the control plane decides and
remembers.** Everything below follows from that, and any row that drifts from it
is either a bug or a decision someone needs to record (§15 risk 5).

| Concern | Control plane | Worker pool |
|---|---|---|
| Run lifecycle + status | creates, owns, terminates | reports its outcome |
| Journal | appends, memoizes, validates step ordering | receives a snapshot; requests commits |
| Handler bodies (`on_event`, `job`, `cron`, `tool`) | never | **executes** — this is its whole job |
| Tool permission + kill switches | resolves, fail-closed | asks |
| Arg pinning | resolves from trusted state, overwrites caller args | receives already-pinned input |
| Scoped credential authorization | intersects tool ∩ connection ∩ user scopes | receives a handle, never the secret |
| `@agent.tool` leaf implementation | never | executes |
| `url:` HTTP tool | performs the call (D3) | not involved |
| Model calls, routes, fallbacks | performs all of them; meters tokens + cost | requests completions |
| Governed model loop | authorizes and journals each step | drives the loop |
| Memory / knowledge / vector search | owns, persists, embeds | requests |
| Sessions | owns | requests |
| Approvals — request, pause, resolve, resume | records the pause, resolves it, re-dispatches | raises the pause and exits |
| Channels (outbound email, webhook) | sends | requests |
| Jobs / cron / delayed work | schedules, leases, retries, dead-letters | claims and executes |
| Turn stream buffer + UI fan-out | owns; **sole path to end users** | emits frames upstream only |
| Guard — egress allowlist | decides and enforces (proxy) | subject to it |
| Guard — grounding gate + id-secrecy scrub | applies at commit | not involved |
| Secret storage + redaction | vaults; redacts before anything persists | holds nothing long-lived |
| Per-environment config | owns, versions, delivers per run (D2) | receives it; never reads ambient env |
| Traces / usage / cost | produces the record | contributes step data |
| Evals | runs them, gates promotion | executes handlers under eval |
| Deployment versions + rollout | builds, pins, promotes, retains | advertises which version it is |
| Dispatch, concurrency, fairness | decides | requests work |
| Worker registration, lease, heartbeat | grants, expires, reclaims | registers and heartbeats |
| Identity (JWT verify, RLS scoping) | verifies, scopes, restores on replay | carries an opaque token |
| Retry / backoff / dead-letter decisions | decides | reports failure, nothing more |

Read the middle column as *authority*, not *location*: a self-hosted pool changes
where the compute sits without moving a single row leftward or rightward.

**One row above is not true of today's code, in a way that matters.** There are
*two* tool-execution paths, and only one is governed. `ctx.tools.call`
(`sdk/context.py:978`) resolves permission, pins, credentials and scrub as
described. `Engine._execute_action` (`runtime/engine.py:419-459`) — the path taken
when an **approval resolves** — re-implements channel / HTTP / handler / mock
dispatch with no permission check, no `guard.scrub`, and only an ad-hoc credential
lookup. A policy service that wraps only `ctx.tools.call` would silently leave
every approved action ungoverned. This is a live gap today, not merely a migration
concern, and closing it is phase-0 work (§14).

### 5.5 Access matrix

Responsibility is what each side *does*; this is what each side can even *reach*.
The second table is the one that makes the first enforceable.

| Resource | Control plane | Worker pool |
|---|---|---|
| **The platform's** Postgres / store | full owner | **never** — no handle, no DSN, no database identity |
| Journal rows | writes them | reads a per-run snapshot; writes only via commit RPC |
| Model provider keys | holds | never sees one |
| Connection secrets / vault | holds and uses | capability handle — *aspirational; today `ctx.connections.secret()` hands over the raw bearer, see §5.6* |
| Object store | owns | scoped, expiring URLs when a payload must move |
| Kill-switch + permission state | authoritative | may cache a hint; never authoritative |
| Per-environment config | owns | receives per-run delivery |
| Deployment bundles / registry | owns | pulls its own image |
| Public internet | for `url:` tools | via the guard proxy (managed); its own path (self-hosted) |
| The client's VPC — their Postgres, internal CRM | only if reachable, and only for a `url:` tool | **native access — this is why self-hosted pools exist** |
| End users (browser, email recipient) | sole path | **never** |
| Any other tenant's anything | RLS-bounded | no addressable surface at all |

Note the shape those two tables make together: the worker is reachable *from*
nothing and reaches *almost* nothing — except the one thing it exists for, which
is the client's own systems. That is the whole architecture in a sentence.

### 5.6 Invariants (the phase-0 review checklist)

Six rules that make D1 checkable rather than aspirational. A PR that breaks one
needs an explicit recorded decision, not a rationale in a commit message.

1. The worker never holds a handle, DSN, or database identity **for the
   platform's own state**. Its access to the *client's* systems is unrestricted —
   that is the entire point of a self-hosted pool (§4.3), and the earlier draft of
   this invariant wrongly forbade it. `rya-agent/src/students_store.py` connecting
   to the client's own Postgres is *correct* under this rule; a worker reaching
   `rya_runs` is not.
2. The worker never holds a long-lived **platform-issued** credential. Model keys
   never reach it at all. Upstream credentials for the client's own systems are
   leased, short-lived and scoped — **not yet true today**, see below.
3. No policy predicate executes worker-side.
4. Every persisted fact **about a run** is written by the control plane.
5. The worker has no inbound network surface — it always dials out.
6. The worker has no path to an end user; all frames go through the platform.

**Invariant 2 conflicts with a deliberate, load-bearing API today.**
`ctx.connections.secret()` (`sdk/context.py:1368`) returns *the raw plaintext
bearer* to handler code — documented as "for a HANDLER to build an upstream client
with", enforcing the scope-intersection rule and seeding the redaction vault, and
deliberately **not** journaled so a replay re-resolves it live rather than
memoizing a credential. This is not an oversight to be tightened away: it is how
`csa-counsellor` authenticates every live Crizac tool (a bearer minted once per
turn and carried to leaf handlers through a `ContextVar`), and `CORE_GAPS.md` asks
for *more* of this capability, not less. Invariant 2 as originally written would
have deleted a working feature the only real client depends on. Three candidate
resolutions — lease short-lived credentials with a TTL and refresh; proxy the
upstream call platform-side so the worker never needs the secret (the real payoff
of D3); or keep raw hand-off as an explicitly governed, audited exception. This is
an open design item (§15), and it is a prerequisite for the CSA proof (§14).

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

**How big the protocol actually is — measured, not estimated.**
`sdk/context.py` routes **38 `_step`/`_astep` call sites** through roughly **35
distinct journaled kinds** (`memory.*`, `knowledge.*`, `session.*`, `connection.*`,
`file.*`, `job.*`, `channel.send`, `log`, `trace.event`, `event.emit`, `ui.emit`,
`llm.respond`, `llm.chat`, `model.call`, `tool.call`). By §5.4's own authority
table, about **30 of those 35 move server-side** — not the handful implied by
calling this "the main surgical split" in §16. Protocol v0 is therefore ~30
semantic operations, and three of them are entangled in ways the protocol sketch
above does not yet resolve:

- **`guard.scrub` runs inside the journaled closure** (`sdk/context.py:1054`,
  within the `run_tool()` passed to `_astep`) — so "execute" and the
  platform-owned "scrub at commit" are literally the same line today. There is no
  existing commit step to split at.
- **Retry and repair happen inside one journaled step.**
  `_invoke_with_recovery` (`sdk/context.py:1088-1128`) runs N backend attempts plus
  an `@agent.repair` callback inside a single `tool.call`. The current code has
  already made an implicit choice — *one authorization, N local attempts* — that
  the protocol must deliberately preserve or knowingly break. Undecided.
- **Streaming callbacks are function pointers, not messages.** `on_token` is
  handed straight into the provider call (`sdk/context.py:494,510,610-613`), so
  token frames fire from inside a third-party HTTP-streaming parser several frames
  below `ctx`. None of this crosses a process boundary unmodified.

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

Note what this argument does *not* rest on: nobody has to distrust the operator.
The failure mode being designed out is skew, not malice — a worker that is
perfectly well-intentioned and four SDK releases behind (§4.4). §5.4 and §5.5 are
the authoritative statement of which side owns what.

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

The client repo contains a sharper instance of the same bug class (in
`chatstudyabroad/rya-agent`, not in `rya` itself — an earlier draft said "this
codebase," which was wrong). `crizac_config_from_env()`
(`src/crizac/config.py:45`) returns `None` when `CRIZAC_*` is unset *or
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

**Two prerequisites, neither true today.** First, **D2 must actually land before
D4 is meaningful** — otherwise a diff between a dev worker (with `CRIZAC_*` unset,
so seed data) and a managed pool (configured, so live data) is dominated by
config-driven divergence, which is the very failure mode §11.2 uses as its
example. Second, **model determinism must be pinned**: `csa-counsellor`'s primary
conversational route (`compose`) carries no `temperature: 0` — only `extract`
does — and its eval judge already shows real run-to-run variance. Until both
hold, a journal diff produces noise, not signal.

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

Each phase ends in something shippable. `csa-counsellor` is the reference client
throughout — but see phase 1 for what it can and cannot actually prove.

- **Phase 0 — draw the policy boundary (no behaviour change).** The defining task
  of the whole migration, and where D1 is either earned or lost. Five pieces of
  work; the scope is materially larger than this plan's earlier draft claimed:
  - Extract policy decisions (permission resolution, pin resolution, credential
    authorization, guard verdict, grounding) out of `sdk/context.py` into a
    service boundary whose in-process implementation is today's code path.
  - **Close the second ungoverned tool path.** `Engine._execute_action`
    (`runtime/engine.py:419-459`) must route through the same policy service, or
    every approved action stays ungoverned (§5.4).
  - Carve kill-switch state out of the generic `_runtime_config` memory scope
    into privileged, worker-unreachable storage.
  - Rewrite `guard.py`'s policy loading away from `cwd` + file mtime toward
    per-environment platform state, and lift `check_egress` out of
    `sdk/context.py`.
  - Land D2 — at its real scope. **86 `os.environ` reads across 20 files**, of
    which `providers/llm.py` alone has 22, called from inside
    `ctx.llm.respond`/`run` and bypassing `ctx._env` entirely. Replacing
    `load_env`/`ctx.secrets` (this plan's earlier scope) does not touch the
    model-call path at all.

  Then freeze protocol v0 as the ~30 semantic operations §7 measures. Nothing
  observable changes; everything after depends on this line being in the right place.
- **Phase 1 — remote worker + the parity harness.** A worker process that
  registers, receives runs with journal snapshots, executes handlers, and commits
  steps over protocol v0. Ship the D4 journal-diff harness *with* it — it is how
  we know phase 1 is correct. Two things this phase must also absorb: the
  **streaming rebuild** (`/ws` and SSE pass raw Python closures into the engine
  in-process and cannot survive a process split; only `turns.py`'s durable seq'd
  buffer can, so the other two get rebuilt on it), and the **test substrate**
  (9 files construct `RuntimeContext` directly against a FileStore — including
  `test_platform_gaps.py`, the direct coverage for exactly the primitives D1
  relocates — and 25 more steer behaviour with `monkeypatch.setenv`).

  **What CSA can and cannot prove here.** The governance half of `csa-counsellor`
  — tool permissions, `pin:`/`adopt:`, the id-secrecy scrub, retry/repair, kill
  switches — needs essentially no change and is a strong proof of D1. The
  live-integration half cannot reach "no behaviour change" in this phase: roughly
  1,500–2,000 of its ~5,900 lines (`src/crizac/*`, `src/plexe.py`,
  `src/students_store.py`, and the credential plumbing in `src/agent.py`) depend
  on the two primitives phase 1b designs. **Phase 1's acceptance bar is therefore
  the governance half against seed data**, with the live-Crizac path explicitly
  out of scope until 1b lands. An earlier draft set "no behaviour change for CSA"
  as the bar for phases 0–2; that is not reachable, and this corrects it.

  Audit tool decomposition for D3 while here, with realistic expectations: of 29
  declared tools, 8 carry a `url:` — but those fields are *governance metadata*,
  not routing. The real Crizac integration is a stateful, TTL-cached, multi-step
  name-resolution cascade, which §7's own escape valve correctly keeps
  worker-side. D3 will thin this client considerably less than earlier drafts implied.
- **Phase 1b — the two missing primitives.** Neither exists today, and CSA's live
  path is blocked on both:
  1. **Client-owned data access.** A worker legitimately reaches its own databases
     and internal services (§5.6 invariant 1 permits this). What is missing is any
     platform-side *notion* of it — declaration, governance and audit for reads
     and writes the platform does not mediate. Today `students_store.py` simply
     opens a connection and nothing knows. `CORE_GAPS.md`'s tier-2
     `rya.data.open()` ask is the client's own version of this request.
  2. **Leaf credential leasing.** The replacement for `ctx.connections.secret()`'s
     raw hand-off (§5.6): short-lived, scoped, refreshable, and reaching leaf
     handlers that today receive no `ctx` at all. `CORE_GAPS.md` #3 and #4 are
     asking for precisely this.

  State it plainly: as drafted, this RFC's invariants make three of the one real
  client's own recorded asks (#3 leaf credentials, #7 a pluggable memory sink to
  their own Postgres, and the tier-2 data ask) *harder* than they are today. 1b is
  where that debt is paid, and it should not be deferred behind the deployment
  pipeline.
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
  full-agent-offline served by single-node platform-in-a-container; **(d)** go
  further and *collapse the dev topology altogether* — local development means
  running the platform locally in a container (`docker-compose.yml` already does
  this), which deletes a whole pool kind from §4.1 and answers this question in the
  same move. (d) is the largest genuine simplification available anywhere in this
  design; it costs the zero-install quickstart and some inner-loop speed. Current
  leaning: (c), with (d) worth costing out properly before phase 1 commits to a
  dev pool.
- **How does a leaf handler get an upstream credential?** Blocking for phase 1b
  and for CSA's live path. `ctx.connections.secret()` hands over a raw long-lived
  bearer today, by design (§5.6); leaf tools receive no `ctx` at all and get it
  through a `ContextVar`. Candidates: short-lived scoped leases with refresh;
  platform-side proxying so the worker never holds it (D3's real payoff); or a
  governed, audited raw-hand-off exception. Until this is answered, invariant 2 is
  aspirational.
- **Does a retry re-authorize, or does one authorization cover N attempts?**
  `_invoke_with_recovery` has already made this choice implicitly (one
  authorization, N local attempts). The protocol must preserve or knowingly break
  it — §7.
- **Does the SDK-free `/queue/*` HTTP path survive?** RWAP depends on it
  explicitly and adopts no SDK. If worker registration becomes mandatory, that
  integration breaks; if it stays optional, we maintain two entry contracts.
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
| `sdk/context.py` | policy + journaling half | `ctx` surface half | the main surgical split; D1 says cut at policy, not at the store. Measured: ~30 of its ~35 journaled kinds move (§7) |
| `runtime/engine.py` | ✅ | worker-side execution loop only | lifecycle stays platform-side; `_execute_action` is a second, ungoverned tool path that must be folded into the policy service (§5.4) |
| `manifest/` | ✅ validation/admission | ✅ authoring/validation | shared schema, versioned |
| `providers/` | ✅ | — | keys never leave the gateway |
| `load_env()` + `ctx.secrets` | ✅ per-environment config service | delivery only | D2: `.env`-next-to-the-code stops being a run input |
| `tools/registry.py` | ✅ permissions/registry | handler registration | |
| `seal.py`, `tenancy.py`, `auth.py` | ✅ | — | tenancy/RLS is complete and directly reusable |
| `guard.py` | ✅ | — | a rewrite, not a move: cwd+mtime policy loading, and `check_egress` currently called from inside `sdk/context.py` |
| `store.py`, `store_postgres.py` | ✅ | FileStore only if the offline tier survives (§15) | |
| `queue.py`, `turns.py` | ✅ | worker client | queue *mechanics* reusable as-is; the run-execution protocol on top is greenfield (§4) |
| `api/`, `console/`, `mcp/` | ✅ | — | |
| `evals.py`, `readiness.py` | ✅ promotion gates | ✅ local `--check` | eval datasets double as the D4 journal-diff trigger set; both are 100% client-local today (they read `load_env` + local `store`/`agent` objects), so the server-side versions are rewrites of their inputs, not relocations |
| `cli/` | operator subset | client subset | **unresolved: both packages want the `rya` console-script name** — see P6 |
| `cloud.py` | — | ✅ | already the client-side connection store |
| `clients/typescript` | — | ✅ grows into the TS SDK | |
