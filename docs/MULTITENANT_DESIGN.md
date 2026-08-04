# Multi-tenant, multi-agent — design

**Status: built, and as of Phase 6 launchable and measured.** Phases 0–6 of the plan
have shipped, so every decision here is in the tree: D19, D20, D22, D25, D26, D29 and
the whole of D21/D28 from Phases 1–3; D17, D18, D23, D24, D30, D31 from Phase 4; and
**both halves of D27** plus D32–D35 from Phase 5. Nothing on this page is still design.

**The two things that were code without evidence are now both closed, and each closed
by being run rather than by being argued.**

*gVisor has been run.* `scripts/verify_gvisor.sh` puts a real `runsc` sentry
(`release-20260727.0`) under the three D23 dependencies plus `os.fork`, and all of them
work — so D23's third-party-wheel criterion holds and §9's "psycopg / pydantic-core /
cryptography misbehaves under runsc" trigger did not fire. It also found the thing a
fixture could not: the isolation probe's `/proc/version` marker was the literal
`4.4.0`, a real sentry reports `4.19.0-gvisor`, and the miss was not a lost signal but
an **inverted** one — a genuine sandbox was actively refuted. It refused in exactly the
configuration the platform launches, because `--cap-drop=ALL` is what makes the *other*
signal unreadable. See §9 risk 0.

*The untrusted posture is launchable.* Phase 5 decided where the broker runs (D32) and
found that `sandbox_env` was written for the process that imports tenant code while
being applied to the process that has to hold the database credential — so a container
driver could never have run a working untrusted claimer. Phase 6 built the
independently-startable template host (`execution/host.py`, `rya template-host`) and
the drivers now render the pair, so `topology_supported` passes on `docker` and
`kubernetes`. It still refuses a driver that launches only the sandbox half, which is
the arrangement a third-party driver is most likely to write by accident. See §7.2.

**Both postures are live, and the difference is declared.** Without
`RYA_UNTRUSTED_TENANTS=1` a deployment runs the trusted posture — which is what every
self-host is, and what PLATFORM_DESIGN D13 describes. `rya posture` prints which one is
in force. Where this document and [PLATFORM_DESIGN.md](PLATFORM_DESIGN.md) disagree,
PLATFORM_DESIGN describes the trusted posture and this one describes the untrusted
posture that sits behind that flag.

See [MULTITENANT_PLAN.md](MULTITENANT_PLAN.md) for the phase-by-phase record of what
shipping each part actually cost.

**Sequencing lives in [MULTITENANT_PLAN.md](MULTITENANT_PLAN.md)** — phases, exit
criteria and re-plan triggers. This document holds the decisions and the reasoning;
that one holds the order. Keep them separate: a decision that changes should not
require editing a schedule, and vice versa.

**The requirement.** One deployment of the platform serves **many tenants**, and
each tenant deploys **many agents** of its own authorship. Tenants are
**untrusted** — self-serve signup, no contractual relationship, hostile until
proven otherwise. Each tenant gets its own bundle namespace.

**The consequence, stated up front.** This overturns D13. PLATFORM_DESIGN §12
risk 1 anticipated exactly this fork:

> Client code runs in a platform-operated process. Contained per D13 to a
> buggy-tenant threat model. **If a hostile-tenant requirement appears, node
> isolation is the answer and it is not in v1.**

That requirement has now appeared. The rest of this document is what follows from
it — and the honest headline is that **sandboxing, not multi-agent routing, is the
bulk of the work.** The routing is a refactor of code that exists. The isolation
is a new plane.

---

## 1. Where we are today

Verified against the tree at `track-a-core` (`aa8e3b3`). Three things are true at
once, and the middle one is easy to miss.

**The data model is already multi-tenant and multi-agent.** `workspace_id` is a
column on all 19 tables in `_DATA_TABLES` (`tenancy.py:36-42`). `agent` is a
column on `rya_runs`, `rya_sessions`, `rya_meter`, `rya_workers`, and is
`NOT NULL` with covering indexes on `rya_versions` and `rya_environments`
(`store_postgres.py:191-230`). `WorkerKey` is already
`(workspace, agent, version)` (`worker.py:61-72`). **The tree
`workspace → agent → environment` exists in the schema.** Nobody has to design
it.

**The control plane honours both dimensions. The execution plane honours
neither.**

| | `api` (control plane) | `worker` (execution plane) |
|---|---|---|
| Tenant | `Tenancy.resolve_key()` → workspace from API key | `--workspace` flag, default `"default"` |
| Store scoping | `PostgresStore(app_data_dsn, workspace_id, user_id)` (`api/app.py:200`) | `open_store(root)` → `PostgresStore(url)` → `workspace_id="default"` (`store.py:86`) |
| DB role | `rya_app`, RLS enforced | `RYA_DATABASE_URL` — the **admin** DSN, RLS bypassed |
| Agent | one, resolved at startup from a mounted manifest | one, from the same manifest |
| `multitenant_enabled()` | checked | **never called** — it appears only in `api/app.py` |

`tenancy.worker_dsn()` and the `rya_worker` role exist, are created, and are
granted table-by-table with a deliberate read-only split on governance tables
(`tenancy.py:119, 155-175`). **They have zero callers** — not in `src/`, not in
`tests/`, not in `docker-compose.yml`, not in the CloudFormation template. The
seam was cut and never connected.

So `rya worker --workspace acme` produces a `WorkerKey` labelled `acme` — used for
registration, `concurrency_key()` and `run["workspaceId"]` (`worker.py:441`) —
while the store reads and writes `workspace_id='default'` as admin. **`--workspace`
is decorative in the worker in exactly the way `{agent_id}` is decorative in the
routes** (14 of 88 routes carry it; every handler resolves `manifest.name`). Same
failure shape, same cause: a dimension present in the schema, honoured in one
plane, a label in the other.

None of this is a live bug. In the single-tenant posture that ships,
`workspace_id='default'` everywhere is correct and RLS is a no-op. It is unbuilt
for the posture described here, not broken in the one that exists.

---

## 2. The threat model, and what it invalidates

A hostile tenant's code executes inside a `worker` process. Assume it can run
arbitrary Python: read `os.environ`, open sockets, open files, and execute SQL on
any connection it can construct. Four current mechanisms stop being boundaries
under that assumption. Three of them are load-bearing today.

**2.1 RLS is not a tenant boundary against code that can execute SQL.** The
policy is:

```sql
CREATE POLICY ws_isolation ON <tbl>
  USING (workspace_id = current_setting('app.workspace_id', true))
```

— `tenancy.py:180-182`. `app.workspace_id` is a **session GUC**, set by the
application at connect time (`store_postgres.py:262`). Any role holding the
connection may call `set_config('app.workspace_id', 'victim-tenant', false)` and
re-scope itself. Postgres does not restrict custom GUCs to superusers.

This is not a flaw in `tenancy.py`. GUC-scoped RLS is exactly right for the `api`,
where the code setting the GUC is trusted platform code and the tenant never
executes. It is the wrong primitive the moment tenant code shares a process with
the connection. **RLS contains bugs. It does not contain code.**

**2.2 `RYA_SECRET_KEY` is a process-wide decryption key.** `seal.py` resolves a
single Fernet key from the environment and uses it to open vaulted connection
secrets. It is one key for the whole deployment, not one per tenant. Hostile code
reads `os.environ["RYA_SECRET_KEY"]`, and combined with 2.1 can fetch and decrypt
**every tenant's** sealed credentials.

**2.3 LLM provider keys are resolved in-process.** `_require_key(route,
"ANTHROPIC_API_KEY", …)` (`providers/llm.py:178-186`) materialises the key in the
worker's memory before the HTTP call. If the platform supplies a pooled key,
hostile code exfiltrates it and bills the platform.

**2.4 `guard.py`'s egress firewall becomes advisory.** It is described as "the
agent's egress firewall — every outbound call checked before it leaves the
process" (`guard.py:1`) and it default-denies (`guard.py:476`). But it is
**in-process**, on the `ctx` path. Hostile code writes `import urllib.request` and
never consults it. It remains valuable as governance and audit — "who approved
this allowlist change" (PLATFORM_DESIGN §12 risk 7) is a real product feature — but it cannot be
the enforcement point.

**2.5 The SDK contract actively invites what the threat model forbids.** D2's
leaf-tool rule says a tool handler "may do real IO (HTTP, DB, read env for
secrets) but must not call journaled `ctx` operations" (`sdk/agent.py:58`). That
is a *durability* contract, and it explicitly blesses raw IO and raw env reads.
Under untrusted tenancy the durability contract and the security contract point in
opposite directions. This is a documented API promise, so changing it is a
breaking change and needs to be planned as one.

---

## 3. The one boundary

PLATFORM_DESIGN §3's boundary is **platform code vs. client code**, drawn at the
leaf-tool rule. That boundary is about *what may be called*. It needs a second,
orthogonal one about *what may be reached*:

> **The tenant process holds no credentials and has no direct network path.**

Everything a handler needs from the outside world — state, LLM calls, secrets,
tool egress, object storage — is brokered by platform code the tenant cannot
reach, over a local channel, inside a sandbox the tenant cannot escape.

This is a stronger claim than "one process per tenant". Process separation already
exists and is insufficient, because the process itself is handed the DSN, the seal
key and the provider keys. **The isolation unit must be credential-free, not just
separate.**

---

## 4. Decisions

Continuing PLATFORM_DESIGN's numbering. D17 supersedes D13.

