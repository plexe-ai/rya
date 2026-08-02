# Multi-tenant, multi-agent — phased implementation plan

**Status: Phases 0–6 complete, and the carried-forward list is empty except for one
item that needs hardware this project does not have.** Companion to
[MULTITENANT_DESIGN.md](MULTITENANT_DESIGN.md), which holds the decisions
(D17–D35) and the reasoning. This document holds only sequencing: what order,
what has to be true before each phase starts, and how we know a phase is
finished. Each completed phase carries an outcome block recording what shipping
it actually cost, including the things that were not on this page beforehand.

Tracking issue: **plexe-ai/rya#4**.

## How to read this

- A **phase** is defined by *one property that becomes true*, not by a set of
  tickets. If the property is not testable, the phase is not defined.
- **Exit criteria are assertions**, and where possible existing ones. `scripts/e2e_platform.py`
  already marks known gaps as `GAP` rather than `FAIL` — those flipping to
  `PASS` is the cleanest exit signal this repo has, and the plan uses it. As of
  Phase 3 there are none left, so a new phase has to bring its own assertions.
  Phase 4's are **adversarial**: a hostile agent published into the running
  deployment, asserting against its own report of what it could reach.
- **No calendar dates.** Sizes are the S/M/L from the design doc's §6. Turning
  those into dates needs a team size and an allocation, which is not a technical
  input — see §8.
- **Phases 1–3 are useful on their own.** Phase 4 is the only one that is
  all-or-nothing, because half a security boundary is not a security boundary.
- **A phase is allowed to find the previous one wrong.** Three have: Phase 4 found
  Phase 3's isolation check reachable from one caller only, and Phase 5 found a
  deadlock on Phase 4's mediated inference path and a `sandbox_env` written for the
  wrong process. Those corrections are recorded in place rather than tidied away,
  because the *pattern* in them is the reusable part — every one was an exit criterion
  whose test exercised the mechanism instead of the behaviour.

---

## 1. Dependency graph

```
 PHASE 0 — decide & measure ✅ COMPLETE
   ├── #9  route addressing ────► D28 ────────────┐
   ├── fork+import measured ──────────────┐       │   (trigger did not fire)
   ├── runsc cold start measured ─────┐   │       │   (trigger did not fire)
   └── OQ1►D30 🔥  OQ2►D29 🔥  OQ3►D31 │   │       │
                                   │   │   │       │
 PHASE 1 — correct the existing posture (all [S], all parallel)
   #5 wire tenancy   #6 queue agent filter   #7 bundle namespace   #8 manifest
   #20 org schema (D29, additive+inert) ◄── OQ2
                                   │   │   │       │
 PHASE 2 — multi-agent ✅ COMPLETE  │   │   │       │
   #10 D21 manifest-free api ◄─────┼───┼───┼───────┘  (needed #8, D28)
        └─ incl. agent-qualified guard/gate policy keys (D28 finding)
                                   │   │   │
 PHASE 3 — portable, managed execution plane ✅ COMPLETE
   #18 D26 driver seam ────┬──────────────►#16 D25 supervisor
                           └──►#19 D27 fork per run (narrow) ◄─── measured OK
        └─ incl. worker liveness (the reap signal) + D22 for `jobs` (findings)
                                       │   │
 PHASE 4 — untrusted-safe (the launch gate) ✅ COMPLETE
   tier 1  #11 D18 broker   #12 LLM proxy 🔥   #21 quota 🔥  ◄── OQ1/D30
   tier 2  #13 seal keys    #14 D23 sandbox ◄── #18   (built, gVisor NOT RUN)
                            #15 D24 egress  ◄── #14
   purge (D31) ◄──── needs #7 AND #13
        └─ incl. the launch gate reaching `rya worker` (finding: it only reached
           the supervisor) + `--agent` on worker_argv (finding)
                                       │
 PHASE 5 — scale the economics ✅ COMPLETE
   #19-8b widen scope to per-tenant ◄── needs #8   (config change, as D27 predicted)
   pre-warming (in-claimer), within-tenant fairness D33, org quota D29/D35
   OQ7►D34 supervisor lease   OQ8►D32 broker topology   (gVisor STILL NOT RUN)
        └─ incl. a mediated-streaming deadlock (finding, Phase 4 path) + the
           after-the-claim filter starving siblings (finding) + `sandbox_env`
           configured for the template while launching the claimer (finding)

 PHASE 6 — close the carried-forward list ✅ COMPLETE
   D32 template host ──► the untrusted posture becomes LAUNCHABLE on both container
                         drivers (the pair: claimer + credential-free sandbox)
   gVisor RUN at last ──► D23's wheel criterion holds; the isolation probe did not
        └─ incl. the probe's /proc/version marker being a wrong fixture, so a real
           sandbox was actively REFUTED (finding) + `--scope tenant` blaming the
           wrong flag (finding) + a credential heuristic misfiring on `maxTokens`
   orgs reconcile scheduler (D35) ──► on the supervisor's fan-out, plus freshness
                                      reporting for deployments that run neither

 #17 docs — threads through every phase, never batched at the end
```

**Two items are disproportionately high-leverage and both are cheap:**

- **#9** is a decision, not code, and it blocks the whole of Phase 2.
- **#18** is `M` and blocks both #14 and #16, the two largest items in the back
  half.

Neither should wait for the phase it nominally belongs to.

---

## 2. Phase 0 — Decide and measure — ✅ **COMPLETE**

**Property:** every expensive decision downstream has an input that is not a guess.

No production code. Days, not weeks. Everything here is parallel.

> **Outcome.** Both measurements were taken and **neither re-plan trigger fired**:
> D23 and D27 stand, now on evidence. Both product questions were answered the
> "harder" way and **both fired their §9 triggers**, so §6 and §7 below are
> amended. Full numbers, method and caveats in **MULTITENANT_DESIGN §11**;
> decisions in **D28–D31**.
>
> | Item | Result |
> |---|---|
> | fork + import | **6.6–13.4 ms** real agents, **420 ms** worst case (**791 ms** under gVisor) vs a 2000 ms budget → Phase 5 reachable |
> | `runsc` cold start | **433 ms** thin / **1335 ms** worst case end-to-end (22% / 67% of budget); ~1.5× syscall tax, 40–95 ms bring-up → **D23 survives** |
> | #9 route addressing | **D28** — 5 rules over all 76 unaddressed routes, plus a time-boxed single-agent fallback |
> | OQ1 LLM billing | **D30 — pooled platform key.** Trigger fired: #12 promoted, quota enforcement becomes a launch requirement |
> | OQ2 tenant unit | **D29 — org above workspace.** Trigger fired: boundaries re-read; isolation stays at `workspace_id` |
> | OQ3 deletion | **D31** — disable-then-purge, crypto-shred first; **depends on #7 and #13** |
>
> Two things Phase 0 surfaced that were not on this page:
> 1. **Guard and gate policy keys collide across agents** — `rya_policy` is keyed
>    `(workspace_id, key)` with bare literals, so two agents in one workspace share
>    one guard policy. A Phase 2 data migration, folded into #10 via D28.
> 2. **Cold start is mostly tenant-controlled** — their module-scope imports
>    dominate every platform term. This makes D27's warm pool a prerequisite for
>    D23 rather than a later optimisation, and opens **question 5**.

| Item | Output |
|---|---|
| **#9 route addressing RFC** | a decision on how the 53 agent-scoped routes name their agent, and whether `/queue/*`, `/files`, `/guard`, `/gate` become workspace-scoped |
| **fork + import measurement** | ms to fork an interpreter and import a realistic agent (`pydantic`, `httpx`, a provider SDK), against `COLD_START_TARGET_MS = 2000` (`worker.py:51`). Runs against **today's** worker — needs nothing else on this page |
| **`runsc` cold-start measurement** | sandbox start + bundle materialisation + interpreter start + import, same budget. Decides whether D23's choice survives contact |
| **Open question 1** — who pays for LLM calls | pooled platform key vs tenant-supplied. Sets #12's priority and the blast radius of a leak |
| **Open question 2** — is workspace the tenant | `tenancy.py:259` already models users in many workspaces. Billing and quotas need one answer |
| **Open question 3** — tenant deletion | RLS makes reads safe, not deletion complete. Bundles, sealed secrets, journal and meter rows all need an erasure path |

**Exit criteria**

- [x] #9 closed with a decision recorded in the design doc, not just in the thread
      — **D28**, MULTITENANT_DESIGN §8
- [x] Both measurements written down as numbers, with the agent they were taken
      against — MULTITENANT_DESIGN §11; reproducible via
      `scripts/bench_cold_start.py` and `scripts/bench_gvisor.sh`
- [x] Open questions 1–3 answered in MULTITENANT_DESIGN §10 — **D30, D29, D31**.
      One residual is explicitly deferred with its cost named: erasure vs the
      append-only governance tables needs legal review, not an engineering answer

