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

### Self-host (OSS)

```bash
git clone <rya repo> && cd rya
cp .env.example .env          # optionally add ANTHROPIC_API_KEY
docker compose up             # Postgres + rya serve on :8787
```

That brings up Postgres and the single-worker runtime serving the mounted agent.
Point the `rya` service's volume at your own project to run yours.

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

## Execution model (today: single worker)

```
event ─► control plane (API) ─► run created ─► worker executes handler
                                                  │
                                       ctx.* steps journaled to the store
                                                  │
                                  approval? ─► run pauses (persisted) ──┐
                                                                        │ human approves
                                  resume ◄── replay handler, memoized ◄─┘
```

One worker process runs all agents (no per-tenant sandbox yet). Per-run
isolation (Modal / Fly Machines / sandboxed containers) is the next hardening
step before untrusted multi-tenant code.

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
