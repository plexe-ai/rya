# Rya Architecture — OSS core + managed cloud

Rya follows the open-core pattern (same shape as Supabase/InsForge): a fully
functional **open-source core** you can self-host, and a **managed cloud** that
adds the operational layer on top. The core never bakes in auth or
multi-tenancy — those live *above* it — so the exact same runtime code serves a
laptop, a self-hosted box, and the cloud.

## The substrate-agnostic core (open source)

The core is the runtime, SDK, manifest, CLI, MCP server, and the **pluggable
state store**. The store is the seam that makes one codebase serve every tier:

> **Being relocated, not removed.** `PLATFORM_DESIGN.md` (D1/D5) keeps this seam
> but moves it *below the policy boundary* — inside the platform, out of reach of
> `ctx` — because a client-versioned process must not own state. D19 then scopes
> `FileStore` to hermetic tests, so the local platform runs Postgres and "a laptop
> needs no database" is no longer one of the properties this seam buys.

```
ctx / engine ──► open_store(root) ──┬─► FileStore     (.rya/ JSON)   zero-config local dev
                                    └─► PostgresStore  (JSONB)        self-host + cloud
```

Selection is by env var — no code change:

| Env | Backend | Use |
|-----|---------|-----|
| (none) | `FileStore` | Local dev, `rya dev`, CI, tests — offline & reproducible |
| `RYA_DATABASE_URL` | `PostgresStore` | OSS self-host (plain Postgres) **and** managed cloud (managed Postgres) |

Durability is real on Postgres: a run can pause for approval in one process and
resume to completion in another (proven in `tests/test_postgres_store.py`).

Likewise the LLM seam (`ctx.llm`) is provider-pluggable: deterministic mock by
default, real Claude when `ANTHROPIC_API_KEY` is set — same agent code either way.

**Bundle artifacts get the same treatment.** `RYA_BUNDLES_S3_BUCKET` selects an
object store for the immutable, content-hashed archives a deploy produces; unset
falls back to a local archive root under `.rya/`. `RYA_BUNDLES_S3_ENDPOINT` points
the S3 arm at MinIO, Ceph or R2 — declaring it also forces path-style addressing,
because the default virtual-host form would resolve `<bucket>.<host>` and no such
name exists on a container network. Leave it blank for real S3.

The local arm only works when the api and the workers share a filesystem, which in
compose they do not — which is why there is a `minio` service, and why `s3` is not
an optional extra in the `Dockerfile`.

### Self-host (OSS)

```bash
git clone <rya repo> && cd rya
cp .env.example .env          # optionally add ANTHROPIC_API_KEY
docker compose up             # Postgres + MinIO + api on :8787 + one worker
```

That brings up Postgres, the bundle archive store, the control-plane api, and one
execution-plane worker. The split is load-bearing rather than incidental: the api
service sets `RYA_API_INLINE_WORKER=0`, so it runs **no** handler code, and without
the `worker` service nothing executes at all.

Set `RYA_PROJECT=../your-agent` in `.env` to serve your own project — one variable,
because the api and every worker mount the same tree and pointing only one of them
somewhere new would leave them serving different code. Platform state
(`/project/.rya`: the unpacked bundle cache, local archives) lives in the
`rya_project_state` volume instead of your working copy, so the containers' root
-owned files never land in your checkout.

`docker compose --profile pinned up worker-pinned` adds a worker that serves
whichever version `prod` points at, rather than the mounted tree.

### Self-host, multi-tenant

```bash
docker compose -f docker-compose.yml -f docker-compose.multitenant.yml up -d
```

An overlay rather than a second stack, and passed with an explicit `-f` rather than
named `docker-compose.override.yml`: that filename is auto-loaded, and multi-tenancy
changes what a credential *means*, so it should be the most visible thing about the
command that starts it. Reserve the auto-loaded name for a developer's own untracked
tweaks.

It needs `RYA_MULTITENANT=1`, `RYA_APP_DB_PASSWORD` and `RYA_WORKER_DATABASE_URL` in
`.env`, and it differs from the base file in three ways:

- **8787 binds to loopback.** Multi-tenant mode opens `POST /v1/signup`, and unlike
  `/v1/projects` that route is *not* gated by `RYA_ADMIN_TOKEN` — deliberately, since
  the premise is self-serve signup by untrusted tenants. The base file publishes on
  `0.0.0.0`, so the combination would let anyone who can reach the host mint a
  workspace. Put a TLS-terminating, rate-limiting proxy in front, then republish.
- **The default `worker` is profiled out**, because it serves workspace `default` —
  the one workspace no API key maps to once tenancy is on. `--profile singletenant`
  brings it back to drain rows written before the switch.