**Why first:** the fork+import number decides Phase 5's shape, the `runsc` number
can invalidate D23, and #9 gates Phase 2. All three are cheap. Taking them after
committing to the work is how a design gets defended rather than tested.

**Vindicated in the event.** Two of the six items changed the plan: the product
answers fired two triggers, amending Phases 1, 4 and 5 *before* any of that work
started. The measurements, by contrast, changed nothing — which is the outcome
that is only worth having because it could have gone the other way.

---

## 3. Phase 1 — Make the existing posture correct — ✅ **COMPLETE**

**Property:** the multi-tenant mode that already ships is actually sound.

> **Outcome.** All five items shipped with tests. Suite: **625 passed** with
> Postgres, **577** without; `scripts/e2e_platform.py` reports **55 passed, 0
> failed, 4 known gaps** — the same four as before, none of which Phase 1 was
> meant to flip.
>
> | Item | Landed as |
> |---|---|
> | **#6** | `queue.agent_of` + an `agent` filter on `claim()`, applied *independently* of the version filter. `turns.create_turn` now tags the owning agent whether or not a version exists to pin to |
> | **#5** | `store.open_worker_store()` — scoped to `--workspace`, connected as `rya_worker`. `config.multitenant_enabled()` moved out of `api/app.py` so the worker can ask without importing FastAPI. `preflight` refuses a key/store workspace mismatch |
> | **#7** | `BundleStore.workspace` + `read_workspaces()`; keys become `<prefix>/<ws>/<hash>.tar.gz`. Writes never divert to a fallback |
> | **#8** | The full manifest on the version record, plus `deployments.manifest_of()` as the one place that knows pre-D21 records have none |
> | **#20** | `rya_organizations` + nullable `org_id`, backfilled one-org-per-workspace, idempotent across `setup()` calls |
>
> Three things worth carrying forward:
> 1. **`queue_depth()` had the same hole as `claim()`.** An unpinned worker has no
>    version to filter on, so a sibling agent's pending turn counted as depth
>    forever — the worker would never claim it and never go idle, which is a
>    scale-to-zero failure reached by a different route than the one §6 documents.
> 2. **"default" and "" were two spellings of one tenant.** `WorkerKey.workspace`
>    defaults to `"default"`; `FileStore` has no workspace at all. Namespacing
>    naively made every single-tenant cold start pay a wasted object-store round
>    trip on a fallback. Both now normalise to the un-namespaced address, so the
>    single-tenant layout is unchanged and costs one lookup.
> 3. **The legacy bundle fallback is opt-in and fail-closed.** A deployment that
>    was *already* multi-tenant before D20 has flat archives, and its tenants
>    would 404 after the upgrade. `RYA_BUNDLES_LEGACY_FALLBACK=1` fixes that, and
>    defaults off because reading a flat key from a named tenant is a cross-tenant
>    read.

Four items, all `S`, all independent of each other — parallelise freely. **This
phase is worth shipping even if the epic stops here**, and it contains the only
live cross-agent defect on this page.

| Issue | Change |
|---|---|
| **#5** | Wire `worker_dsn()` / `rya_worker`, make `--workspace` load-bearing, have the worker consult `multitenant_enabled()`. The role and grants exist with zero callers |
| **#6** | D22 — `agent` on the queue claim path. Today an unpinned worker will execute another agent's `chat-turn` against its own handler |
| **#7** | D20 — workspace-prefixed bundle keys and per-tenant credential scoping |
| **#8** | Persist the manifest on the version record. Unblocks Phase 2, and later preflight-before-claim |
| **#20** | **D29 (new, from Phase 0)** — `rya_organizations` plus a nullable `org_id` FK on workspaces, backfilled one-org-per-workspace. Additive and inert: nothing reads `org_id` until Phase 5's quota work |

**Why #20 lands here and not in Phase 5.** It is the cheapest it will ever be
while every workspace still maps to exactly one org. Adding the column after
tenants exist means a backfill against live data with a billing boundary already
in use.

**Exit criteria**

- [x] A worker scoped to workspace A cannot read or write workspace B's rows —
      asserted, not reasoned — `test_a_worker_scoped_store_cannot_read_or_write_another_workspace`,
      plus `test_the_worker_role_is_subject_to_rls`, which also asserts that the
      superuser DSN it replaces *does* see both rows
- [x] A write from the worker role to any `_GOVERNANCE_TABLES` table fails —
      `test_the_worker_role_cannot_write_governance_tables` (INSERT/UPDATE/DELETE
      across all four, `InsufficientPrivilege`), with
      `test_the_worker_role_can_still_write_run_data` as the counterweight
- [x] An **unpinned** worker for agent A does not claim agent B's `chat-turn` —
      `test_an_unpinned_worker_does_not_claim_another_agents_turn`
- [x] A bundle store scoped to tenant A cannot resolve tenant B's archive —
      `test_a_workspace_scoped_store_cannot_resolve_another_tenants_archive`
- [x] `docker-compose.yml` and `deploy/aws/template.yaml` pass the worker DSN, not
      the admin DSN — via `RYA_WORKER_DATABASE_URL`, which also *overwrites*
      `RYA_DATABASE_URL` in the worker task so the master credential is absent
      rather than merely unused. **Partial by design:** in the single-tenant
      default the roles do not exist yet, so the derive path remains
- [x] No row exists where `data->>'workspaceId'` disagrees with the `workspace_id`
      column — `test_no_row_disagrees_about_its_own_workspace`

**What this phase does NOT deliver.** It contains a **buggy** tenant, which is
D13's threat model. It does not contain a hostile one — RLS is a session-GUC
boundary and does not bind code that can execute SQL (design doc §2.1). Untrusted
tenants are not safe after Phase 1 and the docs must not imply otherwise.

---

## 4. Phase 2 — Multi-agent ✅ COMPLETE

**Property:** one deployment serves many agents, and the `api` never imports tenant code.

| Issue | Change | Status |
|---|---|---|
| **#10** | D21 — `build_app` stops resolving a single manifest; agents come from `rya_versions`/`rya_environments`; the addressing decision from #9 lands across the routes; the console gets an agent selector | ✅ |

**Outcome.** `scripts/e2e_platform.py`: **66 passed, 0 failed, 1 known gap** — up
from 55/0/4, with all three of this phase's gaps flipped and five new multi-agent
assertions added. The suite is 616 passed / 1 failed (the pre-existing
`test_quotas` refusal-message defect, unrelated and failing at HEAD).

What landed, by piece:

| | |
|---|---|
| `agents.py` | The registry. An agent exists because a version or an environment pointer says so; its declarations come from the manifest #8 persisted. No file, no import |
| `api.app.Plane` | Replaces `Engine` as the route dependency. It carries a store and no manifest and no handler, so "the api does not run tenant code" is a property of the **type** rather than of what the handlers happen to call |
| D28 | Rule-2 routes moved under `/agents/{agent_id}/…`; unprefixed spellings kept behind Rule 6 with `Deprecation`/`Sunset`; `_` reserved as the sole-agent alias (the console and `cloud.py` already sent it) |
| Policy keys | `guard:<agent>` / `promotion:<agent>`, with a read-time fallback to the pre-D28 row |
| Plane boundary | `POST …/events` and `/approvals/{id}/approve` enqueue instead of executing; `/reject` stays synchronous because it runs no tenant code |

**Exit criteria** — three were existing `GAP` assertions in `e2e_platform.py`:

- [x] `POST /events does not execute in the api process` — GAP → **PASS**, plus
      `test_post_events_does_not_execute_in_the_api_process`
- [x] `POST /events pins the run to the promoted version` — GAP → **PASS**, plus
      `test_post_events_pins_the_run_to_the_promoted_version`
- [x] `approval resume is claimed by a worker` — GAP → **PASS**, plus
      `test_approval_resume_is_handed_to_a_worker`. The check moved into
      `phase_durability`, where the approval actually happens with a worker alive;
      in `phase_isolation` every worker is dead, so it could only ever have failed
      there
- [x] Two agents, published from two different projects, served and independently
      promotable and rollback-able on one deployment — `phase_multi_agent`, and
      `test_the_two_agents_promote_and_roll_back_independently`
- [x] `POST /agents/{id}/versions` accepts a bundle whose name is not the
      deployment's — obsolete, not relaxed. What survives is the half that was
      always about the artifact (`declared != agent_id`), asserted both ways by
      `test_publishing_an_agent_this_deployment_never_heard_of_is_accepted` and
      `test_a_bundle_still_cannot_be_filed_under_a_name_it_does_not_declare`
- [x] `E_JOURNAL_DRIFT` on a published approval resume no longer occurs — the
      resume job is pinned to `run["versionId"]`, so the process continuing a run
      is on the hash that paused it, by construction
      (`test_the_resume_job_is_pinned_to_the_version_that_paused_the_run`)

### What the work surfaced

