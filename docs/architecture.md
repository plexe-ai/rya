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

### One deployment serves exactly one agent

This is the sharpest constraint in the system and the easiest one to trip over.

`build_app(root)` resolves a single `rya.agent.yaml` at startup and imports one
agent from it; `rya worker` reads the same manifest to learn which agent and
environment it serves. So the `{agent_id}` in every route is **decorative** — each
handler resolves `manifest.name` regardless of what the path says.
`POST /agents/{id}/versions` is the one place that stops shrugging at this and
rejects a bundle declaring a different name, because a version filed under a name
this deployment does not serve would be listed by nothing and executed by nobody.

Serving a second agent means a second deployment: another api process, another
worker, its own mounted manifest, its own port. They can share one Postgres and one
bundle store — versions and environment pointers are keyed per agent — so the cost
is processes and ports, not infrastructure:

```yaml
rya-chat:       { environment: { RYA_PROJECT: ../agents/chat    }, ports: ["8787:8787"] }
worker-chat:    { command: ["rya","worker","--env","prod"] }
rya-support:    { environment: { RYA_PROJECT: ../agents/support }, ports: ["8788:8787"] }
worker-support: { command: ["rya","worker","--env","prod"] }
```

Multi-agent routing inside one process is not a configuration we have withheld; it
does not exist. Adding it means `build_app` stops resolving a single manifest,
which reaches every route, the console and the worker's version resolution.

### Where the plane boundary actually falls

The boundary is about **importing code**, not about touching state.
`POST /agents/{id}/versions` runs in the api process: it verifies the uploaded
bytes, writes the archive to the bundle store, records the version and can flip an
environment pointer — without importing a single handler. The price is stated in
the response rather than hidden: readiness is not evaluated and no attestation is
filed (`"attested": false`), so an environment gated on readiness refuses the
promotion.

Two routes do still execute handler code in the api process — `POST
/agents/{id}/events` and `POST /approvals/{id}/approve` — which the README's
honesty list tracks and `scripts/e2e_platform.py` asserts as open gaps.

### Scaling and isolation

Workers scale horizontally: claims are atomic (`FOR UPDATE SKIP LOCKED`), so N
replicas never double-claim, and `--idle-exit` scales to zero. What is still
missing is *per-run* isolation (Modal / Fly Machines / sandboxed containers):
today one worker process executes every workspace's handlers for its agent, which
is the hardening step owed before running untrusted multi-tenant code.

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
2. **Database layer** — `tenancy.setup()` installs RLS + `FORCE` policies and a
   non-superuser `rya_app` role; the data plane connects as that role with
   `app.workspace_id` set, so Postgres itself rejects cross-tenant rows — even an
   unfiltered `SELECT *` only sees the current tenant (`test_tenancy.py` proves
   this). API keys are stored only as SHA-256 hashes.

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