- **There is one claimer per tenant**, because `--workspace` is load-bearing:
  `open_worker_store` scopes the store to that workspace and connects as the weaker
  `rya_worker` role, where `open_store` would return a store on `default` whatever
  the caller intended. Two are declared (`RYA_WORKSPACE_A`/`_B`) as a worked example
  of the isolation, not as a scaling plan — see the next section.

### Nothing has to declare the workers

Compose declares one worker because a compose file is a static list, and a static
list is exactly what a fleet of N tenants x M agents x V versions cannot be. `rya
supervisor` (D25) is the alternative: it reads claimable depth per key and the
worker registry, then starts, scales and reaps through a pluggable
`ExecutionDriver` (D26).

```bash
rya supervisor --plan      # the decision, and why, with no effects
rya supervisor --env prod  # act on it every 5s
```

Without it, scale-to-zero is one-way — `--idle-exit` makes a worker leave and
nothing brings the key back. With it, work arriving is what brings it back.

**Run one per workspace.** A supervisor takes a lease before it applies a plan (D34), so
a second replica goes passive rather than starting a duplicate fleet — which it otherwise
would, and not by agreement: `observe` reconciles the registry against the *driver's*
inventory, and a second supervisor's inventory is empty, so it sees a fleet it did not
launch and starts its own anyway. Because the lease is per workspace, two supervisors
over many tenants split the work instead of one idling.

Three drivers exist: `local` (a subprocess on this host, isolation `none`), `docker`
and `kubernetes`. The last two declare `sandboxed` **only when configured for gVisor**
— `--runtime=runsc` or a `RuntimeClass` — because a container on the host kernel is a
shared kernel whatever launched it. `ecs` is unwritten.

`RYA_UNTRUSTED_TENANTS=1` then checks all four of D18 (no credentials in the tenant
process), D23 (the sandbox), D24 (egress enforced by the network) and D32 (a driver that
can host the broker outside the sandbox) and refuses to start unless every one holds,
naming those that do not. `rya posture` shows the same
answer without a deploy, and `--verify` probes the substrate rather than trusting its
declaration. Both container drivers satisfy D32 by launching a **pair** — a
credentialed claimer container beside a credential-free sandbox container running
`rya template-host`, sharing an in-memory volume for the two sockets — so the
credential boundary is a container boundary rather than a process one. A driver that
launches only the sandbox half is still refused, because a claimer with no database
credential claims nothing while looking perfectly healthy.

gVisor has been run (`scripts/verify_gvisor.sh`): the three D23 dependencies work under
a real sentry, and doing it found the isolation probe matching the wrong kernel string
— which made it *refute* a genuine sandbox rather than merely fail to confirm one. An
inconclusive probe is still a refusal, so nothing can quietly depend on an unverified
substrate.

## What the managed cloud adds (not in the OSS core)

| Concern | OSS core today | Managed cloud (layered above) |
|---------|----------------|-------------------------------|
| Persistence | File or your Postgres | Managed, backed-up Postgres |
| **Auth / ownership** | operator token (`RYA_TOKEN`) or per-workspace API keys | accounts, billing-linked keys |
| **Multi-tenancy** | **workspaces + API keys + Postgres RLS** (`RYA_MULTITENANT=1`) | managed onboarding, dashboard |
| Execution | one local/self-host worker | managed hosted workers, autoscale |
| MCP | stdio (local) | **remote MCP over HTTP + OAuth** + per-client consent |
| Deploy | `docker compose` | one-command `rya deploy` to managed hosting |
| Dashboard | CLI/API only | hosted console (runs, approvals, traces) |
| Billing/quotas | none | usage metering |

The boundary is deliberate: everything needed to *build and run* an agent is
open source; the cloud sells *operating it at scale without ops*.

## Execution model

```
event ─► control plane (API) ─► run created ─► worker executes handler
                                                  │
                                       ctx.* steps journaled to the store
                                                  │
                                  approval? ─► run pauses (persisted) ──┐
                                                                        │ human approves
                                  resume ◄── replay handler, memoized ◄─┘
```

### One control plane serves many agents; one worker serves one

This used to say "one deployment serves exactly one agent", and it was the
sharpest constraint in the system. `build_app(root)` resolved a single
`rya.agent.yaml` at startup, so the `{agent_id}` in every route was **decorative**
— each handler resolved `manifest.name` regardless of what the path said — and
`POST /agents/{id}/versions` rejected any bundle declaring a different name.