**A pin and a declaration are different questions.** The registry's first cut fell
back to "newest active version" when nothing was promoted, and used that for both.
It broke the e2e immediately: a run got pinned to an unpromoted version, so
`queue.claim`'s version filter routed it to a worker that did not exist and the
turn sat pending forever. `AgentRef` now separates `version` (the pin — the
environment pointer *only*) from `declared_by` (where the manifest came from). An
unpromoted deployment enqueues **unpinned**, exactly as before Phase 2; what it
must not do is invent a pin.

**The api and the worker have to agree on which environment they are.** The api
reads the environment pointer to decide the pin, so an api on `dev` and a worker
on `prod` agree on nothing and every turn sits unclaimed. `RYA_ENVIRONMENT` is now
explicit in the e2e's api spawn and documented in the compose anchor. This is a
new coupling that Phase 2 creates and it will bite someone.

**A promoted version has to beat the mounted tree.** The tempting rule is the
opposite — in single-tenant the tree is what the inline worker executes — but a
tree has no version, so preferring it silently un-pins every run on a deployment
that has both.

**The first guard write went to a file, and both agents read it.** `_guard_source`
chose store-vs-file on whether a policy already existed, so the first `PUT /guard`
always landed in `<root>/rya.guard.yaml` — shared by every agent, and living in
one project's bundle. The file arm is now reachable only for the mounted
project's own agent (`test_each_agent_has_its_own_guard_policy`).

**First user-visible capability on this page.** Tenants can run more than one agent.

**What this phase does NOT deliver.** Still trusted-tenant only. Phase 2 removes
tenant code from the `api`; it does nothing about what tenant code can reach from
the `worker`. And a single-tenant api serves every agent it knows about but can
only *execute* the mounted one — a published agent's code is in a bundle this
process has deliberately never unpacked, so everything else needs `rya worker`.

---

## 5. Phase 3 — Portable, managed execution plane ✅ COMPLETE

**Property:** the fleet schedules itself, on any substrate, and scale-to-zero is two-way.

| Issue | Change | Status |
|---|---|---|
| **#18** | D26 — `ExecutionDriver` seam, `local` driver first, declared isolation + the fail-closed check | ✅ |
| **#19** | D27 — fork per run, bundle cache, hash-keyed warm pool. Ships at the **narrow** `(workspace, agent, version)` scope | ✅ |
| **#16** | D25 — supervisor: start on demand, scale on claimable depth, enforce `maxWorkers` at schedule time, pre-warm, reap | ✅ |

**#18 was done first**, as planned — it is `M`, it blocks the other two, and the
`local` driver gave the supervisor something to schedule against before any
container work existed.

**Outcome.** `scripts/e2e_platform.py`: **87 passed, 0 failed, 0 known gaps** — up
from 66/0/1, and **the gap list is now empty**, so a future phase has to bring its
own assertions rather than flip an existing one. The suite is 689 passed / 1 failed
without Postgres and 737 / 1 with it (the failure is the pre-existing `test_quotas`
refusal-message defect, unrelated and failing at HEAD).

What landed, by piece:

| | |
|---|---|
| `execution/drivers.py` | The substrate seam. `WorkerSpec` → `WorkerHandle`, `local` driver, per-driver `cold_start_target_ms`, declared isolation, and `require_isolation_for_tenancy` |
| `execution/supervisor.py` | The policy. `observe` → `plan` → `apply`, where `plan` is *pure* — so every scheduling question is testable without launching a process |
| `execution/pool.py` | Fork per run from a warm interpreter keyed by **bundle hash**. The claimer holds no tenant import at all |
| `worker.py` | Split into `InlineExecutor` (what a worker always was) and `ForkExecutor`. Everything else — registration, preflight, heartbeat, depth, idle exit — is identical in both, which is what lets the supervisor schedule one shape |
| Liveness | `store.worker_liveness`: a worker that stopped heartbeating reads `lost`, derived at read time so it is true in a deployment running no supervisor |
| `rya supervisor` | `--plan`, `--once`, `--all-workspaces`, `--prewarm`; `rya worker --fork` |

**Exit criteria** — all six, with the named assertions:

- [x] `SIGKILLed workers are not reported alive` — GAP → **PASS**, plus a second
      check that the killed worker is *listed* as `lost` rather than hidden (an
      empty worker list means scale-to-zero, so vanishing would be a worse lie),
      and six unit tests in `test_worker.py`
- [x] A key that has scaled to zero is **restarted automatically** —
      `phase_supervisor`'s "a key that scaled to zero is restarted automatically",
      and the run `phase_isolation` deliberately strands with every worker dead is
      then executed. `test_a_key_scaled_to_zero_is_restarted_when_work_arrives`
- [x] The same supervisor code, no substrate branching —
      `test_the_same_policy_produces_the_same_plan_on_every_driver`, parametrized
      over `local` and three stand-in drivers declaring `shared-kernel`,
      `sandboxed` and `microvm`. What is asserted is that the **plan** is
      identical; the launches are not, which is what a driver is for
- [x] Untrusted tenancy on a weak driver **fails at startup** — `E_ISOLATION_INSUFFICIENT`
      in `phase_supervisor` against the real deployment, plus a test asserting the
      refusal for *every* driver in the registry so a new one cannot be added quietly

      > **Phase 4 correction: this was true of `rya supervisor` and not of the
      > platform.** The check had exactly one call site, so a hand-started
      > `rya worker` never reached it and `RYA_UNTRUSTED_TENANTS=1` on the `local`
      > driver started happily. The criterion said "fails at startup" and the test
      > asserted the *predicate*, which is why the gap survived a green suite. It is
      > now called from `start_worker` — which every route to a running worker goes
      > through — and `phase_posture` asserts the refusal rather than the function.
      > The lesson generalises: an exit criterion phrased as behaviour needs a test
      > that exercises the behaviour, not the mechanism behind it.
- [x] `maxWorkers` enforced when scheduling —
      `test_maxworkers_is_enforced_when_scheduling_not_only_when_registering`
- [x] A run executes in a fork, keyed by hash not version id —
      `test_a_turn_runs_in_a_forked_child_not_in_the_claimer` (the run reports a pid
      that is neither the claimer's nor the template's) and
      `test_a_template_that_loaded_different_content_is_refused`

### What the work surfaced

**Two things depended on the heartbeat lie, and the second one was worse.**
`GET /workers` overstating the fleet was the known cosmetic half. The other half is
that `quotas._usage_for` counts `worker_list(status="alive")` against `maxWorkers` —
so every crash consumed a slot permanently, and enough crashes would refuse the
whole fleet while reporting, correctly from the data it had, that the tenant was out
of capacity. The supervisor's schedule-time quota check could not have been written
against that signal.

**The `jobs` primitive had the cross-agent hole D22 closed for turns.**
`claim_due_job()` took no agent filter and `create_job` recorded no agent, so in a
workspace with two agents either worker claimed either agent's due job and
`run_job` raised `E_HANDLER_NOT_FOUND` on the one it could not serve — and
`queue_depth` counted every sibling's job as its own, so it never went idle. Found
because the supervisor needs to route a due job to a key and had nothing to route
on. Fixed the same way D22 fixed the queue: an agent on the row, a filter on the
claim, and an absent value stays claimable by anyone so existing rows keep working.

**Fork-per-run had to keep the claim and the execute together.** The obvious split
is "parent claims, child executes"; it is wrong, because every durability guarantee
in `turns.py` — the lease, the reclaim, the memoized replay — lives in that pair.
So the child runs the ordinary `execute_pending` path with `limit=1` and the parent
only decides *whether* to fork. Claims are atomic, so N children racing on one key
is a case the design already covered, and fork-per-run needed no new concurrency
reasoning at all.

**`preflight`'s before-claiming guarantee is relocated, not lost.** §7.1 predicted
that a claimer which does not import cannot advertise a handler set, and treated
that as the wide scope's regression. At the narrow scope it is avoidable: starting
the warm template *is* the preflight, because the template imports the bundle and
reports its handler set before anything is claimed. A handler-set hole and a
failing import both surface at `start_worker`, exactly as they did inline.

**The pool's content check was vacuous in its first cut.** The template reported
back the hash it had been *asked* for, so the comparison only confirmed the caller
remembered its own question. It now recomputes the hash from the tree it is about
to import. §9 risk 9 asked for a check on content; a check on the request is not
one.

**The state root is not the bundle root.** A forked child cannot inherit the
claimer's store handle — a psycopg connection must not be shared across a fork — so
it opens its own, and the first cut opened it against the unpacked bundle
directory. That produced a private empty database, claimed nothing, and reported an
idle tick indistinguishable from "no work to do".

**Phase 2's environment coupling is now mitigated where it is created.** Open
question 4 and the §9 trigger below both say the api and its workers must agree on
`RYA_ENVIRONMENT` or every turn sits unclaimed. A supervisor *knows* which
environment it scheduled for, so `WorkerSpec` carries it and the driver forces it
onto the launched process. The convention still exists for hand-started workers;
what changed is that the supported path no longer relies on it.