| # | Decision | Why |
|---|---|---|
| **D17** | **The threat model is a hostile tenant.** Supersedes D13 | self-serve signup means no contract and no recourse. §8's "per-tenant node pools or gVisor/Kata are the answer if that threat model changes" is now in scope, not deferred |
| **D18** | **The tenant process holds zero credentials.** No DB DSN, no `RYA_SECRET_KEY`, no provider key, no bucket credential. All state and IO go through a platform-side broker over a local socket | 2.1–2.3 are all the same bug: a secret in reachable memory. Removing the secrets kills the class, and does so independently of how good the sandbox turns out to be — defence in depth where the layers fail differently |
| **D19** | **RLS is retained as defence in depth, and is not the tenant boundary.** The broker is the boundary. GUC-scoped policies stay for the `api`, where they are sound | 2.1. Keeping RLS costs nothing and catches platform bugs; *relying* on it against tenant code would be a false claim of the kind §8 exists to avoid |
| **D20** | **Bundles are namespaced per tenant**: `<prefix>/<workspace_id>/<hash>.tar.gz`, with per-tenant credential scoping. Cross-tenant dedupe is forfeited | today's key is flat and content-addressed (`bundles.py:546-553`) with no workspace dimension at all. Content-addressing justifies dedupe; it does not justify a shared read namespace for tenant code |
| **D21** | **The `api` becomes agent-agnostic and manifest-free.** It boots with no `rya.agent.yaml`, learns tenants from `tenancy` and agents from `rya_versions`/`rya_environments` | `build_app(root)`'s single `load_manifest` + `load_agent` is the root of the one-agent limit. It also means the control plane stops importing tenant code entirely, which D13→D17 requires |
| **D22** | **`agent` is a first-class filter on queue claim**, alongside the existing type and version filters. **Both dispatch surfaces**: `rya_queue` (turns, resumes) and `rya_jobs` (the `jobs`/`cron` primitive) | `queue.claim` filtered on `types` + `version_id` only (`queue.py:150-183`), so an unpinned worker — "claims anything" — would execute another agent's `chat-turn` against its own handler. Under D17 that is a cross-tenant path, not a mixed-up one. **Phase 3 found the same hole in `claim_due_job`**, which took no filter at all and whose rows recorded no agent: either worker claimed either agent's due job and `run_job` raised `E_HANDLER_NOT_FOUND` on the one it could not serve, while `queue_depth` counted every sibling's job as its own and so never went idle. Surfaced by the supervisor needing something to route a due job on |
| **D23** | **Isolation is gVisor (`runsc`), because it is the only sandbox that is substrate-portable** — a drop-in OCI runtime requiring no virtualization support | D1 promises "one deployment, one topology, in our infra, a customer's, or a laptop", so the sandbox may not depend on one cloud. Kata needs KVM; Firecracker needs `/dev/kvm`; Fargate's microVM needs Fargate. Only `runsc` runs identically on any Linux host, in plain Docker, and in Kubernetes via `RuntimeClass`. See §7. **Confirmed by measurement (§11): ~1.5× on syscall-heavy work, 40–95 ms bring-up, 67% of budget in the worst case** |
| **D24** | **Egress is enforced at the network layer.** `guard.py` is retained for governance, audit and grounding, and is no longer described as enforcement | 2.4. The allowlist is the right *policy*; an in-process check is the wrong *mechanism* once the process is hostile |
| **D25** | **A supervisor owns the fleet.** Worker processes are started, scaled and reaped by platform code, never by a compose file or a human. **The scheduling policy is ours; only the launch mechanism is pluggable** | §6 is explicit that this is the greenfield half, and N tenants × M agents × V versions cannot be statically declared. §12 risk 3 warns specifically against underestimating it. Delegating policy to a substrate scheduler (KEDA on k8s, autoscaling on ECS) would mean one scheduler per substrate — three behaviours, three test matrices — for a component §9 already calls the subtlest here |
| **D26** | **An `ExecutionDriver` seam separates scheduling policy from launch mechanism.** Drivers: `local`, `docker`, `kubernetes`, `ecs`. **Each declares its isolation level, and untrusted tenancy on a driver that declares less than `sandboxed` is a startup failure** | the third instance of a pattern the codebase already has twice — `open_store()` is "the seam that makes the OSS self-host and the managed cloud the same code" (`store.py:69-91`) and `resolve_bundle_store()` already spans local/S3/MinIO/Ceph/R2 (`bundles.py:570`). Declared isolation turns §9 risk 5's two postures from prose into something `preflight` can refuse, the same way it already refuses a handler-set hole |
| **D27** | **Runs execute in a fork, not in the claimer** — a bundle cache plus a hash-keyed warm interpreter pool. **Claimer scope is configuration**, starting at `(workspace, agent, version)` and widening to per-tenant when the idle tail justifies it | the lock-in is fork-per-run vs import-at-startup, *not* tenant-vs-triple. Build the fork and the scope becomes a config change; build today's import-at-startup and widening later is a rewrite of `worker.py`. Starting narrow keeps `preflight`'s before-claiming fail-closed guarantee and per-agent resource limits; widening later collapses sandbox count from the N×M×V product to active tenants. **D3 survives either way**: the forked process is still one agent on one version. See §7.1. **Confirmed by measurement (§11): fork+import is 6.6–13.4 ms real / 791 ms worst case under gVisor, so the wide scope is reachable — and the pool is a *requirement* under D23, not an optimisation** |
| **D28** | **Route addressing: derive from the row where an id determines the agent; move collections and singleton configs under the existing `/agents/{agent_id}/…`; keep `/queue/*` worker-facing with `agent` as a filter.** Guard and gate policy keys become agent-qualified | a redundant path segment is a second source of truth needing a reconciliation check, and `app.py:1565` is the one we are trying to retire, not a pattern to spread. Reusing the existing prefix avoids a second addressing convention. See §8 |
| **D29** | **An `organization` is the billing entity and owns many workspaces. The isolation boundary stays `workspace_id`; the billing boundary becomes `org_id`** | answers open question 2. Enterprise invoicing, cross-workspace quota and org-wide SSO need a unit above the workspace, and `tenancy.py:259` already models users across workspaces. The cost is that two boundaries must be kept straight — resolved by the split rule below |
| **D30** | **The platform pools the provider key.** Inference is metered per workspace, budgeted per org, and billed. Provider credentials never enter a tenant process | answers open question 1. It makes the LLM proxy a **hard secrecy boundary** rather than a convenience, promotes it alongside the broker (PLAN §9 re-plan trigger, fired), and makes per-tenant quota a launch requirement: with a pooled key, an unbounded tenant spends *our* money, so theft-of-service is a billing control, not an abuse nicety |
| **D31** | **Tenant deletion is two-phase: `disable` immediately, `purge` after a retention window, with crypto-shredding as the primary mechanism** | answers open question 3. RLS makes reads safe and says nothing about erasure. Destroying a per-tenant seal key (D18/#13) makes sealed data unreadable in O(1) instead of chasing rows, and bundle objects are only enumerable once keys carry the workspace prefix (D20/#7) — so **#7 and #13 are prerequisites for a complete purge**, a dependency the plan did not previously show |
| **D32** | **The broker is a *sibling* of the tenant process, never its parent, and never inside the tenant's own container.** The unit a driver launches for the untrusted posture is therefore a **pair**: a credentialed claimer beside a credential-free sandbox, sharing the socket over an in-memory volume | answers open question 8. Phase 4 left two topologies disagreeing, and Phase 5 found the disagreement was not cosmetic: `sandbox_env` builds an environment with no DSN, so the container it configures **cannot be the claimer** — a claimer with no DSN opens a FileStore in its own container, claims nothing, and reports idle ticks indistinguishable from "no work to do". The strongest form needs an independently-startable *template host* inside the sandbox, which is not built; until it is, `topology_supported` refuses the untrusted posture on a container driver rather than starting something that looks healthy and serves nothing. See §7.2 |
| **D33** | **Fairness inside a tenant is equal dispatches per (agent, version) group, not depth-weighted.** A group with nothing pending is not a candidate at all | the wide claimer scope (D27/#19-8b) reopens `concurrency_key`'s question one level down: five of a tenant's agents behind one claimer share a dispatch budget, and serving strictly by depth makes a sibling with one item wait for a backlog of forty. A deficit or weighted-fair scheme needs a per-item service-time estimate, and the platform does not have one — turn durations span a mocked reply and a ten-minute tool loop, which is the same reason `backlog_per_worker` is a queue-length heuristic. Equal turns is the strongest thing that can be said honestly, and throughput is redistributed rather than lost |
| **D34** | **A supervisor is a singleton per workspace, enforced by a lease rather than by convention.** A supervisor that cannot take the lease keeps observing and planning and applies nothing | answers open question 7. The obvious way to run one on Kubernetes is a Deployment, a Deployment is scalable by default, and a second replica does not merely duplicate work: `observe` reconciles the registry against the *driver's* inventory, and a second supervisor's driver inventory is empty — so each replica believes it is the only one and the fleet converges on 2N. Per *workspace* rather than per process, which also lets two supervisors split a hundred tenants instead of one idling |
| **D35** | **An org budget is enforced through a derived per-workspace verdict, computed by a privileged reconciler — never by a cross-tenant read on the admission path** | D29 makes the org the billing boundary, and summing an org's meter needs a connection that spans workspaces. Putting one on every tenant's admission path would hand the hot path of every run a credential that can read every other tenant, which is precisely what Phase 4 spent itself removing from a far less privileged process. So the aggregate is computed outside the tenant plane and only its *verdict* is written into each member's own policy row. The cost is staleness bounded by the reconciler's interval, which is the same trade §11.12 already made for token limits with a narrower bound |

### The D29 split rule

Two boundaries now exist, so name which one each concern follows. Getting this
wrong is how a billing change quietly becomes an isolation bug.

| Follows `workspace_id` (isolation) | Follows `org_id` (billing) |
|---|---|
| RLS policies and the `rya_worker` role (#5) | invoices and payment methods |
| bundle namespace (D20/#7) | quota **budgets** and spend caps |
| per-tenant seal keys (#13) | org-wide SSO and admin roles |
| sandbox and claimer scope (D26/D27) | cross-workspace usage rollups |
| guard and gate policy (D28) | contract and plan tier |

Metering rows carry **both**: attributed at `workspace_id`, aggregated at
`org_id`. `org_id` is added as a nullable FK on workspaces and backfilled
one-org-per-workspace, so no existing boundary moves on day one.

---

## 5. Target topology

```
                       ┌─────────────────────────────────────┐
   tenant CLI ────────►│  api  (control plane)               │
   rya publish         │  no manifest, no tenant code (D21)  │
                       │  Tenancy + RLS as rya_app           │
                       └───────────────┬─────────────────────┘
                                       │ enqueue (D4)
                       ┌───────────────▼─────────────────────┐
                       │  supervisor (D25) — policy, ours    │
                       │  watches claimable depth per tenant │
                       └───────────────┬─────────────────────┘
                                       │ ExecutionDriver (D26)
                          local │ docker │ kubernetes │ ecs
                                       │ start / stop / list
   ╔═══════════════════════════════════▼═════════════════════╗
   ║  sandbox — gVisor (D23). Scope is CONFIG (D27):          ║
   ║  narrow = per (ws, agent, version) · wide = per tenant   ║
   ║  ┌──────────────────────┐   ┌────────────────────────┐  ║
   ║  │ claimer (platform)   │──►│ broker (platform code) │──╬──► Postgres
   ║  │  ├─ fork: agent/v7   │   │ holds the DSN, seal key│  ║    LLM providers
   ║  │  ├─ fork: agent/v8   │◄──│ and provider keys      │──╬──► allowlisted egress
   ║  │  └─ fork: billing/v2 │   └────────────────────────┘  ║
   ║  │  NO credentials (D18), no network (D24)           │  ║
   ║  └──────────────────────┘                              ║
   ╚═════════════════════════════════════════════════════════╝
```

The broker and the driver seam are the new components. Everything else exists in
some form.

The three forks shown are the **wide** scope — one tenant's agents and versions
sharing a sandbox, which is D27's endgame. At the **narrow** scope this sandbox
holds forks of a single agent-version and there are three such sandboxes instead.
Either way the fork is where tenant code runs, and either way **two tenants never
share a sandbox** — that is the boundary that has to hold. `acme/support/v7`
sharing a Sentry with `acme/billing/v2` is the same trust domain.

**Why a broker rather than one DB role per tenant.** Role-per-tenant fixes 2.1
(bind the policy to `current_user` instead of a settable GUC) and nothing else —
the seal key and provider keys are still in reachable memory, and egress is still
unmediated. The broker fixes all four, and it is the same shape the `ctx` surface
already has: a mediated API in front of state. Role-per-tenant is a reasonable
*additional* layer under D19 and a poor substitute for D18.

---

## 6. What must be built

Ordered so each item ships independently and the early ones are worth doing even
if the rest never happens. Items 1–3 improve the platform as it exists today.

1. **Connect the execution plane to tenancy.** Wire `worker_dsn()`/`rya_worker`,
   make `--workspace` real, have the worker consult `multitenant_enabled()`. The
   role, the grants and the governance-table read-only split already exist
   (`tenancy.py:155-175`); nothing calls them. **S.** Highest value per unit of
   work on this page, and it makes the *current* multi-tenant mode sound.
2. ~~**Land D22**~~ **BUILT (Phase 1 for `rya_queue`, Phase 3 for `rya_jobs`)** —
   `agent` on the claim path and on the enqueued row. `idx_queue_claim` was already
   `(workspace_id, status, run_at)`, so item 1 covered the tenant axis for free. The
   `jobs` half was missed the first time because the primitive looks unrelated to
   chat dispatch; it is the same hazard on a different table.
3. **Land D20** — workspace-prefixed bundle keys and per-tenant credential
   scoping. **S.** Needs a migration plan for existing flat keys.
4. **Persist the manifest on the version record.** `create_version` keeps only
   `manifestVersion`, as "a LABEL only" (`deployments.py:136-149`). D21 needs the
   tool list and channel config from the database rather than from a mounted file.
   `rya_versions.data` is JSONB, so this is additive. **S.** Also the prerequisite
   for preflighting a handler set *before* claiming once D27's scope widens — see
   §7.1, where that is the one guarantee the wide scope gives up.
5. **Decide route addressing, then land D21.** 88 routes: 14 carry `{agent_id}`,
   21 are platform-level, **53 are agent-scoped with no agent in the path**. They
   split three ways and the split does most of the work — see §8. **L.**
6. ~~**Build the broker (D18).**~~ **BUILT (Phase 4)** — `broker/`, plus `keys.py`
   for the per-tenant seal keys and the LLM proxy as a broker *service*. The `ctx`
   surface was the natural interface and turned out not to be the whole one: D27 puts
   the claim loop inside the fork, so what had to be mediated is also what
   `turns.execute_pending` and `Engine.work_once` call. `queue.claim` applies D22's
   agent filter *in the caller's process*, which is fine when the caller is the
   platform and worthless when it is a fork holding tenant code — so claiming, and the
   three other lease verbs, became services with every identity argument forced
   platform-side. The mediated claimer is consequently **more** constrained than an
   unmediated one. The D2 contract change shipped as a deprecation on
   `agent.tool`'s docstring, with `ctx.egress` as the sanctioned replacement.
7. ~~**Define the `ExecutionDriver` seam (D26)**~~ **BUILT (Phase 3)** —
   `execution/drivers.py`. The `local` driver, per-driver cold-start targets, and
   `require_isolation_for_tenancy`, which refuses untrusted tenancy on a driver
   declaring less than `sandboxed`.
   - 7b. **Phase 5 added a fourth thing a driver declares:** ``launched_unit`` — is
     the process it starts the *claimer* (holds credentials, spawns templates) or the
     *sandbox* (holds nothing)? The seam needed it because `sandbox_env` and
     `worker_env` want opposite environments and both were reaching the same
     container. See D32 and §7.2.
8. ~~**Land D27's fork-per-run execution**~~ **BUILT at the narrow scope
   (Phase 3)** — `execution/pool.py` plus the `InlineExecutor`/`ForkExecutor` split
   in `worker.py`. `rya worker --fork` claims without importing; a warm template per
   bundle hash forks a child per run. The bundle cache already existed
   (`resolve_bundle_root`'s unpacked, content-addressed tree). The measurement that
   gated it was taken in Phase 0 (§11).
   - 8b. ~~**Widen the scope to per-tenant**~~ **BUILT (Phase 5)** —
     `execution/scope.py`, `RYA_CLAIMER_SCOPE=tenant`, one key spelled `ws:*:*`.
     It *was* a config change, which is the claim item 8 was built to make true. What
     it cost beyond that was the three things the narrow scope got for free: the
     preflight ordering (peek → warm → fork, and §7.1's concession turned out to be
     unnecessary), fairness between a tenant's own agents (D33), and one broker
     resolving config per agent. Sandbox count now tracks active tenants.
9. ~~**Build the sandbox (D23) and network egress enforcement (D24)**~~ **BUILT
   (Phase 4), RUN (Phase 6)** — the `docker` and `kubernetes` drivers ask for `runsc`
   and a gVisor `RuntimeClass`, resource limits and the hardened defaults apply at
   every level, and `egress.py` carries the network verdict and the divergence
   reconciler. Phase 6 executed it: `scripts/verify_gvisor.sh` puts a real
   `release-20260727.0` sentry under `cryptography`, `pydantic-core`, `psycopg`,
   `yaml`, `httpx` and `os.fork`, and all six pass — so the criterion is **closed**.
   Running it also broke the isolation probe's positive path, which had been tested
   against a fixture with the wrong kernel string; see risk 0.
    - 9b. **Phase 6 built the pair (D32).** `execution/host.py` and
      `rya template-host`: a credential-free template host inside the sandbox, so the
      claimer no longer has to be the tenant process's parent and can hold the DSN in
      a container of its own. `topology_supported` passes on both container drivers.
10. ~~**Build the supervisor (D25).**~~ **BUILT (Phase 3)** —
    `execution/supervisor.py` and `rya supervisor`. `observe` → `plan` → `apply`,
    with `plan` pure so scheduling questions are testable without launching
    anything. It also needed a prerequisite this list did not name: `lastHeartbeatAt`
    was written and never read, so there was no signal to reap or replace on — and
    `quotas` believed it, leaking a `maxWorkers` slot per crash.
    - 10b. **Phase 5 gave it a lease (D34)** and a *passive* mode, because the
      `kubernetes` driver made "two supervisors" the default way to deploy one. Also
      `_tenant_targets`: one key per workspace at the wide scope, which is where the
      N×M×V collapse actually becomes a number.
11. **(Phase 5) Org-aggregated usage and budget (D29/D35)** — `orgs.py`,
    `rya orgs`, and the `org` block on `/quotas` and `/usage`. The interesting part is
    the shape rather than the arithmetic: the rollup is computed with the admin DSN
    *outside* the tenant plane and only its verdict is written into each member
    workspace's own policy row, so no tenant's admission path holds a credential that
    can read a sibling.
    - 11b. **Phase 6 gave it a scheduler.** `supervisor.reconcile_orgs`, on the
      multi-workspace fan-out rather than inside `Supervisor.tick` — a `Supervisor` is
      scoped to one workspace and an org rollup spans them, so putting it on the tenant-
      scoped object would have handed that object the cross-workspace read D29 keeps
      out of it. Also `orgs.freshness`, which reports a verdict nothing is refreshing;
      that is the half that works on a deployment with no supervisor at all.

**Do not read items 1–4 as progress toward the hosted product.** They are four
small changes that make the existing posture correct. Items 6–11 are the product,
and they are each larger than 1–5 combined. This is the same warning PLATFORM_DESIGN §12 risk 3
gives about the deployment pipeline, and it applies here for the same reason.

---

## 7. Sandbox alternatives considered

**Portability is the deciding criterion, not maximum isolation strength.** D1
promises "one deployment, one topology, in our infra, a customer's, or a laptop",
and §8 makes self-hosting a residency control. A sandbox that only exists on one
cloud forfeits both. So the question is not "what contains an escape best" — several
options do — but "what contains an escape on *any* substrate we must support".

That reorders the table. What each option demands of the host:

| Option | Contains kernel escape | Requires of the host | Portable to |
|---|---|---|---|
| Bare container (namespaces + seccomp) | **No** — shared kernel | nothing | everywhere, but fails the requirement |
| **gVisor `runsc` (D23)** | Yes — userspace kernel | Linux 4.14+, **no virtualization** | any Linux host; plain Docker via `--runtime=runsc`; k8s via `RuntimeClass`; EKS/GKE/AKS/on-prem |
| Kata Containers | Yes | **KVM** | bare metal or nested-virt instances only |
| Firecracker | Yes — strongest | **`/dev/kvm`** | bare metal / specific instance families |
| Fargate microVM | Yes — strongest | **being on Fargate**, and `runsc` cannot nest inside it | AWS only |
| WASM | Yes | nothing | **rules out the Python ecosystem** — non-starter for an SDK whose value is `pip install` |

gVisor wins on one property the others lack: it is a **drop-in OCI runtime that
needs no special hardware**, so the same image and the same driver code run on a
developer's Linux box, a customer's on-prem cluster, and any managed Kubernetes.
Kata and Firecracker isolate at least as well and are strictly less portable.

**Bare containers are excluded on evidence, not principle.** Namespaces, cgroups
and seccomp all sit on *one shared kernel*, so tenant syscalls reach the host
directly. `runc` alone has had CVE-2019-5736 (host binary overwrite via
`/proc/self/exe`) and CVE-2024-21626 (fd leak into the host mount namespace), and
host-wide kernel bugs such as Dirty Pipe were reachable from inside a container.
Hardened Docker — userns-remap, all capabilities dropped, read-only rootfs, tight
seccomp — is materially better than the default and is the right configuration for
the `docker` driver. It still cannot contain a kernel bug, which is why it declares
`shared-kernel` under D26 rather than `sandboxed`.

**Fargate is available, not depended on.** Fargate gives per-task microVM
isolation for free, which satisfies D23's requirement by a different mechanism —
so the `ecs` driver declares `microvm` and skips `runsc` entirely. That is exactly
what the D26 seam is for. What Fargate cannot be is *the* design: task launch is
tens of seconds against a `COLD_START_TARGET_MS` of 2000 (`worker.py:51`), it
offers no bin-packing, and it is one cloud.

**Still the decision most likely to be wrong**, and the reason has not changed:
these figures are published vendor numbers, none measured here. A real measurement
of `runsc` start + bundle materialisation + interpreter start + import against the
2s budget should precede implementation.

### 7.1 The execution unit (D27), worked

The choice is *not* "how strong is the sandbox" but "how many sandboxes does the
supervisor have to manage". Same scenario, both options:

> **`acme`** — agents `support` (v7 in prod, v8 just promoted) and `billing` (v2).
> 40 turns queued for `support`, 2 approvals paused mid-run on v7.
> **`globex`** — agent `chat` (v3), idle one hour.
> **`initech`** — agent `triage` (v1), one turn just arrived.

**Option A — sandbox per `(workspace, agent, version)`** (today's `WorkerKey`):

| Key | Sandboxes | Why |
|---|---|---|
| `acme/support/v8` | 1 → 4 under load | the 40 queued turns |
| `acme/support/v7` | 1 | the 2 paused approvals must replay on v7 (D12) |
| `acme/billing/v2` | 1 | its own traffic |
| `globex/chat/v3` | 0 | idle-exited |
| `initech/triage/v1` | 1 | full cold start for a single turn |
| **total** | **7** | |

The rollout is the sharp edge: `support` needs v7 *and* v8 alive simultaneously,
and will until the last v7-pinned run drains. Every promotion transiently doubles
a key. A tenant with 5 agents × 2 live versions is 10 sandboxes while active.

**Option B — sandbox per tenant, fork per run (D27):**

| Tenant | Sandboxes | Contents |
|---|---|---|
| `acme` | 1 → 3 under load | forks for `support/v8` ×N, `support/v7` ×2, `billing/v2` |
| `globex` | 0 | idle-exited |
| `initech` | 1 | one fork |
| **total** | **4** | and it does not grow with agents or versions |

The container runs a **generic claimer** scoped to one tenant: claim any job for
`acme`, read the job's pinned `versionId`, materialise that bundle from a local
cache, fork an interpreter, run, discard. The v7/v8 overlap costs nothing — it is
two forks in one sandbox. A warm interpreter pool per hot version keeps the
per-run cost off the critical path, the same pre-fork trick a WSGI server uses.

| | Option A | Option B |
|---|---|---|
| Sandbox count | ∝ active (tenant, agent, version) | **∝ active tenants** |
| Rollout overlap | doubles a key | free |
| Old-version approval resume | needs a dedicated sandbox | a fork |
| Supervisor signal | depth per triple — many keys, `GROUP BY` on job metadata | **depth per tenant — one number each** |
| Brokers | one per triple | **one per tenant** |
| Per-run cost | none — process already warm | fork + import, needs a warm pool |
| Crash blast radius | one (agent, version) | one tenant's in-flight work |
| **`preflight` fail-closed** | **at startup, before claiming** | after claiming, in the fork |
| **Resource limits** | **per agent-version** | per tenant |
| Delivery risk | incremental — today's worker in a sandbox | new execution mode, cache, pool, fairness |

### Two things Option A does better

**1. `preflight` fails closed before claiming.** Today a worker "refuses to start
when the manifest declares a tool it cannot serve, so 'the image is missing a
handler' is a startup failure rather than a mid-run one", and `preflight` runs
"BEFORE claiming anything" (`worker.py:207`). A generic per-tenant claimer cannot
do that — it does not know at startup which handlers it will need. **The guarantee
degrades from "before claiming" to "after claiming, in the fork"**, which is the
mid-run failure the current design deliberately eliminated. It is recoverable by
preflighting from the version record's manifest before claiming, which makes item 4
(persist the manifest) more load-bearing than it first appears — but it is a
regression to design around, not a free win.

**2. Per-agent resource limits.** Option A can cgroup a runaway `billing` agent
without touching `support`. Option B contends within a tenant. For a product sold
on governance, per-agent limits are arguably a feature rather than overhead.

### The observation that resolves it

There is a critique of Option B worth conceding: if the warm pool works, B
converges to *"A's process model, nested inside a per-tenant container."* A warm
process per hot version **is** Option A.

That is the answer rather than an objection. B is not a different process model —
it is the same one with the sandbox boundary moved:

| | Process granularity | Sandbox granularity |
|---|---|---|
| A | one agent-version | one agent-version |
| B | one agent-version *(same)* | **one tenant** |

The expensive thing was never the process. It is the sandbox: image pull, `runsc`
start, network setup, and a scheduling decision. Those are what scale with the
N×M×V product, and those are what B collapses.

### Decision: B's mechanism, A's initial scope

The lock-in is **fork-per-run vs import-at-startup**, not tenant-vs-triple.

- **Build fork-per-run, the bundle cache, and the hash-keyed pool from day one.**
  This is the load-bearing mechanism and the expensive thing to retrofit.
- **Make claimer scope configuration**, and ship it scoped to
  `(workspace, agent, version)`. Startup `preflight` and per-agent limits are
  retained, and version one behaves like today's worker — materially lower
  delivery risk for the component §9 already calls the subtlest here.
- **Widen the scope to per-tenant** when the idle tail costs more than per-agent
  governance granularity is worth. That is then a config change.

Build A's shape instead — one long-lived import bound to one version at startup —
and widening later is a rewrite of `worker.py`.

**Where this lands if the tail forces one answer: per-tenant.** Self-serve signup
with untrusted tenants is the regime where a long tail of mostly-dormant agents
makes sandbox count the dominant cost term, which is what §6 opens by naming as
*the* constraint. Option A's steady state is the enterprise shape — few tenants,
each busy, few live versions — and that is not the product D17 describes.

**What would reverse it:** if fork + import for a realistic agent lands near the 2s
budget even with a warm pool, B's advantage erodes and A's simplicity wins. That
measurement is cheap, needs none of the sandbox or driver work, and can be run
against the worker as it stands today. **It should be run before item 8 starts.**

**What D27 does not change.** D3's reasoning holds exactly as written — a
version-pinned run still executes in a process that loaded only that version,
because `load_agent` mutates `sys.path` and never unwinds it
(`runtime/engine.py:79-81`). D27 decouples the *container* from the process; it
does not put two agents in one interpreter.

**Shipped at the wide scope in Phase 5, and the prediction held with one
correction.** Widening was configuration: `RYA_CLAIMER_SCOPE=tenant`, a key spelled
`ws:*:*`, and the same drain loop. What §7.1 got wrong was the concession — it said
the `preflight` guarantee "degrades from *before claiming* to *after claiming, in the
fork*". It does not have to. The claimer **peeks** at the queue (a read, not a
claim), resolves the version the next item is pinned to, warms that version's
template — which is the import, and therefore the preflight — and only then forks a
child that claims. The guarantee is preserved by *ordering*, and the peek being
allowed to be stale is what makes that safe: the fork's claim is still atomic and
still filtered to its own group, so a sibling claimer taking the item first costs
nothing.

### 7.2 Where the broker runs (D32), worked

The wide scope forced this. One claimer serving many agents means one broker serving
many agents, which is a small change (per-agent config resolution) — and it put the
question "what process is the broker in" in front of the question "what container is
that process in", where the answer turned out to be wrong.

| | Broker location | Boundary between tenant and credentials | Buildable |
|---|---|---|---|
| **weak** | parent process of the template, in the tenant's container | a process boundary, same uid | today |
| **good** | a sibling container in the same sandboxed pod | a container boundary, different uid, no shared PID namespace | needs a template host |
| **best** | outside the sandbox entirely | the sandbox boundary itself | needs a template host + a control channel across it |

**What blocks both of the good options is one thing:** the claimer *spawns* the
template with `multiprocessing`, so the template is necessarily its child, so the two
are necessarily in the same container. Moving the broker out means the template must
be startable independently and reachable over a socket.

**And a container of templates is not one template.** The obvious fix — one sandbox
container per bundle, launched with the bundle as its entrypoint — reintroduces
exactly what D27 collapsed, because the warm pool holds one interpreter per hot
version and those would become one *container* per hot version. So the sandbox
container has to run a **template host**: a small platform-trust process, holding no
credentials, that accepts "import this bundle and serve forks of it" over the socket.
Platform code with no credentials is safe inside the sandbox by construction, which
is what makes the arrangement work at all.

### 7.3 The template host, built (Phase 6)

`execution/host.py` and `rya template-host`. The launched unit for a container driver
is now the **pair**, and `topology_supported` passes on it:

```
┌─ claimer container ────────────┐   ┌─ sandbox container (gVisor) ──────────┐
│ rya worker --fork              │   │ rya template-host                     │
│  · holds the DSN, seal key,    │   │  · holds NOTHING                      │
│    pooled provider key         │   │  · spawns templates, forks per run    │
│  · runs the BrokerServer  ─────┼──▶│                                       │
│  · mints one capability/fork   │◀──┼──── the forks call back to the broker │
└────────────────────────────────┘   └───────────────────────────────────────┘
   broker.sock and host.sock, both on the shared in-memory volume /run/rya
```

**Two sockets, pointing opposite ways, and that is the whole design.** The claimer
drives the host (control: start a template, fork it, stop it) and the forks call the
broker (data: claim, journal, model). Neither socket lets its caller become the other
side. Multiplexing both over one socket would mean one authorisation surface for two
audiences, which is how a control op ends up reachable with a dispatch capability.

**Four properties worth stating, because each was a choice:**

- **The host is credential-free by wire format, not by filtering.** Its start request
  has no field for a state root, so there is no value the claimer *could* send that
  would give it a database. That is stronger than "we remember not to send one".
- **The host does not mint capabilities**, it forwards them. The HMAC secret never
  leaves the broker, so a compromised host can replay a live capability into the
  dispatch it was already given and forge nothing — the same bound a compromised
  *template* already has, and templates run tenant code.
- **`WarmPool` did not change.** Same keying, same LRU, same eviction; one branch
  picks a `HostedTemplate` instead of a `WarmTemplate`. That is the second test of
  whether D27 keyed the right thing, and it passed the same way widening the scope did
  in Phase 5: content-addressing decouples *which* interpreter serves what from
  *where* it lives.
- **Both postures get the pair.** The alternative — one container for trusted
  deployments, two for untrusted — is a second launch path that only the less-tested
  posture exercises, and the trusted single container was never correct either: it was
  configured by `sandbox_env` and had no DSN.

What is *not* deferred and never was: the k8s pod's `emptyDir` was already the right
mechanism and is unchanged. What it lost in Phase 5 was a comment claiming a sidecar
the function did not render; what it gained in Phase 6 is the sidecar.

**What this does not reach is D32's "best" row.** The broker is a sibling container,
not a process outside the sandbox entirely, so the boundary is a container boundary
with a different uid and no shared PID namespace — good, and not the sandbox boundary
itself. Getting the last row needs a control channel *across* the sandbox, which is a
network listener where there is currently a Unix socket, and a listener a tenant's
neighbours can reach is a worse trade than the one being made.

---

## 8. Route addressing — decided (D28)

Item 5's real cost, and the one design decision that blocks the most work.
**Closed in Phase 0.** The inventory below is generated from `api/app.py`, not
estimated: **90 route decorators, 14 already carrying `{agent_id}`, 76 without.**

Five rules cover all 76. The organising principle is *derive where an identifier
already determines the agent; address explicitly only where nothing does* —
because a redundant path segment is a second source of truth, and a second source
of truth needs a reconciliation check. That check already exists once, at
`app.py:1565`, and D21 is supposed to retire it rather than propagate it.

| Rule | Applies to | Change |
|---|---|---|
| **1. Derive from the row** | `/runs/{run_id}`(+trace), `/sessions/{session_id}`(+messages), `/versions/{version_id}`(+4), `/approvals/{approval_id}/*`, `/files/{file_id}`, `/queue/jobs/{job_id}/*` | **No API change.** The id is globally unique and the record names the agent. Authorization checks the workspace; the response gains an `agent` field so callers stop inferring it |
| **2. Address explicitly** | `/tools`(3), `/models`, `/evals`(2), `/knowledge`(2), `/channels`, `/guard`(4), `/gate`(3), `GET /sessions`, `GET /approvals`, `/inbound`, `/slack/events`, `POST /files` | Move under the **existing** `/agents/{agent_id}/…` prefix. These mean "for *the* agent" and have nothing to derive from |
| **3. Workspace-level** | `/workers`, `/usage`, `/quotas`, `/connections`, `GET /files`, `/files/presign` | Stay unprefixed, become workspace-scoped. Not agent concerns |
| **4. Worker-facing** | `/queue/*` (11) | Stay unprefixed. D14 deliberately keeps this SDK-free for foreign consumers. `agent` arrives as a **filter** (D22), not a path segment — the queue is the platform's dispatch surface, not a per-agent API |
| **5. Unchanged** | `/`, `/console`, `/console.html`, `/v2`, `/favicon.ico`, `/lucide.min.js`, `/healthz`, `/v1/*` (14) | Static, health, and the tenancy/identity surface, which is already workspace-addressed |

**Rule 6 — a time-boxed single-agent fallback.** During Phase 2, an unprefixed
Rule-2 route on a deployment serving exactly one agent resolves to that agent and
returns `Deprecation` + `Sunset` headers; with more than one agent it is a 400
naming the candidates. Removed at the end of Phase 3. Without it, the CLI, the
console and `e2e_platform.py` all break in a single commit — and a migration that
must land atomically is a migration that gets deferred.

### What inspection reversed

This section previously guessed that `/queue/*`, `/files`, `/guard` and `/gate`
"read as workspace-level surfaces" and that calling them workspace-scoped would
"shrink the 53 materially". Two of the four do not survive reading the handlers:

- **`/gate` is agent-scoped** — `get_gate_ep` already calls
  `list_environments(engine.store, agent=manifest.name)`. It is agent-scoped
  today and merely takes its agent from the closure.
- **`/guard` is agent-scoped** — a guard policy is authored per project
  (`rya.guard.yaml` ships inside the bundle), so it cannot be shared by two agents.
- **`/files` splits** — the reads are workspace storage, but `POST /files` "fire[s]
  a `file.uploaded` event at the agent", which is agent-scoped by definition.
- **`/queue/*` does hold up** as workspace-level.

### The finding that makes D28 more than routing

`/guard` and `/gate` are unaddressed **in storage, not only in the route.**
`rya_policy` is keyed `(workspace_id, key)` (`store_postgres.py:169`) and both
call sites pass a bare literal — `POLICY_KEY = "guard"` (`guard.py:81`) and
`POLICY_KEY = "promotion"` (`gates.py:49`). So in a multi-agent workspace, two
agents would **share one guard policy and one promotion gate**, silently.

Neither the route prefix nor D21 fixes this. D28 therefore also requires
agent-qualified policy keys (`guard:<agent>`, `promotion:<agent>`). The table
itself does not change — the qualification lives in the key string.

### What shipping D28 changed about D28

**The migration is a read-time fallback, not a rename.** This section originally
called for "a migration mapping each existing row to the single agent its
deployment served". That is only safe where the workspace has exactly one agent —
which is precisely the case where a fallback is already correct. Where it has two,
a rename has to guess, and guessing wrong hands one agent's gate to the other and
leaves the second silently ungated. So the unqualified row keeps governing every
agent that has not been given its own, and the first qualified write takes over
for that agent alone (`gates.gate_policy`, `guard.store_key_for`). A miss is
`None`, not `{}`, so an operator who deliberately cleared a policy does not have
the legacy one resurrected under them.

**`_` is the reserved sole-agent alias.** The inventory treated `{agent_id}` as
uniformly decorative, but the console and `cloud.py` both send the literal `_`,
and it has always meant "the one agent this deployment serves". Keeping it is not
keeping the old behaviour: every value used to resolve, so a typo was
indistinguishable from a hit. `_` now resolves the sole agent and refuses —
naming the candidates — once there are several, which is the difference between
an alias and a placeholder. Rule 6's unprefixed fallback is implemented as `_`.

**`/approvals` is Rule 3, not Rule 2.** The inventory put `GET /approvals` under
"address explicitly". Reading it again: "everything awaiting a human in this
workspace" is the question an approvals inbox actually asks, and it is the one the
console renders. Both exist — `/agents/{id}/approvals` filters by the run's
agent — and the workspace-level one carries no deprecation header because it is
not going away.

**Kill switches stay workspace-wide.** `/tools` is agent-addressed because the
declarations are the agent's, but `POLICY_KILLSWITCHES` is not qualified: "stop
calling `refund.issue` anywhere in this workspace, now" is exactly the blast
radius an incident wants, and per-agent switches would make an operator flip N of
them under time pressure.

---

## 9. Risks

0. ~~**(Phase 4; open through Phase 5) The sandbox has been built and never run.**~~
   **Closed in Phase 6, and it was worth the wait for the reason risk registers exist.**
   `scripts/verify_gvisor.sh` runs a real `runsc release-20260727.0` sentry over
   `cryptography` (Fernet, HKDF, `getrandom`), `pydantic-core` (validation and a raise
   across the Rust FFI boundary), `psycopg` (conninfo and a socket connect that must
   fail with psycopg's own error rather than crash), `yaml`, `httpx` and — added
   because it would invalidate more than one dependency — `os.fork`. **All six pass.**
   D23's third-party-wheel criterion holds and §9's trigger did not fire.

   **And running it found a live defect that three phases of reading did not.** The
   isolation probe's `/proc/version` marker was the literal `4.4.0`, taken from a
   captured fixture. A real sentry reports `4.19.0-gvisor`. The miss was not a lost
   signal but an **inverted** one: `read_isolation_signals` treats "a version string
   that is not gVisor's" as positive evidence of a host kernel, so a genuine sandbox
   was *actively refuted*, `effective` downgraded to `shared-kernel`, and
   `require_untrusted_posture` refused. It refused in exactly the configuration the
   platform launches, too — `hardening_args` always passes `--cap-drop=ALL`, which is
   usually what makes `dmesg`, the signal still working, unreadable. So the
   deployments that would have hit it are the correctly hardened ones. The marker is
   now the `-gvisor` suffix, which is the stable thing: gVisor has moved its reported
   kernel version at least once and has never not said `-gvisor`.

   That is §9 risk 8's thesis demonstrated on itself. **An isolation claim nobody
   checked is worse than one nobody made** — and here the unchecked claim was not
   "gVisor works", it was "we can tell whether gVisor is working", which failed in the
   safe direction and would have looked like a mysterious refusal rather than a bug.

   *What is still not measured:* the numbers are from a sentry nested in a privileged
   container with `--ignore-cgroups`, because this host has no `runsc`, no passwordless
   sudo, and AppArmor blocks unprivileged user namespaces
   (`apparmor_restrict_unprivileged_userns=1`). The nesting affects **timing** and not
   correctness — the sentry is real and the syscall interception is real — so the
   *criterion* is closed and the *cost model* still carries §11's caveats.

0b. ~~**(Phase 5) The untrusted posture is unlaunchable on every shipped driver.**~~
   **Closed in Phase 6.** Phase 5's discovery stands as written: `sandbox_env` builds
   an environment with no DSN and the process it configured was the *claimer*, so the
   configuration Phase 4 described as the launch posture would have started, opened a
   FileStore inside its own container, claimed nothing, and reported idle ticks that
   look exactly like "no work to do". Nothing detected it because the container path
   was never run end to end.

   The fix is the template host (§7.3), and the shape of the fix is the point: there
   was never one method that was wrong. `sandbox_env` and `worker_env` describe
   opposite processes and both were correct; what was missing was the second container.
   `claimer_env` is the credentialed half, `host_argv` is what the other one runs, and
   `topology_supported` now passes on `docker` and `kubernetes` — while still refusing
   a driver that declares it launches only the sandbox, which is the arrangement a
   third-party driver is most likely to write by accident.
1. **The sandbox is the product's cost floor.** Every margin argument in §6 now
   runs through gVisor's overhead and the supervisor's bin-packing. This is not a
   security tax bolted onto a working economic model; it *is* the economic model.
2. **D18 is a breaking SDK change.** The leaf-tool rule blesses raw IO and env
   reads (2.5). Removing that promise breaks tenant handlers that rely on it, and
   the migration is a tenant-facing deprecation, not an internal refactor.
   **Shipped as one in Phase 4**, on `agent.tool`'s docstring, naming the three
   affected capabilities separately because they change by different amounts: direct
   HTTP stays legal and stops *working* (the sandbox has no route); reading a
   *platform* credential from the environment is gone; direct database access was
   always outside the rule and is now unreachable rather than discouraged. Two things
   made it a deprecation rather than a removal — `ctx.egress` as the sanctioned
   replacement, and the fact that the **trusted posture is unaffected**, so the
   migration statement is "your agent behaves differently on our cloud than on your
   laptop" rather than "rewrite it".
3. ~~**Per-tenant seal keys need real key management.**~~ **Addressed in Phase 4**
   (`keys.py`): four providers, a `KmsWrapper`, rotation as a separate act from
   re-sealing, and an envelope that names its key so two generations can coexist.
   The residual is smaller and worth naming: the recovery story for the
   ``wrapped``+``local`` arm is still "the root key you already had", so it is
   per-tenant compromise isolation and crypto-shredding **without** a hardware-backed
   root. Only ``wrapped``+KMS gets that, and the default is still the deployment key
   because an upgrade must not re-address ciphertext already written.
4. ~~**`guard.py`'s description must change with D24, or the docs overclaim.**~~
   **Changed in Phase 4.** It described itself as "real network-level blocking, not
   advice", which was fair about a cooperative runtime and an overclaim about a hostile
   one. Its docstring now states the division — this module is *what is allowed*,
   `rya.egress` is *what can physically leave* — and keeps the role it was always
   genuinely good at: attributable verdicts, a reviewable allowlist, and
   grounding/secrecy checks the network cannot do at all.
5. **Self-hosting as a residency control (§8) survives; self-hosting as a *trust*
   control does not.** A self-hosted deployment with one trusted tenant does not
   need any of items 6–8. Two postures now exist and the docs will need to say
   which one a given deployment is in, because the security claims differ.
6. ~~**Cold start on a sandboxed, scaled-to-zero key is now user-visible latency**
   on top of bundle materialisation.~~ **Measured (§11).** 433 ms thin, 1335 ms
   worst case, against a 2000 ms budget. The re-derivation this asked for produced
   a sharper problem than the one posed: the dominant term is *tenant* import
   time, so the target cannot be a single global number. Carried forward as open
   question 5.
7. **The supervisor is a scheduler.** Schedulers are where distributed-systems
   complexity re-enters a design that D1 deliberately removed it from. It stays
   inside one deployment, which bounds it, but it is the most subtle component
   here. **Still live after Phase 3, in a narrower form.** The mitigation taken was
   to make the decision a pure function — `Supervisor.plan` reads a registry
   snapshot, a depth count and a quota, and returns a list of actions — so the
   subtlety lives in something fully testable and `rya supervisor --plan` shows an
   operator the real decision without effects. What the split does not remove is
   the distributed part: two supervisors both see the same depth and both act on
   it (open question 7).
8. **The driver seam is where the security claim can silently weaken.** D26 makes
   isolation a *driver property*, so "is rya safe for untrusted tenants" stops
   having one answer. The fail-closed check on declared isolation is what keeps
   that honest, and it is load-bearing rather than defensive — without it, someone
   runs the hosted product on the `docker` driver on a shared kernel and the
   documentation is not wrong, just not consulted. **Enforced since Phase 3**
   (`require_isolation_for_tenancy`), with two details worth keeping: a driver that
   forgets to declare its isolation inherits `none`, and an isolation level this
   build has never heard of ranks as the weakest — both so that the safe direction
   is the default one.

   **Phase 4 closed the residual and found a worse one.** The residual named here was
   that the declaration is a *claim* — nothing verified that pods really landed on a
   gVisor `RuntimeClass`. `verify_isolation` now asks the sandbox what kernel it is on,
   an unverifiable answer is a *refusal* rather than an assumption, and a refuted one
   downgrades the declaration to `shared-kernel`. Three drivers also stopped claiming
   more than their configuration supports: `docker` resolves `sandboxed` only with
   `--runtime=runsc`, and `kubernetes` only with a RuntimeClass.

   The worse one was that **the check was only wired into the supervisor.** A
   hand-started `rya worker` never called it, so `RYA_UNTRUSTED_TENANTS=1` on the
   `local` driver started happily. That is this risk's exact failure mode reached by a
   route nobody had checked — the documentation was not wrong, and the *code* was not
   consulted either. It is now called from `start_worker`, which every route to a
   running worker goes through. Found by the e2e asserting the refusal rather than the
   mechanism, which is the general lesson: a gate is only as good as its least-guarded
   entry point, and the way to know is to attack it rather than to read it.
9. ~~**D27's warm interpreter pool is a cache, and caches skew.**~~ **Addressed in
   Phase 3, and it was nearly addressed in name only.** Pool entries are keyed by
   bundle hash, and the template *recomputes* the hash of the tree it is about to
   import and refuses a mismatch (`E_POOL_HASH_MISMATCH`). The first implementation
   had the template echo back the hash it was asked for, which made the check
   confirm only that the caller remembered its own question — a keyed-by-content
   cache has to be *checked* against content. The one entry that cannot be
   content-checked is the working-tree mode (`rya dev`), where the tree changing is
   the whole point.
10. **The wide scope makes a tenant's own agents noisy neighbours.** Once D27
    widens, `acme`'s `billing` agent can starve `acme`'s `support` agent inside one
    sandbox, and resource limits stop being per-agent. Within a trust domain, so
    not a security issue — but `concurrency_key` fairness was designed across
    workspaces and needs a within-tenant answer before 8b. Does not apply at the
    narrow scope, which is one reason to ship there first.

    **Half closed in Phase 5.** *Scheduling* fairness is answered (D33: equal
    dispatches per group, `FairOrder`), and the phase found a second and sharper form
    of the same risk that this entry did not anticipate: `queue.claim`'s
    after-the-claim agent filter counted a released sibling job against `limit`, so
    the deepest backlog decided whose work got claimed at all. That is fixed. What is
    **not** closed is the *resource* half — one cgroup per tenant, one memory limit for
    up to twelve warm interpreters, and no per-agent ceiling. §7.1 records that as the
    trade the wide scope makes, and it is now a live consideration rather than a
    prediction.
11. **The wide scope gives up `preflight`'s before-claiming guarantee** (§7.1). A
    generic claimer learns a handler-set hole after claiming rather than at startup,
    which is the mid-run failure `worker.py:207` was written to prevent. Item 4 is
    the mitigation; it must land before 8b, not alongside it.

    **Phase 3 found this does not apply at the narrow scope, which §7.1 assumed it
    would.** The claimer does not import, but it does not need to: starting the warm
    template *is* the preflight, because the template imports the bundle and reports
    its handler set before anything is claimed. A hole and a failing import both
    still surface at `start_worker`. The guarantee is lost only once one claimer
    serves many agents and cannot know at startup which handlers it will need — so
    the regression belongs to 8b specifically, not to fork-per-run.

    ~~**And Phase 5 found it does not apply at the wide scope either.**~~ **Retired.**
    The claimer cannot know at startup which handlers it will need, and it does not
    have to know at startup — it has to know *before the claim*. So it **peeks** at the
    queue (a read), warms the version the next item is pinned to, and only then forks a
    child that claims. `template_for` raises `E_HANDLER_SET_INCOMPLETE` there, with the
    item still pending on attempt zero. Item 4 (the persisted manifest) turned out to
    be load-bearing for something else — resolving one agent's model routes in a
    multi-agent broker — rather than for this.
12. ~~**Deciding D27's scope on intuition rather than the measurement.**~~
    **Retired — the measurement was taken (§11).** fork + import is 6.6–13.4 ms
    for real agents and 420 ms for a deliberately pessimistic one; 791 ms under
    gVisor. The wide scope is reachable and the §6 cost model rests on a number.
    The residual is that these were taken on `aarch64` and production is likely
    `x86_64`, so they are re-measurable, not final.
14. **(new, Phase 4) Fork-per-run made the mediated surface the execution plane's,
    not the `ctx` surface's.** D27 keeps the claim and the execute together, so the
    loop that claims runs inside the fork — which means D18 had to mediate
    `turns.execute_pending` and `Engine.work_once`, not just what a handler calls.
    Handing those over unmediated would have been *worse* than the status quo, because
    `queue.claim` applies D22's agent filter in the caller's own Python and
    `queue._check_holder` decides the lease in the caller's own Python. Both are sound
    when the caller is the platform and worthless when it is a fork with the tenant's
    bundle in it. The mitigation taken was to move all five verbs (claim, complete,
    fail, heartbeat, claim-due) to *services* with every identity argument forced
    platform-side, which leaves the mediated claimer strictly more constrained than an
    unmediated one. The residual is that the surface is now 31 store methods plus 9
    services rather than the 19 `ctx` calls the design assumed, so the allowlist is a
    bigger thing to keep correct — and the thing that keeps it correct is that it
    refuses by default, so a method added to `Store` is not automatically reachable.
15. **(new, Phase 4) The wrapped key provider holds every tenant's key material in
    one process's memory.** `WrappedKeyProvider` caches unwrapped DEKs, because the
    seal path is on every connection read and an unwrap is a KMS round trip. That is
    the same exposure the deployment key always had, now multiplied by tenant count in
    the api and the claimer. It is not a *tenant* boundary problem — a tenant process
    has no key at all — but it does mean "compromise one tenant's key" and "compromise
    the platform" are still different sizes of event by a smaller margin than the
    per-tenant framing suggests. `KeyRing.open`'s workspace check is the mitigation
    against a mis-scoped read, not against a memory disclosure.
13. **Fork-per-run inherits `fork`'s own footguns.** New, from Phase 3. Three, all
    documented in `execution/pool.py` rather than solved: a tenant module that starts
    a background thread at import loses it in the child, because only the calling
    thread survives a fork; `os.fork` does not exist on Windows, which is why claimer
    mode is configuration and the in-process executor stays; and a handler that never
    returns still wedges its claimer, because the parent waits for the child.
    Fork-per-run makes the third one *fixable* for the first time — `--run-timeout`
    kills the child and the queue lease reclaims the item — but it defaults to off,
    because an unbounded handler is a pre-existing property of the platform and this
    was not the change to start refusing it in.
16. **(new, Phase 5) A nested mediated call needs its own connection, and that is a
    property nothing enforces.** The deadlock Phase 5 fixed — a streaming
    `ctx.llm.respond` whose `on_token` writes to the store from inside the model call —
    was one instance of a general shape: any broker call made from inside another
    broker call's callback. `BrokerClient` handles it with a thread-local depth counter
    and a temporary socket, which is correct for the case that exists and is a
    *convention* rather than a check. A second nesting level, or a nested call from a
    different thread than the outer one, is untested. The reason this is a risk and not
    a bug is that the failure mode is a deadlock rather than a wrong answer, so it is
    loud once reached and silent until then — exactly how this one survived a phase.
17. ~~**(Phase 5) An org budget is enforced only as often as somebody schedules the
    reconciler.**~~ **Mostly closed in Phase 6, and the residual is named.** D35 puts
    the rollup outside the tenant plane deliberately, and the consequence was that
    `rya orgs reconcile` was the enforcement and nothing ran it — a budget that caps
    nothing, which is the failure `quotas.py` refuses for a *mistyped* limit reached by
    omission instead. `supervisor.reconcile_orgs` now runs it on the multi-workspace
    fan-out (default every 300s, `SupervisorPolicy.reconcile_orgs_seconds`), and
    `orgs.freshness` reports a stale or absent verdict for the deployments that run no
    supervisor.

    **The residual: a deployment with a budget, no supervisor and no cron is still
    unenforced — it now says so instead of being silent.** That is deliberate rather
    than incomplete. Making it a *refusal* would mean a billing rollup being late could
    stop every tenant's work, which is the direction `read_verdict` already declines to
    fail in, and the ordinary case of "no verdict" is a single-tenant deployment with
    no org at all. So `freshness` distinguishes `none` from `stale` and only the second
    is a warning.
18. **(new, Phase 6) The pair's two halves can start in the wrong order, and only one
    of them retries.** `DockerDriver.start` brings the sandbox up first because the
    claimer dials it, and tears the sandbox down if the claimer fails to start. In
    Kubernetes both containers start concurrently and there is no `startupProbe` on the
    host, so a claimer that warms a template in its first tick before the host has bound
    its socket gets `E_TEMPLATE_HOST_UNAVAILABLE`. It recovers — the item's lease
    expires and the reclaim path re-runs it — so the cost is a delayed first turn on a
    cold pod rather than lost work, which is why this is a risk and not a bug. If it
    turns out to be common on slow nodes the answer is a readiness probe on the sandbox
    container, not a retry loop in the claimer.

---

## 10. Open questions

Questions 1–3 were **closed in Phase 0** (D30, D29, D31) and questions 7–8 in
**Phase 5** (D34, D32). They are kept here with their answers rather than deleted,
because the reasoning is what makes the decision reviewable later — and in Phase 5's
two cases the investigation changed the answer, which is only visible if the question
is still on the page.

1. **Who pays for LLM calls?** → **ANSWERED: the platform pools a provider key
   (D30).** Consequences, all of which follow from a leak being platform-wide
   rather than tenant-scoped: the LLM proxy (#12) is promoted out of the general
   Phase 4 pool to sit alongside the broker (#11); metering becomes the billing
   record and therefore governance data that tenant code must not be able to
   write; and **per-tenant quota enforcement becomes a launch requirement**,
   because with a pooled key an unbounded tenant spends the platform's money.
   This fired the PLAN §9 trigger of the same name.
2. **Is workspace the tenant?** → **ANSWERED: no — an `organization` owns many
   workspaces and is the billing entity (D29).** The isolation boundary stays at
   `workspace_id`, so no RLS, bundle-namespace or sandbox-scope work changes; the
   billing boundary moves to `org_id`. This fired the PLAN §9 trigger "every
   `workspace_id` boundary in Phases 1 and 5 needs re-reading against the billing
   boundary" — carried out, and the result is the D29 split rule in §4. The
   re-read found **no isolation boundary that needs to move**; what changes is
   that quota and usage become org-aggregated.
3. **Tenant deletion and retention?** → **ANSWERED: two-phase disable-then-purge,
   crypto-shredding first (D31).**
   - `disable` — immediate, synchronous: revoke API keys, stop scheduling, refuse
     claims. Reversible, and the only step a billing failure should trigger.
   - `purge` — after a retention window: destroy the tenant's seal key (which
     makes every sealed secret unreadable without enumerating them), delete
     bundle objects under the workspace prefix, then delete rows across the 19
     `_DATA_TABLES`.
   - **Unresolved inside this answer:** the governance tables are append-only and
     `SELECT`-only by design ("Read the verdict, never write it", `tenancy.py:167`),
     which is in direct tension with erasure. The position taken is to keep an
     **anonymised audit stub** — retain the decision record, drop the payload —
     but a jurisdiction demanding full erasure would override that, so it needs
     legal review before launch rather than an engineering answer.
   - Depends on **#7** (bundles are not enumerable per tenant until keys carry the
     workspace prefix) and **#13** (there is no per-tenant key to shred until seal
     keys are split). Both are prerequisites for a *complete* purge.

Still open:

4. **What is the api's environment, and who says so?** New from Phase 2,
   **narrowed by Phase 3**. D21 makes the control plane decide which version a
   queued run is pinned to, which it reads from the environment pointer — so the api
   and its workers must agree on *which environment they are*, or the api pins to
   nothing and every turn sits unclaimed.

   Phase 3 removed this from the supported path rather than answering it. The
   supervisor knows which environment it scheduled for, so `WorkerSpec` carries it
   and the driver forces `RYA_ENVIRONMENT` onto the launched process — a worker the
   platform started cannot disagree. What remains open is the hand-started worker
   (`rya worker --env`, a compose file, an ECS task definition), where the agreement
   is still convention and nothing detects a mismatch. The options are unchanged: a
   startup check, a registration-derived answer, or a loud failure in `/agents`. The
   case is now narrower and less likely, which is not the same as closed.
5. **Is `rya dev` still `FileStore`?** PLATFORM_DESIGN §12's open decision, sharpened: none of
   D18–D24 apply locally, so the local and hosted execution planes now diverge
   much more than they do today. Parity testing needs a position.

   **Phase 4 narrowed it and made the divergence measurable.** Two things now hold the
   two planes together rather than hoping: `BrokerStore` is duck-typed to `Store`
   closely enough that `RuntimeContext` cannot tell, and
   `test_the_mediated_and_direct_paths_produce_the_same_run` asserts the same handler
   produces the same run either way. `ctx.egress` is deliberately identical on a laptop
   *including being refused*, so the allowlist is not a thing that only becomes real in
   production. What is still open is the substrate half: nothing local exercises gVisor
   or a network namespace, so the parity claim covers the mediated path and not the
   sandbox. The options are unchanged; the question is now specifically about the
   sandbox rather than about the whole plane.
6. **What bounds a tenant's import time?** New, from the §11 measurements. The
   largest term in cold start is the tenant's own module-scope imports, and
   gVisor multiplies it by ~1.5×. A single global `COLD_START_TARGET_MS` cannot
   hold when the dominant term is tenant-chosen. The options are an import-time
   budget checked at publish (rejects the bundle, which is honest but strict), a
   documented degradation (slow agents just start slowly), or per-plan targets.
   Not urgent until D23 ships, but it is the loose end the measurement exposed.
7. ~~**Who runs the supervisor, and what happens when two do?**~~ → **ANSWERED in
   Phase 5: a lease (D34).** The question arrived in Phase 3 and became urgent when
   Phase 4 shipped the `kubernetes` driver, because the obvious way to run a
   supervisor there is a Deployment and a Deployment with two replicas doubles every
   replica count.

   Investigating it to build the lease sharpened *why* it doubles, and the reason is
   worse than "both see the same depth": `observe` reconciles the worker registry
   against the **driver's** inventory, and a second supervisor's driver inventory is
   empty. So the second replica sees a fleet it did not launch, counts it, and starts
   its own anyway — each believes it is the only one. The over-provisioning is
   therefore not bounded by "they agree and act twice"; it is N× by construction.

   The lease is per **workspace**, not per process, which turns the fix into a
   feature: two supervisors over a hundred tenants split the fleet instead of one
   idling. A supervisor that cannot take it goes *passive* — it still observes and
   still plans, and logs the plan it did not apply, because an operator debugging "why
   is nothing scaling" needs to see a correct plan going unapplied rather than
   silence. `--no-lease` opts out and says so.
8. ~~**(new, Phase 4) Who runs the broker?**~~ → **ANSWERED in Phase 5: a sibling,
   never a parent, and never inside the tenant's container (D32).** What made this
   answerable was discovering the disagreement was not cosmetic. `sandbox_env` builds
   an environment with **no DSN** — correct for the process that imports tenant code,
   and impossible for the process that has to open the database and *be* the broker.
   Both were the same container. A claimer launched that way falls back to a FileStore
   inside its own container, claims nothing, and reports idle ticks that look exactly
   like "no work to do". Nothing detected it because the container path was never run
   end to end: the mediation e2e used `local`, and the container drivers were tested on
   their rendered arguments.

   So the decision names the target arrangement and the gate refuses everything short
   of it: `topology_supported` is now the launch gate's fourth condition, and the
   untrusted posture is currently unlaunchable on **every** driver — `local` fails
   isolation, the container drivers fail this. That is the honest state and it is the
   same stance Phase 4 took for gVisor: the platform declines to claim a boundary it
   has not built. See §7.2 for what remains (a template host), and note that the
   *availability* half of the original question is untouched — the broker's lifetime
   still bounds a dispatch, and `E_BROKER_UNAVAILABLE` mid-run is still reachable and
   still recovered by the queue lease.
9. **(new, Phase 4) What happens to a v2-sealed row when the provider changes?**
   `KeyRing.open` refuses with a named error rather than a bare `InvalidToken`, and
   `rya keyring reseal` moves values across — but there is no migration that runs
   *itself*, so a deployment that flips `RYA_KEY_PROVIDER` and forgets to re-seal has
   working writes and failing reads on exactly the rows it wrote yesterday. A startup
   check ("this store holds rows sealed by a provider you are not configured for")
   would catch it, and `readiness.py` is the natural home. Not urgent, because the
   default provider is unchanged and nobody reaches this without opting in.

---

## 11. Measurements (Phase 0)

Both numbers the plan required before the expensive phases were designed. Taken
with `scripts/bench_cold_start.py` and `scripts/bench_gvisor.sh`, both
reproducible. Budget throughout is `COLD_START_TARGET_MS = 2000` (`worker.py:51`).

**Both re-plan triggers in PLAN §9 were checked and neither fired.** D23 and D27
stand as written, now on evidence rather than on argument.

### Method

Measured inside the **production image** (`Dockerfile`, `.[api,postgres,llm,mcp,s3]`),
not the dev venv — `boto3` and `anthropic` are absent from the dev venv and
present in production, and they turn out to dominate. Cold stages are timed
externally around a fresh process, because in-process timers and `-X importtime`
both omit interpreter bring-up, which is precisely what scale-to-zero pays.

### Fork + import — gates D27's claimer scope and therefore Phase 5

| Agent | fork + import (native) | under gVisor |
|---|---|---|
| `followup_agent` (77 lines, `rya` + stdlib) | **6.6 ms** | 18.6 ms |
| `loan-renewal` (486 lines, `psycopg`, `sqlite3`) | **13.4 ms** | — |
| `csa-counsellor` (28 declared tools) | **13.4 ms** | — |
| synthetic worst case (`httpx` + `anthropic` + `boto3` + `pydantic` at module scope) | **420 ms** | **791 ms** |

Warm-pool **hit** (fork only, agent already resident): **3.5 ms** native,
**15.7 ms** under gVisor.

The trigger was "fork + import ≈ 2s even warm". The pessimistic case is **791 ms
under gVisor — 40% of budget**, and the realistic cases are one to two orders of
magnitude below it. **Phase 5 is reachable; the wide scope is not blocked.**

### `runsc` cold start — gates D23

| | native (`runc`) | gVisor (`runsc`) | ratio |
|---|---|---|---|
| sandbox bring-up (`--network=none`) | 0.5 ms | **42.6 ms** | — |
| sandbox bring-up (`--network=host`) | 0.5 ms | **93.6 ms** | — |
| interpreter floor | 8.9 ms | 13.4 ms | 1.51× |
| platform import | 223.6 ms | 335.6 ms | 1.50× |
| bundle unpack + verify | 1.1 ms | 1.9 ms | 1.73× |
| **end-to-end scale-from-zero, thin agent** | 234 ms (12%) | **433 ms (22%)** | 1.85× |
| **end-to-end scale-from-zero, worst case** | 730 ms (37%) | **1335 ms (67%)** | 1.83× |

gVisor's tax is a **~1.5× multiplier on syscall-heavy work** (imports are mostly
`stat`/`open`/`read`) plus a **40–95 ms one-off bring-up**. Even the pessimistic
agent lands at 67% of budget. **D23 survives contact.**

### What the numbers changed

1. **The dominant term is the tenant's own third-party imports** — not the fork
   (3.5 ms), not the platform (224 ms), not gVisor's bring-up (94 ms). Going from
   `boto3`+`anthropic` absent to present moved fork+import from 53 ms to 420 ms.
   Cold start is therefore **mostly tenant-controlled**, which is not a property
   the design previously accounted for.
2. **Therefore D27's warm pool is a requirement under D23, not an optimisation.**
   A tenant importing `pandas` or `torch` can exceed the budget natively, and
   gVisor multiplies whatever they chose by ~1.5×. The pool turns a 1335 ms worst
   case into 16 ms. D23 and D27 are load-bearing *for each other*; shipping the
   sandbox without the pool would make the budget a tenant's choice.
3. **The budget needs a per-tenant answer, not just a target.** §9 risk 6 asked
   for the cold-start target to be re-derived under D23. The honest re-derivation
   is that a single global target cannot hold when the largest term is tenant
   code — this needs an import-time budget enforced at publish, or a documented
   degradation. **Recorded as open question 5.**
4. **Network setup is over half of sandbox bring-up** (42.6 → 93.6 ms). Under D24
   the sandbox has no general egress anyway, so a minimal network configuration is
   worth roughly 50 ms per cold start.

### Caveats on these specific numbers

- **`aarch64`, `systrap` platform, no KVM** (`/dev/kvm` absent on this instance —
  itself evidence for D23, since Kata and Firecracker are simply unavailable
  here). Production on `x86_64` should be re-measured; both scripts take a
  `--label` for exactly that.
- Run **nested in a privileged container** with `--ignore-cgroups`, because the
  host has no `runsc`, no passwordless sudo, and AppArmor blocks unprivileged
  user namespaces. Cgroup setup cost is therefore excluded.
- `runsc do` approximates sandbox bring-up; a production launch via
  `docker --runtime=runsc` or a Kubernetes `RuntimeClass` adds image and snapshot
  setup, which `runc` pays too.

---

## 12. What this changes in existing docs

Recorded so the drift is deliberate rather than discovered later.

- **D13 is superseded by D17.** PLATFORM_DESIGN §8's "accepted residual" and §12
  risk 1 both become "addressed by D23", and §12's own framing already points
  here.
- **`README.md`'s "Node isolation is an accepted residual"** entry needs the
  two-posture split from risk 5.
- **`docs/architecture.md`'s "One deployment serves exactly one agent"** section
  ✅ **done** — rewritten as "One control plane serves many agents; one worker
  serves one", because that is where the constraint actually moved. `load_agent`
  mutates `sys.path` and never unloads, so a *worker* still holds one agent; the
  api holds none.
- **`VISION_GAP.md:124`'s "(L) multi-agent routing within one deployment"** is
  item 5 here, and is no longer the largest item on the list.