**D21 removed that.** The api boots with no manifest and imports no agent. What
agents exist comes from `rya_versions` / `rya_environments`, and what each one
declares comes from the manifest persisted on its version record — so publishing
a second agent to a running deployment just works, and the name check is
obsolete rather than relaxed:

```bash
rya publish --url https://… --key … --env prod     # from project A
rya publish --url https://… --key … --env prod     # from project B, same deployment
curl …/agents                                       # both, independently promotable
```

The split is now between the planes rather than between deployments:

| | Agents per process |
|---|---|
| **api** (control plane) | many — it holds no manifest and runs no handler |
| **`rya worker`** (execution plane) | **one.** `load_agent` mutates `sys.path` and never unloads, so a process cannot hold two. D3: the process *is* the version |

So a second agent costs a second **worker**, not a second api, port, database or
bundle store. `rya worker --fork` (D27) does not change that count — a fork is
still one agent on one version — but it moves the import out of the long-lived
process into a warm interpreter it forks per run, which is what makes changing the
count later a configuration change rather than a rewrite. Addressing is D28: agent-scoped routes live under
`/agents/{agent_id}/…`, `_` is the reserved sole-agent alias, and the unprefixed
spellings still resolve while a deployment serves one agent — with `Deprecation`
and `Sunset` headers, and a 400 naming the candidates once it serves several.

> **Sharing one database is now sound.** Queue jobs carry their owning agent and a
> worker claims only its own (D22) — before that an unpinned worker would claim a
> sibling agent's `chat-turn` and execute it against its own handler. The same fix
> reached the `jobs`/`cron` primitive in Phase 3, where `claim_due_job` had taken no
> filter at all: a due job now records its agent, and a row written before that
> stays claimable by anyone so existing work keeps running. Guard and
> promotion-gate policy are keyed `guard:<agent>` / `promotion:<agent>` (D28), so
> two agents in one workspace no longer share one policy. A workspace that
> predates D28 keeps its unqualified row, which still governs every agent that has
> not been given its own — a rename would have to guess which agent a shared row
> belonged to, and getting that wrong leaves an agent silently ungated.

**One thing a single-tenant api still cannot do is *execute* every agent it
serves.** A published agent's code lives in a bundle the api has deliberately
never unpacked, so the inline worker only ever runs the mounted project. Every
other agent needs `rya worker`.

### Where the plane boundary actually falls

The boundary is about **importing code**, not about touching state.
`POST /agents/{id}/versions` runs in the api process: it verifies the uploaded
bytes, writes the archive to the bundle store, records the version and can flip an
environment pointer — without importing a single handler. The price is stated in
the response rather than hidden: readiness is not evaluated and no attestation is
filed (`"attested": false`), so an environment gated on readiness refuses the
promotion.

The two routes that *did* still execute handler code in the api — `POST
/agents/{id}/events` and `POST /approvals/{id}/approve` — no longer do:

- **`POST …/events`** writes a run row in `queued` state, **pinned to the version
  the environment points at**, and enqueues it. The caller gets a run id
  synchronously (so `GET /runs/{id}` answers straight away and the pin is
  auditable), the admission check still returns 429 to the caller rather than
  becoming a silently failed run, and a worker executes it. Deciding the pin in
  the control plane is the substantive change: the api used to enqueue unpinned,
  so *which code ran a request* was decided by whichever worker claimed it.
- **`POST /approvals/{id}/approve`** records the decision — the part that needs
  the authenticated human — and enqueues the resume, pinned to the run's own
  version. That is also what ends `E_JOURNAL_DRIFT` on a published run: the
  process continuing a run is on the same content hash as the one that paused it,
  by construction. **`/reject` stays synchronous**, because rejecting marks two
  records and appends a trace step; it runs no tenant code at all.

Both are gated on the same switch that already governed the background loops:
multi-tenant never executes here, and single-tenant does unless
`RYA_API_INLINE_WORKER=0`. A bare `rya serve` IS the whole deployment, and
silently ceasing to run anything would be a worse failure than the isolation gap
it closes. `rya dev` sets the flag and starts a real worker, so the local shape
matches production.

### Scaling and isolation

Workers scale horizontally: claims are atomic (`FOR UPDATE SKIP LOCKED`), so N
replicas never double-claim, and `--idle-exit` scales to zero.

**Per-run isolation now exists**, in two layers that arrived a phase apart.
`rya worker --fork` (D27) runs each item in a fork of a warm interpreter, so a
handler's blast radius is one process rather than the claimer. `RYA_BROKER=1` (D18)
takes the credentials out of that process entirely — it gets a Unix socket and a
capability that expires with the dispatch, and the database, the seal keys and the
pooled provider key stay in the claimer. The `docker`/`kubernetes` drivers put the
whole thing in a gVisor sandbox with no network route (D23/D24).