**What this phase did NOT deliver.** Isolation. Every driver available at the end
of Phase 3 declared `none`, so the fail-closed check *refused* untrusted tenancy —
correctly. That is the point of building the check before the sandbox. It also did not
deliver a fleet larger than one box: `local` launches a subprocess here. `docker` and
`kubernetes` landed with #14 in Phase 4; `ecs` is still unwritten.

**And one thing it delivered less of than it recorded.** The fail-closed check was
wired into `rya supervisor` and nothing else, so a hand-started `rya worker` walked past
it — Phase 4 found that by asserting the refusal rather than the mechanism. The
Phase 3 claim above was true of the supervisor and not of the platform.

---

## 6. Phase 4 — Untrusted-safe ✅ **BUILT**

**Property:** tenant code holds no credentials, has no unmediated network path, and
runs in a sandbox that contains a kernel escape.

**This is the only all-or-nothing phase.** Half a security boundary is not a
security boundary, so nothing here ships incrementally to the untrusted posture —
though the pieces can be *built* and merged incrementally behind the Phase 3
fail-closed check. That is what happened, and the fail-closed check is now the
launch gate: one refusal over all three of D18, D23 and D24.

**Amended by Phase 0.** D30 pooled the provider key, which fired the §9 trigger
promoting #12. It is no longer one of five peers — it is a credential boundary of
the same kind as the broker, and it acquired a billing responsibility.

| Tier | Issue | Change | Status |
|---|---|---|---|
| **1 — credential boundaries** | **#11** | D18 — the broker: `ctx` implementations become RPC to a sidecar the tenant cannot reach | ✅ |
| **1 — credential boundaries** | **#12** | **Promoted (D30).** LLM proxy. A pooled key makes this a hard secrecy boundary: a leak is platform-wide, not tenant-scoped. Also enforces the model allowlist and writes the metering record | ✅ |
| **1 — credential boundaries** | **#21** | **New (D30).** Per-workspace quota enforced at the proxy, budgeted per org (D29). With a pooled key, an unbounded tenant spends *our* money — theft-of-service is a billing control, not an abuse nicety | ✅ |
| 2 | **#13** | Per-tenant seal keys, KMS-backed, with rotation and re-seal | ✅ |
| 2 | **#14** | D23 — `docker` and `kubernetes` drivers with `runsc`; resource limits per sandbox | ✅ |
| 2 | **#15** | D24 — deny-by-default egress at the network layer; `guard.py` becomes governance | ✅ |

**Built in dependency order**, which was not the tier order: `keys.py` first (it is
what D31 shreds and it blocks nothing else), then the broker, then the two boundaries
that hang off it, then purge. The tiers describe what must ship *together* for the
posture to hold; they do not describe what to write first.

What landed, by piece:

| | |
|---|---|
| `keys.py` | The key-provider seam: `deployment` \| `derived` \| `wrapped` \| `wrapped`+KMS, a `enc:v2:<key_id>:` envelope so rotation has something to name, and `destroy` — the crypto-shred D31 needs |
| `broker/protocol.py` | The wire, the **method allowlist**, and capabilities. Imports neither of the other two, so the security-relevant part is readable on its own |
| `broker/server.py` | Runs in the claimer, holds every credential, and re-scopes every identity argument. Nine *services* where a credential must not travel or the authorization logic itself had to move across the boundary |
| `broker/client.py` | A `Store`-shaped façade over a socket. Unknown methods raise `AttributeError` rather than refusing on the wire, so `RuntimeContext`'s capability checks degrade correctly |
| `broker/inventory.py` | The credential inventory, classifying `platform` \| `tenant` \| `ambiguous` — and the scrub, from the same list, so the two cannot disagree |
| `egress.py` | The network verdict, the mediated `fetch`, and the divergence reconciler |
| `execution/drivers.py` | `DockerDriver` + `KubernetesDriver`, `sandbox_env` built from nothing, resource limits, the isolation **probe**, and `require_untrusted_posture` |
| `purge.py` | D31's two phases, with an *attestation* that distinguishes "unreadable by construction" from "rows deleted" |
| `ctx.egress` | The replacement for the leaf-tool's raw request: mediated, journaled, and identical on a laptop |
| CLI / api | `rya posture`, `rya keyring show\|rotate\|reseal`, `rya workspaces disable\|enable\|purge`, `GET /posture`, `GET /lifecycle` |

**Exit criteria** — all eleven, with the named assertions:

- [x] A credential inventory proves the tenant process environment and memory
      contain **no** DB DSN, seal key, provider key or bucket credential —
      `phase_mediation` asserts it **from inside a hostile handler's own report**,
      which is stronger than the platform's claim about itself; plus
      `test_a_mediated_child_holds_no_database_credential` and
      `test_a_sandbox_environment_is_built_from_nothing`. The honest limit is stated in
      `broker/inventory.py`: freed heap cannot be proven, so the guarantee rests on the
      sandbox's environment being *constructed* without the values rather than scrubbed
- [x] Tenant code calling `set_config('app.workspace_id', …)` reaches no database
      connection at all — `AttributeError`, asserted in `phase_mediation`. There is
      nothing to re-scope because there is no connection
- [x] Tenant code issuing a raw `urllib` request to a non-allowlisted host is
      **blocked by the network** — `phase_mediation`'s `rawEgress` check. The sandbox
      has no route (`--network none` / an empty-egress `NetworkPolicy`), so it fails at
      `connect()` rather than at a Python check tenant code could skip
- [x] A divergence between `guard.py`'s verdict and the network verdict is detectable
      and alertable — `egress.EgressService.divergences` and `egress.reconcile`, with
      six tests including both directions of disagreement. The divergence is **real
      rather than ceremonial**: the network posture is a snapshot and the policy is
      live, so a promote produces one until every sandbox is recycled
- [x] The `kubernetes` driver declares `sandboxed` and the fail-closed check passes —
      `test_the_kubernetes_driver_defaults_to_asking_for_gvisor`, and the declaration is
      now *verified* rather than trusted (below)
- [ ] `psycopg`, `pydantic-core` and `cryptography` all work under `runsc` — **not
      run.** No gVisor on this machine, and `scripts/bench_gvisor.sh` from Phase 0 is
      the harness for it. See "what is not proven" below
- [x] Compromising one tenant's seal key decrypts nothing belonging to another —
      `test_shredding_one_tenants_key_leaves_the_others_readable`, asserted from the
      harder direction: destroying `acme`'s key and then *reading* `globex`'s. Plus
      `E_KEY_WORKSPACE_MISMATCH`, which refuses a cross-workspace open even when the
      caller holds both keys
- [x] The D2 leaf-tool contract change has shipped a deprecation, not a surprise —
      `sdk/agent.py`'s `tool()` docstring names the three affected capabilities
      separately, because they change by different amounts, and states plainly that the
      trusted posture is unaffected
- [x] **(D30)** A tenant that exhausts its quota is refused inference at the proxy —
      `test_quota_is_enforced_on_the_call_path_not_only_at_admission`, wired to the same
      `require_admission` with `kind="model"` so there is one answer to "is this
      workspace over budget"
- [x] **(D30)** The pooled provider key appears in no tenant process, and every
      inference call has a metering row tenant code could not have written —
      `test_the_broker_writes_the_meter_row_for_a_call_it_made` (rows carry
      `source: "broker"`), and `meter_append` is off the allowlist so
      `test_metering_is_not_on_the_allowlist_because_the_billed_party_would_write_it`
      is the other half
- [x] **(D31)** A purge destroys the tenant's seal key, its bundle objects and its
      `_DATA_TABLES` rows, and leaves an anonymised audit stub — **exercised** in
      `phase_lifecycle` end to end and in 23 tests in `test_purge.py`

**The launch gate, now in code.** Self-serve signup must not be enabled until every
box above is ticked, and `require_untrusted_posture` is what enforces it: one refusal
naming every unmet condition, because three separate warnings are three separate
things to miss. `rya posture` and `GET /posture` show it without a deploy. The design
doc records that D13's residual is superseded by D17; that window is now closed for
every property except the one measurement below.

### What is not proven

**gVisor was never run.** The `kubernetes` driver *asks* for a `RuntimeClass` and the
`docker` driver *asks* for `--runtime=runsc`, and this machine has neither, so the
sandbox has been built and never executed. Two consequences, and they are different
sizes:

1. The **third-party-wheel criterion is untested.** `psycopg`, `pydantic-core` and
   `cryptography` under `runsc` is a real question — they are the three that do
   syscall-heavy or crypto-accelerated work — and D23 rests on the answer. Phase 0's
   `scripts/bench_gvisor.sh` is the harness; it needs a host with gVisor installed.
2. The **probe's positive path is untested against a real sentry.** Its *negative*
   path is: `test_a_container_on_a_host_kernel_is_refuted` feeds it this machine's
   actual `/proc/version` and it refutes. The positive path is tested against
   captured gVisor output, which is a fixture rather than a measurement.

Both are stated here rather than in a footnote because §9 risk 8's whole point is that
an isolation claim nobody checked is worse than one nobody made. What Phase 4 delivers
is that the platform **refuses to claim it**: an unverifiable probe is a refusal, not
an assumption, so a deployment that cannot prove gVisor cannot run untrusted tenants.

### What the work surfaced

**The mediated surface is much larger than the `ctx` surface, and the reason is
D27.** Fork-per-run put the *claim loop* inside the tenant's trust domain, so what
had to be mediated was not just what a handler calls but what `turns.execute_pending`
and `Engine.work_once` call — the execution plane's own methods. That would have been
a downgrade rather than a boundary, because `queue.claim` applies D22's agent filter
**in Python, after the claim** (a deliberate choice, recorded in `queue.py`, to avoid
forking the two store backends). A hostile handler would simply not release the
sibling's job it was handed. So claiming became a *service* with the agent, the
version, the worker id and the lease all forced platform-side — and the mediated
claimer is now strictly **more** constrained than an unmediated one.

**All four lease verbs had to move, not just the claim.** Found by a failing test
rather than by reasoning: forcing the worker id on the claim while the child completed
under its own id made every mediated turn fail `E_QUEUE_CONFLICT`. `queue._check_holder`
is caller-side code, so `complete`, `fail` and `heartbeat` are services too — and the
heartbeat's extension is clamped, because a fork granting itself a week-long lease
would disable the reclaim path that recovers a wedged run for a week.

**Metering was written by the party being billed.** `RuntimeContext._meter` calls
`store.meter_append` from inside the tenant process. That was merely untidy while the
tenant held its own provider key; under D30 it is the invoice. The broker writes the
row now, from the response it made the call for, and `meter_append` is off the
allowlist — so a handler that reaches for it gets `AttributeError` and the runtime
degrades to not metering, which is exactly the arrangement D30 wants.

**`derived` per-tenant keys cannot be crypto-shredded, and that is the trap.**
HKDF-from-a-root is the cheap way to get per-tenant keys and it genuinely delivers
compromise isolation — but the key is a pure function of the root and the workspace
id, so there is nothing to destroy. A deployment that adopted it believing D31 was
satisfied would have a provably false erasure story. `destroy()` therefore *raises*
rather than returning zero: an erasure path must not appear to succeed.

**A purge that stopped at `_DATA_TABLES` would leave every member's email address.**
Those nineteen tables are the data plane; `rya_workspace_members` is admin-plane and
holds exactly the category of data a deletion request is usually actually about. The
purge deletes the tenancy rows too, and deliberately leaves `rya_users` alone — a
person can belong to several workspaces, so deleting the account because one of them
was purged would erase a different tenant's data.

**The supervisor could schedule for one agent and launch a worker serving another.**
A Phase 3 defect, surfaced by needing a mediated worker for a *second* agent:
`worker_argv` never passed `--agent`, so a launched worker resolved its own from the
mounted `rya.agent.yaml`. Invisible with `--version` (the version record names the
agent) and decisive with `--env`, which is how the supervisor schedules an unpinned
key. Same class of cross-agent mix-up D22 closed on the claim path, one layer up in
the launch path.

**Phase 3's error codes were never declared in the exit table.** All eight of them
fell through to `EXIT_GENERIC`, and the one that mattered is
`E_ISOLATION_INSUFFICIENT`: the launch gate exited `1`, so an operator's deploy script
could not tell a deliberate refusal from a crash. That is the same miss `errors.py`
already records a comment about for `E_NOT_FOUND`.

**`resolve_driver(env=…)` selected a driver and then let it configure itself from
`os.environ`.** Invisible while `local` read nothing from the environment; decisive
once the reading decides whether a driver claims to be sandboxed. D8's own rule,
applied to a seam Phase 3 wrote.

**A half-sent frame pinned a broker thread forever.** An accepted socket inherits no
timeout, so a peer that announced a length and stopped sending held a thread for the
life of the process — a denial of service available to any tenant. Fixed with a
separate, short body deadline: an idle connection between calls is normal and may last
a whole run, and a half-sent frame is not.

**`ctx.egress` had to exist before the D2 deprecation was honest.** Deprecating "a
leaf tool may do real IO" without providing a sanctioned replacement would have been a
removal dressed as a deprecation. It is journaled, unlike a leaf's raw request, which
is also the reason a leaf could never have had it: an outbound call is a side effect,
and a replay after an approval pause must return the memoized response.

---

## 7. Phase 5 — Scale the economics ✅ COMPLETE

**Property:** sandbox count tracks active tenants, not the N×M×V product.

| Item | Change | Status |
|---|---|---|
| **#19-8b** | Widen the claimer scope to per-tenant. A config change if Phase 3 built #19 as specified | ✅ |
| — | Pre-warm the current version of each production environment | ✅ |
| — | Within-tenant fairness (**D33**): `concurrency_key` was designed across workspaces and needs an answer within one | ✅ |
| — | Preflight-before-claim, replacing the startup guarantee the wide scope gives up | ✅ — and **not** by the route the plan predicted; see below |
| — | **(D29)** Usage and quota become org-aggregated: metered at `workspace_id`, budgeted and invoiced at `org_id`. `/usage` and `/quotas` gain the org rollup | ✅ (**D35**) |
| — | Decide where the broker runs (open question 8) | ✅ **D32** — and it found a live Phase 4 defect |
| — | Give the supervisor a lease, or make its replica count deliberate (open question 7) | ✅ **D34** |
| — | Run gVisor. D23's third-party-wheel criterion is the only Phase 4 exit box still open | ❌ **still not run** — no `runsc` on the build machine, no passwordless root, and installing it means restarting the Docker daemon under someone else's running containers. Unchanged as §9 risk 0 |

**The gate held.** Phase 0's measurement was the precondition (fork + import 6.6–13.4 ms
real, 791 ms worst case under gVisor, against a 2000 ms budget) and it said the wide
scope was reachable. It was: `RYA_CLAIMER_SCOPE=tenant`, one key spelled `ws:*:*`, and
the same drain loop. D27's central bet — that building fork-per-run first would make
this configuration rather than a rewrite — paid off.

**Outcome.** `scripts/e2e_platform.py`: **146 passed, 0 failed, 0 known gaps** — up
from 129/0/0. The suite is **854** passed without Postgres and **910** with it, both
with **zero failures**, which is a first: the `test_quotas` refusal-message
failure that every phase since Phase 1 has reported as pre-existing turned out to be a
**hardcoded date in the test**, not a product defect — `_run()` defaulted `createdAt` to
a literal `2026-07-30`, so every `maxRunsPerDay` assertion passed on the day it was
written and fell out of the current UTC day the next morning. Fixed, and the fixture now
says why.

What landed, by piece:

| | |
|---|---|
| `execution/scope.py` | **New.** The scope vocabulary, `peek`, `resolve_version` and `FairOrder`. One grouping implementation, read by both the supervisor (how deep is each key) and the claimer (which group next) — two would be two answers, and they have to agree or replicas oscillate against a claimer working on something else |
| `worker.py` | `WorkerKey.scope`, `ForkExecutor` resolving a bundle **per version**, `_drain_tenant`'s peek → warm → fork loop, and `warm()` for pre-warming inside one claimer |
| `broker/server.py` | Per-agent `config_for`/`egress_for` (one broker, every agent), `public_routes` moved here so the redaction lives next to the credential, and `_Owned` re-keyed by **dispatch** |
| `broker/client.py` | A nested call gets its own connection — the deadlock fix (below) |
| `orgs.py` | **New.** D29/D35: budget vocabulary, the cross-workspace rollup, and the derived per-workspace verdict |
| `store.py` / `store_postgres.py` | `lease_acquire`/`lease_release`/`lease_get`, `rya_leases`, authoritative on Postgres in one statement |
| `execution/supervisor.py` | `_tenant_targets`, the lease, and the passive mode |
| `rya orgs` | `create`/`list`/`assign`/`budget`/`show`/`reconcile`; `rya worker --scope --prewarm`; `rya supervisor --scope --no-lease` |

**Exit criteria** — all five, with the named assertions:

- [x] A tenant with 5 agents × 2 live versions occupies **one** sandbox while active,
      not ten — `test_five_agents_with_two_versions_each_occupy_one_claimer` builds
      exactly that scenario and asserts the narrow scope wants **10** keys and the wide
      scope wants `{default:*:*}`. The e2e's `phase_tenant_scope` asserts it against
      the real deployment: one registration, key `default:*:*`, several agents warm
      inside it
- [x] A version rollout does not transiently double sandbox count —
      `test_a_rollout_does_not_transiently_double_the_sandbox_count` promotes a second
      version mid-flight and asserts the planned key set is unchanged, with a guard
      that both versions really are live (the test would pass trivially otherwise)