What that composes to is: **two tenants never share a sandbox, and the process
running tenant code holds nothing worth stealing.**

**How many sandboxes that is, is configuration.** `RYA_CLAIMER_SCOPE` (D27/#19-8b) is
`version` by default — one claimer per (workspace, agent, version), which is what a
worker has always been — or `tenant`, where one claimer serves everything a workspace
owns. At tenant scope the claimer peeks at the queue, warms the version the next item is
pinned to, and forks a child to claim it, so a tenant with five agents on two live
versions each is one sandbox holding ten warm interpreters rather than ten sandboxes.
Inside it, dispatches are shared equally across (agent, version) groups (D33), because
"one workspace must not starve another" reappears one level down as soon as a tenant's
own agents share a process.

The sandbox is a *pair* of containers, not one. The claimer holds the DSN, the seal key
and the pooled provider key, and runs the broker; the sandbox holds nothing and runs
`rya template-host`, which imports bundles on request and forks per run. Two Unix
sockets on a shared in-memory volume, pointing in opposite directions: the claimer
drives the host (control), and the host's forks call the broker (data). Neither lets
its caller become the other side.

One residual, and it is about cost rather than correctness: the gVisor numbers come
from a sentry nested in a privileged container, and nothing has been measured on
`x86_64`. The trusted posture — every self-host — deliberately runs none of this.

## Multi-tenancy (Postgres)

One deployed agent serves many isolated tenants. Enabled with
`RYA_MULTITENANT=1` (+ Postgres):

```bash
rya workspaces create acme            # a tenant
rya keys create --workspace ws_…      # a per-workspace API key (shown once)
# caller: Authorization: Bearer rya_sk_…  -> request scoped to that workspace
```

Isolation is enforced at two layers (see `tenancy.py`, `store_postgres.py`):

1. **App layer** — every store query filters by `workspace_id`.
2. **Database layer** — `tenancy.setup()` installs RLS + `FORCE` policies and two
   non-superuser roles; each plane connects as its own, with `app.workspace_id`
   set, so Postgres itself rejects cross-tenant rows — even an unfiltered
   `SELECT *` only sees the current tenant (`test_tenancy.py` proves this). API
   keys are stored only as SHA-256 hashes.
   - `rya_app` — the control plane.
   - `rya_worker` — the execution plane, built by `store.open_worker_store()`.
     Same RLS scoping, but **SELECT-only on the governance tables**
     (`rya_policy`, `rya_policy_log`, `rya_versions`, `rya_environments`), so the
     process that imports client bundles cannot write policy, flip an environment
     pointer or forge a version. Set `RYA_WORKER_DATABASE_URL` to give that
     process a least-privilege DSN *and no other* — otherwise it still holds the
     admin DSN in its environment, and superusers bypass RLS entirely.

Bundle archives are namespaced per tenant as well
(`<prefix>/<workspace>/<hash>.tar.gz`), which is what an IAM or bucket policy can
be scoped to. Content addressing means two tenants publishing the same bytes
derive the same hash, so a flat namespace made one tenant's archive readable by
anyone who could compute it. Single-tenant deployments normalise to the old flat
address and are unchanged.

## Layer map

```
┌─────────────────────────── managed cloud (proprietary) ───────────────────────────┐
│  auth · API keys · multi-tenancy (RLS) · hosted workers · remote MCP+OAuth ·       │
│  one-command deploy · dashboard · billing                                          │
├─────────────────────────────── OSS core (Apache-2.0*) ─────────────────────────────┤
│  runtime/engine · SDK (define_agent, ctx.*) · manifest · CLI (--json) ·            │
│  stdio MCP · skill · pluggable store (File | Postgres) · LLM provider seam · API   │
└────────────────────────────────────────────────────────────────────────────────────┘
```

\* **Committed.** An Apache-2.0 `LICENSE` has been in-repo all along, so the older
note here ("not yet committed in-repo — a product decision") was already wrong when
written. The product decision it referred to is now settled too: **D10** in
[PLATFORM_DESIGN.md](PLATFORM_DESIGN.md) selects Apache-2.0 for *both* halves of
the platform/SDK split, with copyright kept consolidated so a source-available
relicence stays available if a hosting threat ever appears.

Note that the layer map above is **superseded in one respect**: it places
multi-tenancy (RLS) in the proprietary managed-cloud box, but `tenancy.py` ships in
the OSS core under that same Apache grant. Under D10 there is no proprietary tier
to move it to — the boxes now describe *what we operate*, not *what is closed*.