- [x] An approval resuming on a retired version does not require a dedicated sandbox —
      `test_an_approval_resuming_on_a_retired_version_needs_no_dedicated_sandbox`, and
      it asserts the *contrast*: the narrow-scope supervisor correctly refuses to start
      anything (a worker on a retired version would raise `E_VERSION_RETIRED` on every
      attempt, so the resume waits for a human), and the wide scope forks it
- [x] A handler-set hole is still detected **before** the job is claimed —
      `test_a_handler_set_hole_is_detected_before_the_job_is_claimed` checks the item
      is still `pending`, still unclaimed and still on attempt **zero** after the
      refusal, which is what "before claiming" has to mean to be worth anything. Plus
      `test_one_agents_broken_bundle_does_not_stop_a_sibling`
- [x] One agent cannot starve a sibling agent inside the same tenant sandbox —
      `test_one_agent_cannot_starve_a_sibling_inside_one_claimer` (8 queued for one
      agent, 1 for the other, and the sibling is served inside three dispatches) plus
      `test_fair_order_gives_equal_dispatches_not_depth_weighted_ones` for the policy
      without processes

### What the work surfaced

**A mediated `ctx.llm.respond` deadlocked the fork, and had since Phase 4.** The
worst finding of the phase and nothing to do with the scope. `turns.py` wires
`on_token` to `store.stream_append`, so a streaming model call writes to the store
*from inside itself* — and `BrokerClient.call` held a non-reentrant lock across the
whole exchange. Every mediated streaming turn wedged its fork until the queue lease
expired. A reentrant lock would have been worse than the deadlock: the nested request
would have gone out on a socket with a reply still in flight and the two exchanges
would have read each other's frames. So a nested call gets its **own connection** —
which then required the broker to key authority by **dispatch** rather than by
connection, because the second connection had no right to write the run it was
streaming.

Phase 4 did not catch it because its mediation e2e used a *hostile* agent, and that
agent never calls the model. The general lesson is narrower than "test more": an
adversarial fixture tests the boundary and not the path, and the path needed an
ordinary agent doing an ordinary thing.

**The wide scope made D22's after-the-claim filter pathological.** `queue.claim`
releases a job belonging to another agent rather than executing it — correct, and it
counted that release against `limit`. With several agents' turns interleaved in one
queue *by design*, a fork asking for one item claimed the oldest, released it, and
reported "nothing to do": with N agents active a dispatch had roughly a 1-in-N chance
of finding its own work and the deepest backlog decided whose. Fixed by retrying past
a release (bounded by `MAX_CLAIM_LIMIT`, and safe only because released jobs are held
until the loop ends, so the same row cannot come back twice). The *cost* is not fixed
and the docstring says so — the SQL predicate `queue.py` declined in Phase 2 is now a
named re-plan trigger rather than a pre-optimisation.

**Phase 4's `sandbox_env` was written for the wrong process.** It builds an
environment with no DSN, which is exactly right for the process that imports tenant
code and impossible for the process that has to open the database and *be* the broker
— and both were the same container. A `docker` or `kubernetes` claimer would have
started, fallen back to a FileStore inside its own container, claimed nothing, and
reported idle ticks indistinguishable from "no work to do". Nothing detected it
because the container path was never run end to end: the mediation e2e used `local`
and the container drivers were tested on their rendered arguments. The gate now has a
fourth condition and the untrusted posture is unlaunchable on every shipped driver,
which is the honest state (D32).

**The `preflight` concession §7.1 made was unnecessary.** The design doc said the
guarantee would degrade from "before claiming" to "after claiming, in the fork". It
does not: the claimer *peeks* at the queue, warms the version the next item is pinned
to — which is the import, and therefore the preflight — and only then forks a child
that claims. Ordering, not luck. The peek being allowed to be stale is what makes it
safe.

**`limit` had to mean items, not attempts.** The first cut spent a dispatch slot on
a group that turned out to be empty, so a tick with five groups and four already
drained by a sibling claimer did almost no work while reporting a full budget. Found
by a fairness test asserting 3 and getting 2.

**Every mediated claimer since Phase 4 leaked its broker's socket directory**, and it
was found by counting `/tmp` rather than by any test. `ForkExecutor.close` stopped the
warm pool and left the `BrokerServer` running, so a 0700 directory with a stale socket
survived each claimer — one per exit, which is invisible on a laptop and unbounded on a
box whose supervisor recycles claimers for a living. 146 of them after one suite run;
zero now, with a test that asserts the directory is gone. It fixes the *orderly* exit
only: the e2e still leaves two behind because it SIGKILLs its claimers, and a process
that is killed cannot clean up after itself — that residue is a `/tmp` reaper's job, not
the platform's. The order matters and is
commented: templates first, then the socket, because closing the socket under a child
mid-call turns an orderly shutdown into `E_BROKER_UNAVAILABLE` inside a handler that was
about to finish.

**One broker serving many agents splits cleanly, and the split is informative.** What
had to become per-agent is exactly what a *manifest declares* — model routes and
secrets, plus the agent-qualified guard policy (D28). What stayed per-tenant is
everything that came from the *deployment*: the store, the key ring, the quota check.
That is the line between tenant declaration and operator configuration, and it fell
out of the refactor rather than being imposed on it.

**The org rollup cannot live on the admission path, and saying why produced D35.**
Summing an org's meter needs a connection that spans workspaces; putting one on every
tenant's every run would hand the hot path a credential that reads every other tenant
— exactly what Phase 4 removed from a far less privileged process. Hence a privileged
reconciler and a derived per-workspace verdict. The refusal message had to learn to
name *which boundary* refused, because a tenant told "workspace quota exhausted" while
its own usage is near zero will go looking in the wrong place.

**Open question 7 was worse than it read.** "Two supervisors both see the same depth"
undersells it: `observe` reconciles the registry against the **driver's** inventory,
and a second supervisor's driver inventory is empty — so it sees a fleet it did not
launch, counts it, and starts its own anyway. The over-provisioning is N× by
construction, not 2× by agreement.

**Two Phase 4 assertions were wrong rather than incomplete**, and both were in the
posture phase: they asserted that a sandboxed, mediated, network-restricted
`kubernetes` deployment *passes* the launch gate. Rewritten to assert the refusal.
Same shape as Phase 4's own correction to Phase 3's isolation criterion — an exit
criterion phrased as behaviour needs a test that exercises the behaviour.

### Carried forward

*All four were taken up by Phase 6; §8 records what each one cost.*

- **gVisor has still never been run.** §9 risk 0, unchanged and now a phase older.
  This is the only Phase 4 or Phase 5 exit box still open, and Phase 5 deliberately
  did not multiply sandbox *density* in a way that depends on it — the tenant scope
  reduces sandbox count, so it makes the unproven thing less load-bearing rather than
  more.
- **The template host (D32).** Until an independently-startable template exists inside
  the sandbox, the untrusted posture is unlaunchable on every driver. Phase 6's first
  item, and design §7.2 has the shape.
- **The claim walk's cost.** Correctness restored, efficiency named as a trigger.
- **Org reconciliation has no scheduler.** `rya orgs reconcile` is idempotent and
  meant for a cron; nothing in the platform runs it yet, so an org budget is enforced
  only as often as an operator arranges. Deliberate — a built-in scheduler would be a
  fourth run mode — but it is a documented gap, not a finished feature.

---

## 8. Phase 6 — Close the carried-forward list ✅ COMPLETE

**Property:** nothing the previous five phases deferred is still deferred, and the
untrusted posture is launchable rather than merely designed.

Not a planned phase. Phases 0–5 each left something behind, and Phase 5's list had
grown to four items — three of which had been carried for more than one phase, which
is the point at which a carried item stops being a deferral and starts being a
decision nobody made. So this phase has no new property of its own; its content is
the list above.

| Item | Change | Status |
|---|---|---|
| **D32** | The template host: an independently-startable, credential-free template inside the sandbox, so the claimer can hold the DSN in a container of its own | ✅ `execution/host.py`, `rya template-host`, and both container drivers render the pair |
| **§9 risk 0** | Run gVisor. D23's third-party-wheel criterion, open since Phase 4 | ✅ **run** — and it found a live defect in the isolation probe |
| **§9 trigger** | "Nobody runs `rya orgs reconcile`" | ✅ `supervisor.reconcile_orgs` + `orgs.freshness` |
| **§9 trigger** | `x86_64` re-measurement of the Phase 0 numbers | ❌ **not possible here** — this host is `aarch64`. Still not taken, and still a trigger |

**Outcome.** `scripts/e2e_platform.py`: **156 passed, 0 failed, 0 known gaps** — up
from 146/0/0. The suite is **893** passed without Postgres and **950** with it, both
with **zero failures**.

### Exit criteria

- [x] **A container driver passes the launch gate.** `topology_supported` returns
      `True` for `docker` and `kubernetes`, and `check_untrusted_posture` on a
      RuntimeClass-configured `kubernetes` driver with mediation and egress in force
      reports `ok` with nothing unmet. Phase 5's version of this assertion had to fake
      `launched_unit` because no driver launched the pair; the fake is gone.
- [x] **A driver that launches only the sandbox is still refused.** The refusal names
      `rya template-host` and the shared directory, because the arrangement is the one
      a third-party driver is most likely to write by accident.
- [x] **The tenant's interpreter is not a descendant of the claimer.** Asserted from
      the handler's own report: `test_the_template_is_not_a_child_of_the_claimer`
      starts a real `rya template-host` subprocess with an allowlisted environment,
      runs a real turn through it, and reads `/proc/<template>/status` to confirm the
      template's parent is the host and not the claimer.
- [x] **The three D23 dependencies work under a real sentry.** `cryptography`,
      `pydantic-core` and `psycopg` — plus `yaml`, `httpx` and `os.fork` — under
      `runsc release-20260727.0`. Six of six.
- [x] **The isolation probe recognises a real sentry in the hardened
      configuration**, i.e. with `dmesg` unreadable, which is what `--cap-drop=ALL`
      produces. This is the one that failed first.
- [x] **An org budget nobody reconciles says so.** `orgs.freshness` distinguishes
      `none` (no org — ordinary) from `stale` (a verdict that is not being refreshed),
      and the supervisor's fan-out refreshes it.

### What the work surfaced

**The isolation probe was inverted, and only running gVisor could show it.** The
`/proc/version` marker was the literal `4.4.0`, from a captured fixture;
`release-20260727.0` reports `4.19.0-gvisor`. What makes this worse than a missed
signal is that `read_isolation_signals` treats *a version string that is not gVisor's*
as positive evidence of a host kernel — so a genuine sandbox was **actively refuted**,
`effective` downgraded to `shared-kernel`, and the launch gate refused. And it refused
in precisely the configuration the platform launches: `hardening_args` always passes
`--cap-drop=ALL`, which is usually what makes `dmesg` — the signal that was still
working — unreadable. A correctly hardened, genuinely sandboxed deployment would have
been told it was not sandboxed.

That is §9 risk 8's thesis applied to itself. The claim nobody had checked was not
"gVisor works"; it was "we can tell whether gVisor is working". It failed safe, which
is why it could survive two phases, and it would have presented as an unexplainable
refusal rather than as a bug. **A fixture is a recording, not a measurement**, and the
distinction is only visible from the other side of an actual run.

**`sandbox_env` was never the thing that was wrong.** Phase 5 read the defect as "this
function configures the wrong process" and left the fix as future work. Building the
fix showed the framing was off by one: `sandbox_env` and `worker_env` describe
*opposite* processes and both were correct. What was missing was the second container.
Once `claimer_env` exists beside `sandbox_env` there is nothing to repair in either —
which is why the diff to the drivers is mostly additive and why the trusted posture got
a fix it was not asking for. The single trusted container had no DSN either.

**`WarmPool` did not change, and that is the second time D27's keying paid.** Phase 5's
claim was that widening the claimer *scope* would be configuration because the pool is
keyed by bundle hash rather than by container. Phase 6 moved the templates into a
different container entirely and the pool needed one branch in a new `_build` helper.
Same keying, same LRU, same eviction. Content addressing decoupled *which* interpreter
serves what from *where* it lives, and both of those turned out to be the questions
that moved.

**A credential heuristic built for environments does not transfer to a schema.** The
first cut of the host's start-request guard ran `inventory.classify` and refused its
`ambiguous` bucket, which rejected an ordinary model route because `maxTokens` contains
"TOKEN". `inventory.py` says in as many words that ambiguous names are *not* removed
because "a shape-based heuristic is not a good enough reason to break a handler" — and
that bucket exists for an open set of unknown variable names where a human decides. A
wire schema is a closed set this codebase defines, so the heuristic has nothing to add,
and a false positive there is not a warning somebody reads: it is a tenant whose agent
will not warm. Found in the first end-to-end run, not by a test.

**A bench container leaves a root-owned directory in the repo.** `.rya/bench` is
created by whichever privileged container ran first, so the developer who ran it cannot
subsequently write there — and `verify_gvisor.sh`'s first version failed on its own
output redirect. Fixed by writing from the side that owns the directory. Small, but it
is the kind of thing that makes a script look broken when it is the filesystem.

### Carried forward

- **`x86_64` is still unmeasured.** This host is `aarch64` and always was. The §9
  trigger stands unchanged, and both bench scripts accept `--label` for the day
  somebody has the hardware.
- **The gVisor numbers are still nested.** `verify_gvisor.sh` runs `runsc` inside a
  privileged container with `--ignore-cgroups`, because the host has no `runsc`, no
  passwordless sudo, and AppArmor blocks unprivileged user namespaces. That is fine for
  the *correctness* criterion — the sentry is real and so is the syscall interception —
  and it is not fine for the cost model, which still carries §11's caveats.
- **D32's "best" row is not built.** The broker is a sibling container, not a process
  outside the sandbox. Reaching the last row needs a control channel across the sandbox
  boundary, which means a network listener where there is currently a Unix socket —
  a worse trade than the one being made. Design §7.3 records it as a decision rather
  than a gap.
- **The pair has no startup ordering in Kubernetes.** Both containers start
  concurrently and the host has no readiness probe, so a claimer's first warm can lose
  the race on a cold pod. It recovers through the ordinary reclaim path, so the cost is
  a delayed first turn. New design §9 risk 18.

---

## 9. What this plan deliberately does not do

**No dates, and no engineer-weeks.** Sizes are relative (`S`/`M`/`L`). Converting
them needs team size, allocation and how much of this is concurrent with other work
— none of which are technical inputs. Given a capacity number this becomes a
schedule in one pass; inventing one now would produce a plan that looks committed
and is not.

**No claim that the phases are equal.** Phases 1–3 are roughly the size of Phase 4
put together, which restates the design doc's §6 warning in scheduling terms: *do
not read Phase 1's four small items as progress toward the hosted product.* They
make the existing posture correct. Phase 4 is the product.

**No reordering to ship the exciting part first.** Multi-agent (Phase 2) is the
visible capability and it is deliberately behind Phase 1, because Phase 1 contains
a live cross-agent defect (#6) that multi-agent makes easier to hit.

---

## 10. Re-plan triggers

Named in advance so hitting one reads as information rather than failure.

| Trigger | Status | Consequence |
|---|---|---|
| Fork + import ≈ 2s budget even warm | **did not fire** — 791 ms worst case under gVisor, 40% of budget | Phase 5 is cancelled; narrow scope becomes permanent; sandbox count stays ∝ N×M×V and the cost model needs redoing |
| `runsc` overhead materially above target | **did not fire** — ~1.5× tax, 67% of budget worst case | D23 is reopened. Per-tenant node pools with bin-packing become competitive; the #18 seam is what makes that swap survivable |
| Open question 1 answered "pooled platform key" | 🔥 **FIRED** — D30 | **Applied in §6:** #12 promoted to tier 1 beside #11, and #21 (quota enforcement) added as a launch requirement |
| Open question 2 answered "workspace ≠ tenant" | 🔥 **FIRED** — D29 | **Applied:** boundaries re-read against the billing boundary. Result — no isolation boundary moves; `org_id` is additive (#20 in Phase 1) and only quota/usage become org-aggregated (§7) |
| A tenant needs per-agent resource limits contractually | not seen | Phase 5's wide scope conflicts with it; narrow scope becomes a product tier rather than a stepping stone |
| Untrusted launch pressure arrives before Phase 4 completes | not seen | **No shortcut exists.** The honest options are: delay, or launch the trusted posture only and say so. The Phase 3 fail-closed check exists precisely so this cannot be resolved quietly by configuration |
| **(new)** A tenant's module-scope imports exceed the cold-start budget | not seen | From the §11 measurements: cold start is mostly tenant-controlled and gVisor multiplies it ~1.5×. Forces open question 5 — an import-time budget at publish, per-plan targets, or documented degradation |
| **(new)** `x86_64` re-measurement diverges materially from `aarch64` | not taken | The §11 numbers were taken on `aarch64` with `systrap` and no KVM. Both scripts accept `--label`; re-run before committing the Phase 4 cost model |
| **(new)** The api and its workers disagree about which environment they serve | **mitigated, not closed** | From Phase 2: D21 makes the control plane decide the version pin from the environment pointer, so a mismatch pins to nothing and every turn sits unclaimed. Phase 3 removed it from the supported path — the supervisor knows which environment it scheduled for and the driver forces it onto the worker — but a hand-started `rya worker` can still disagree and nothing detects it. Open question 4 stands for that case |
| **(new)** A warm template dies repeatedly under load | not seen | From Phase 3: `E_TEMPLATE_LOST` is reported rather than retried, on purpose, because "the interpreter holding this tenant's code keeps dying" is usually a memory limit and a retry loop would hide it. If it turns out to be common, the supervisor needs a per-key backoff rather than the pool needing a retry |
| **(new)** `backlog_per_worker` turns out to be the wrong shape of knob | not seen | From Phase 3: the supervisor scales on queue *length* because the platform has no per-item service-time estimate — turn durations span a mocked reply and a ten-minute tool loop. If replica counts oscillate or lag badly, the fix is a measured service time (which the meter could supply), not a different constant |
| **(new)** `psycopg`, `pydantic-core` or `cryptography` misbehaves under `runsc` | **did not fire — Phase 6** | Run for real by `scripts/verify_gvisor.sh` against `runsc release-20260727.0`: all three work, plus `yaml`, `httpx` and `os.fork`. D23 stands and the Kata / per-tenant-node-pool alternatives stay parked. The run found something else instead — the isolation *probe* was inverted (§9 risk 0), which is a different failure than this trigger anticipated and a worse one, because it refused a working sandbox rather than admitting a broken one |
| **(new)** A supervisor is deployed as a multi-replica Kubernetes Deployment | **closed in Phase 5 — D34** | The lease shipped. A supervisor that cannot take it goes passive rather than doubling the fleet, and per-workspace keying means two supervisors *split* a hundred tenants instead of one idling. Building it found the failure was N× by construction rather than 2× by agreement: `observe` reconciles against the driver's inventory, and a second supervisor's is empty |
| **(new)** The broker's lifetime becomes a availability problem rather than a coupling | **decided in Phase 5 — D32; built in Phase 6**, and the *availability* half is still open | Picking the topology found something worse than a disagreement: `sandbox_env` builds an environment with no DSN, so the container it configures cannot be the claimer, so the container drivers could never have run an untrusted claimer at all. The gate refused them until Phase 6 built the template host, and the framing turned out to be off by one: `sandbox_env` was never wrong, the second container was missing. What remains untouched is the original question's other half — the broker's lifetime still bounds a dispatch, and `E_BROKER_UNAVAILABLE` mid-run is still reachable, now across a container boundary rather than a process one |
| **(new)** The after-the-claim agent filter becomes a throughput problem | **not seen, and now measurable** | From Phase 5: `queue.claim` releases a sibling's job rather than running it, so a fork can walk up to `MAX_CLAIM_LIMIT` items to reach its own. Correctness is fixed; the cost is bounded and real. If it bites, the answer is the JSONB predicate `queue.py` declined in Phase 2 — and the *shape* of that predicate depends on whether the pressure is depth (one agent, huge backlog) or breadth (many agents, shallow each), which is why it was not pre-optimised |
| **(new)** A tenant's warm-interpreter working set exceeds its memory limit | not seen | From Phase 5: at the wide scope the pool holds one interpreter per hot version of every agent a tenant owns (default 12), inside one sandbox with one cgroup. §7.1 records "per-agent resource limits" as the thing this scope gives up, and this is where that bill arrives. The knobs are `RYA_POOL_MAX_ENTRIES` and the sandbox memory limit; the fix if it is chronic is per-plan pool sizing, not a bigger default |
| **(new)** Nobody runs `rya orgs reconcile` | **closed in Phase 6** | From Phase 5: an org budget is enforced through a derived per-workspace verdict (D35) and nothing in the platform refreshes it. A deployment that sets a budget and never schedules the reconciler has a budget that caps nothing — the same failure mode `quotas.py` refuses for a mistyped limit, arrived at by omission instead. Answered with the second of the three: `supervisor.reconcile_orgs` on the multi-workspace fan-out, not inside `Supervisor.tick` — a `Supervisor` is scoped to one workspace and an org rollup spans them, so the tenant-scoped object never gains the cross-workspace read D29 keeps out of it. Plus `orgs.freshness`, for the deployments that run no supervisor at all: a verdict nothing refreshes now says so rather than presenting itself as current |

---

## 11. How we know, overall

The repo has 75 test files and 907 test functions, and
`scripts/e2e_platform.py` runs an end-to-end platform smoke test that reports
`PASS` / `FAIL` / `GAP` — a `GAP` being "a check that documents a known
platform defect rather than a broken test" — printed and summarised, but it "does
not fail the run, so the harness stays useful in CI while the gap is open".

**That script is the plan's scoreboard.** It read 55/0/**4** before Phase 1,
66/0/**1** after Phase 2, 87/0/**0** after Phase 3, 129/0/0 after Phase 4, 146/0/0
after Phase 5, and reads **156 passed, 0 failed, 0 known gaps** after Phase 6. Every
gap the script carried was closed in Phase 3, so from there on a phase has to *add*
assertions rather than flip one. Phase 2 added `phase_multi_agent`; Phase 3 added
`phase_supervisor` and `phase_fork_execution` plus three checks inside
`phase_isolation`; Phase 4 added `phase_posture`, `phase_mediation` and
`phase_lifecycle`; Phase 5 added `phase_tenant_scope` and `phase_supervisor_lease`, and
**rewrote two of Phase 4's** that asserted a launch-gate pass which was not true; Phase
6 added `phase_template_host` and **rewrote those same two again** — this time from a
refusal back to a pass, because the thing they were waiting for got built.

That one pair of checks has now been rewritten in three consecutive phases, and the
sequence is the most useful thing in this section. Phase 4 asserted a pass it had not
earned. Phase 5 asserted the refusal and named what was missing. Phase 6 built it and
asserted the pass again. **The refusal was load-bearing**: it is what made the gap
impossible to forget, and it is why the third rewrite is a deletion of a workaround
rather than a discovery of a two-phase-old bug.

**Phase 4's assertions are adversarial, and that earned its keep immediately.**
`phase_mediation` publishes a deliberately hostile agent and asserts against *its own
report* — the handler reads `os.environ` for every credential D18 names, tries to forge
a metering row, reaches for the execution plane, calls `set_config`, and issues a raw
request to the cloud metadata endpoint. "The handler could not find a DSN" is a
stronger statement than "we believe we removed the DSN", and it is the only way to
check a property whose whole content is what an attacker can reach.

`phase_posture` then found the phase's most important bug by asserting the *refusal*
rather than the mechanism: the launch gate did not fire, because Phase 3 had wired the
isolation check into `rya supervisor` and nothing else. A hand-started `rya worker`
walked past it. Three more real defects came out the same way — a purge that shredded
nothing while reporting accurately (the key sentinel), a supervisor that could launch a
worker for the wrong agent, and an e2e assertion of mine that passed while proving less
than it claimed. The general lesson is worth keeping: **a gate is only as good as its
least-guarded entry point, and the way to learn that is to attack it rather than to
read it.**

**Phase 5's assertions had to run an ordinary agent, and that is what earned them.**
Phase 4's adversarial fixture tested the boundary and could not test the *path*: its
hostile handler never calls the model, so a deadlock on every mediated streaming turn
sat undetected behind a green suite and a green scoreboard. `phase_tenant_scope`
publishes nothing special — it drives the scaffolded agents through one tenant claimer,
and the check that found the bug is "a mediated `ctx.llm.respond` completes rather than
wedging the fork". The pair is the lesson: **an adversarial test proves what an attacker
cannot do, and an ordinary one proves the product works.** Neither substitutes for the
other, and Phase 4 shipped only the first.

Phase 5 also spent four of its checks failing on purpose before they passed — a
precondition timeout tuned to exactly the liveness window, a pre-warm assertion that
did not expect a third agent, a plan assertion that counted reap actions as starts, and
a provider key that turned an offline stub into a real network call. All four were the
*harness* being wrong about the product rather than the reverse, which is worth
recording because the first instinct on a red check is the other diagnosis.

**Phase 6's red check was the other diagnosis, and it took two runs to believe it.**
`phase_template_host` started the pair with `--scope tenant` and no `--fork`, and got
`E_BROKER_UNAVAILABLE: broker mediation was requested without --fork`. The obvious
reading is a harness that forgot a flag — Phase 5 had just produced four of those. It
was not: the CLI help says `--scope tenant` "implies `--fork`", `drivers.worker_argv`
renders both, and `start_worker` did neither. What the harness actually exposed is that
the mediation check ran *before* the scope was resolved, so it reported the wrong flag
to an operator who had no reason to type it. The fix needed `fork` to become tri-state,
because "not asked" and "asked for `False`" want opposite answers at the tenant scope
and a `bool` cannot say which one happened.

So the running lesson has a counterweight now: the first instinct on a red check is
"the harness is wrong", the second is "the product is wrong", and **the way to tell
them apart is which document the product disagrees with.** Here it disagreed with its
own `--help`.

Phase 6's other lesson is about what a test cannot substitute for. The isolation
probe's positive path had a passing test in every phase from 4 onward, written against
a captured fixture, and the fixture had the wrong kernel string — so the test asserted
that the platform correctly recognises a gVisor that does not exist. **A fixture is a
recording of an assumption, and it will keep confirming that assumption forever.** The
only thing that could break it was `scripts/verify_gvisor.sh` running a real sentry.

A phase is done when its box list is ticked and the scoreboard says so. Not when
its issues are closed.
