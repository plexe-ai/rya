# Migration plan: ChatStudyAbroad → Rya (csa-counsellor)

This is the living migration spec for porting the production ChatStudyAbroad agent onto Rya.
It sits alongside [`PRD.md`](./PRD.md) (what the agent *is and must do*) and the Rya project in
this folder (`rya.agent.yaml` + `src/agent.py`). PRD §15 gives the high-level primitive
mapping; this document is the detailed, phased engineering plan — including the **core Rya
runtime changes** the port requires, not just the example wiring.

> **Revision 2 — corrections applied after source review.** The following claims in rev 1 were
> verified wrong against the actual source and are fixed below:
> 1. **`send_email` is NOT a stub** — prod sends real mail via `sendAgentEmail`
>    (`client.ts:2520`) behind a preview/confirm gate. We port it as *real send behind the
>    durable approval gate* (not a no-op).
> 2. **Id-secrecy regex** is prod's exact `\b[A-Za-z]{3,8}\d{8,}\b` → `"(id hidden)"`
>    (`utils.ts:135`), which deliberately preserves passports / numeric CAMS ids / phones. The
>    looser rev-1 pattern over-redacted.
> 3. **Student-key adoption.** Rev 2 held that an "empty pin rejects → model retries" path can't
>    work because an unresolved pin overwrites the arg with `None` and calls the handler anyway
>    (`context.py:894`), so prod *reorders* `create_lead` first (`stream.ts:200`). **Resolved in
>    Phase 3 by A5 option (c)** (see §A5): a declarative `adopt:` primitive writes the key into
>    `student_state` memory on success and the pins read `memory.student_state.camsId`, so the
>    empty-pin handler rejection becomes *sound* (the re-emitted call resolves the adopted key) —
>    no loop reordering needed. Gated in `tests/test_agent_phase3.py`.
> 4. **No `upsert_connection` exists** (`store.py` has create/get/revoke/reseal only), so A3
>    real login-minting is net-new core work, not free.
> 5. **`create_lead` idempotency** depends on prod's `passportAlreadyExists` guard
>    (`leads.ts:360`), which rev 1 omitted — declarative retry without it double-creates leads.
> 6. **No hard 8–15 rec cap in prod** (schema is `.min(1).max(30)`, guidance is soft). Caps and
>    the "self-heal" framing are corrected below.
> Framing corrections: the governed tool loop, per-user connections, `emit_ui`, memory scopes,
> and approvals durability **already exist** in the runtime — so Track B is smaller than rev 1
> implied. Prod cards are **client-rendered** from tool results; moving them to `emit_ui` is a
> deliberate re-architecture, not a mirror.

## Context

`chatstudyabroad/` is a live study-abroad counsellor agent (Next.js + Anthropic TS SDK): a
6-turn Sonnet tool loop over a 28-tool registry (Crizac CAMS, Plexe, app-local), with
per-counsellor auth, memory, inline cards, and *prompt-convention* safety (master-id secrecy,
currency grounding, confirm-before-write, human-only apply). We are porting it onto **Rya** so
those guarantees become **runtime primitives** and turns become durable.

`csa-counsellor/` exists today as the **governance shell**: the manifest declares all 28 tools
with correct permission tiers, `pin:` on student-scoped tools, model routes, memory
collections, egress + grounding guard, and one durable email-approval gate.

**The gap:** the shell is *only* a shell. `src/agent.py` calls single-turn
`ctx.llm.respond(route="compose")` — never the tool loop — and **zero of the 28 tools are
wired**. Nothing the agent "does" actually runs. This plan takes the migration to functional
parity.

## Guiding principle: split-and-port

We reimplement in **Python** — no permanent proxy to the Next.js app. For every capability we
**split**:

- **Infra / cross-cutting concerns → promoted into core Rya** as reusable primitives (tool-call
  retry+repair, id-secrecy scrubbing, per-counsellor login credential, deterministic
  student-key adoption). Generic; every agent benefits.
- **Business logic → Python `@agent.tool` handlers** in csa-counsellor (name-resolution
  cascade, ranking, card shaping, domain error-repair mappings, Plexe payload assembly).

What already exists in the runtime and is **not** core work: the governed model-driven loop
(`ctx.llm.run`, `context.py:471`), durable approvals, `ctx.emit_ui`, identity-scoped memory,
per-`(provider, owner)` connections, and pins. Track A is only the genuinely-missing pieces.

Two interleaved workstreams: **Track A — core Rya** and **Track B — csa-counsellor handlers**.

## Decisions locked

1. Retry/repair primitive: **declarative manifest policy + a registered repair callback**
   (A1). Note this is a *deliberate redesign*: prod's transient-5xx retry is code, but its
   domain self-heal (closest valid destination/course type, home-state spelling) is a
   *system-prompt instruction to the model*, not a repair map. We move it into deterministic
   core+handler code (per the "self-heal → core" decision) and keep the prompt guidance as
   defense-in-depth.
2. Per-counsellor auth: **mirror chatstudyabroad's token mechanism** (Crizac `/v1` login → held
   bearer → passed per turn → `401` surfaces reconnect, no silent refresh). Net-new core work
   is narrow: `upsert_connection` + an `E_CONNECTION_EXPIRED` reconnect signal (A3).
3. `student_refresh`: **`ctx.emit_ui` frame**, emitted **only after a real state change**
   (mirrors prod's `toolOk` guard, `stream.ts:236`).
4. Caps (corrected to match prod): **shortlist 15** enforced in handler (prod does too,
   `shortlist.ts:137`); **apps 5** enforced in handler — this *adds* enforcement prod's agent
   loop never had (UI/prompt only), acceptable but new; **recs** have **no hard cap** — prod
   schema is `.min(1).max(30)` with soft "about 8–15 / about 5" prompt guidance. The handler
   validates shape, it does not hard-reject on count.
5. `send_email`: **real send behind the durable approval gate** (prod sends real mail; do not
   regress to a no-op). Flag if you want it stubbed for early phases instead.
6. Wiring: **complete Python implementation, no proxy**, from Phase 1.

---

# Part 1 — Core Rya feature changes (Track A)

Each is a standalone runtime feature under `src/rya/`, with csa-counsellor as its first
consumer. Landed before/with the CSA phase that needs it.

## A1. Declarative tool-call retry + repair callback

**Why:** prod `create_lead` retries once on transient 5xx (`leads.ts:327-346`, `isTransient`
excludes 4xx/auth) and *self-heals* recoverable errors — but the self-heal is a prompt
instruction to the model, not code. We make it deterministic. Today only `ctx.jobs` retries
(`engine.py`); tool calls do not.

**Changes:**
- `src/rya/manifest/schema.py` — extend `ToolDecl` with an optional `retry` block:
  ```yaml
  retry: { max_attempts: 2, backoff: exponential, on: [5xx, timeout] }   # NEVER 4xx/auth
  ```
- `src/rya/sdk/context.py` `_Tools.call` (~line 868) — wrap the chosen backend in the retry
  loop: classify the error and retry with backoff while attempts remain and the class is in
  `on`. **The classifier must exclude 4xx/auth exactly as prod's `isTransient` does.**
- New decorator `@agent.repair("<tool_id>")`. On a *recoverable* error (a new
  `RyaRecoverableToolError` carrying a machine-readable reason + upstream body), the runtime
  invokes the registered repair callback once with `(input, error)`; it returns a **patched
  input** (or re-raises to surface). Journaled as a `tool.repair` step.
- **Idempotency + attempt accounting (required, was missing):**
  - **One retry owner.** Ported handlers must *not* also retry internally — pick the runtime
    wrapper as the single owner, else prod's inner retry × the wrapper = 4 attempts.
  - **Return vs raise.** Prod handlers *catch* transient errors and return `{ok:false, reason}`
    rather than throwing; a ported handler that returns a structured failure bypasses a
    classifier that only sees raised errors. Handlers to be retried must **raise** the typed
    error, not return it.
  - **A repaired retry consumes a `max_attempts` slot** (cap worst-case upstream writes).
  - **Idempotency key.** Port prod's `passportAlreadyExists` guard (`leads.ts:360`, run before
    creation) into the `create_lead` handler and run it on **every** attempt, so a
    POST-succeeds-then-response-times-out path does not double-create.

**Verify:** evals — recoverable "invalid destination" repairs+succeeds; **timeout-then-success
does not double-create** (idempotency gate before Phase 3 ships).

**SHIPPED (Phase 3).** `RetryDecl` on `ToolDecl` with `max_attempts`/`backoff`/`on`; the
classifier (`_Tools._error_class`) returns only `timeout` (E_TIMEOUT/`TimeoutError`) or `5xx`
(HTTP tool `http_status` 500–599) — a 4xx/auth is `None` and never retried, exactly like prod's
`isTransient`. `RyaRecoverableToolError(reason, detail)` + `@agent.repair`; `_invoke_with_recovery`
unifies retry+repair across the agent/HTTP/mock backends in one journaled step (so a replay after
an approval pause returns the memoized final result — retries/repairs never re-run). Two
deliberate refinements vs. the sketch: (1) the classifier reads `http_status` off the error
rather than scraping the message; (2) **repair is capped at once and is orthogonal to the
transient budget** (a self-heal does not consume a `max_attempts` slot) — worst-case upstream
writes are still bounded because repair fires at most once. Idempotency is the handler's job:
`create_lead` runs its `passportAlreadyExists` check first on **every** attempt, so a
commit-then-lost-response retry returns the existing lead. The `on:` YAML-1.1 boolean footgun
(`on` → `True`) is rescued in `RetryDecl` so the policy is never a silent no-op.

## A2. Id-secrecy output guard (per-result hook + outbound)

**Why:** prod runs `scrubMasterIds()` in **three** places — before the model sees a result
(`stream.ts:249`), at render (`chat-view.tsx:965`), at persist (`chat-view.tsx:603`). Rya only
grounds *money* figures (`guard.py:218`) and the tool loop appends the **raw** result to model
context (`context.py:524`) with no interception hook. This primitive closes that gap once, at
the tool boundary.

**Changes:**
- `rya.guard.yaml` — new `secrecy` block, using **prod's exact pattern**:
  ```yaml
  secrecy:
    enabled: true
    patterns:
      - { id: crizac_master_id, kind: regex, pattern: "\\b[A-Za-z]{3,8}\\d{8,}\\b",
          replacement: "(id hidden)",
          note: "3–8 letters + 8+ digits = master id. Preserves passports, numeric CAMS ids, phones." }
    apply_on: [tool_result, outbound]
    action: scrub
  ```
- `src/rya/guard.py` — add `secrecy_scrub` and `secrecy_check`. **Scrub per string-leaf of the
  parsed dict, not a serialized JSON blob** (tool results are dicts, `context.py:524`); the
  replacement token must be quote-safe so JSON is never corrupted.
- `src/rya/sdk/context.py` — invoke the scrub in two places: (1) `_Tools.call` **after the
  handler body returns, before the result enters the loop** (the missing per-result hook; also
  covers direct `ctx.tools.call`); (2) `ctx.channels.send` / outbound, beside the grounding
  gate (`context.py:964`). Ordering matters: the scrub runs *after* the handler so `create_lead`
  adoption (A5) still sees the raw numeric id.
- Expose `ctx.guard.check_secrecy(text)`.

**Handler-side complement (Track B):** the blanket scrub cannot mint the `"CAMS id pending"`
label prod uses for a brand-new lead with no numeric id yet (`utils.ts:117`) — a blanket scrub
would leave the model `(id hidden)` with no way to reference the student. That labelling stays
in the `cams_lookup_student` / `create_lead` handlers.

**Verify:** eval `master_id_never_surfaced`; **negative eval porting `utils.test.ts:249-257`**
— passports (`Z1234567`, `MEGHA1234`), numeric CAMS ids (`CAMS 1472802`), phones are
**preserved** (regex over-redaction gate before Phase 2 ships).

## A3. Per-counsellor Crizac credential (mirror chatstudyabroad)

**Framing (corrected):** per-user connections already exist — `_authorize_connection`
(`context.py:301`) resolves per-`(provider, owner=identity.sub)`, enforces the connection∩user
scope intersection, and seals the secret; `rya connect` stores per-user tokens
(`cli/main.py:441`). Net-new work is narrow: a runtime **write path** and a **reconnect
signal**.

**Changes:**
- **`upsert_connection(provider, owner, …)` (new core, required).** `store.py` has only
  create/get/revoke/reseal; `create_connection` always writes a **new** doc and
  `get_connection` returns the first user-owned active match in glob order — so repeated logins
  mint duplicates and the runtime can later inject a **stale token**. Add overwrite-in-place
  keyed on `(provider, owner)` in both `store.py` and `store_postgres.py`.
- **Login handler** in csa-counsellor (Track B) hits Crizac login using
  `ctx.secrets.get("CRIZAC_BASIC_AUTH")` + submitted email/password, extracts
  `data.token`/`agentId`/`counsellorNumber`, and calls `upsert_connection` for
  `(crizac, identity.sub)`. **Endpoint correction (verified against `client.ts:406-458`):**
  there is no `/v1/api/agentAuth/login`; the real paths are `POST /v2/api/agentAuth/login`
  (device mode: body `{email, password, tenantId, security:true, trustDevice:true,
  isCheckedTandC:true, currentVersion, deviceToken, deviceData?}`) and `POST
  /v1/api/agentAuth/loginWithAdmin` (SSO mode). Token via `extractToken` order
  `data.token → data.accessToken → token → accessToken`. `tenantId` is NOT in the login
  response — it comes from config (`CRIZAC_TENANT_ID`, default `"1"`). Base URL env is
  `CRIZAC_BASE_URL`.
- **`E_CONNECTION_EXPIRED` / reconnect signal (new).** A Crizac `401` in a tool call raises a
  typed error the runtime maps to a `reconnect` outcome on the stream (mirrors prod's
  `CrizacAuthError`, `client.ts:353`). No auto-refresh. NB this is net-new for the *chat* path:
  prod's clean reconnect banner lives on REST routes, not the SSE loop.
- **Fail closed on missing identity (required).** If the webhook channel does not forward the
  verified `X-Rya-User-Token`, `owner=None` and `get_connection` currently falls through to a
  **workspace-shared** connection (`store.py:418`) — a silent attribution leak. Make a missing
  `sub` a **hard failure** for `provider: crizac` tools.
- **Fail closed on plaintext at rest (required).** `seal()` silently falls back to plaintext
  when `cryptography`/key is absent (`seal.py:85`); prod always encrypts the bearer. Make
  plaintext connection storage a **`rya deploy --check` block** for `provider: crizac`.
- Store `agentId`/`counsellorNumber`/`tenantId` in identity-scoped memory (scope `"user"`) for
  `create_lead` ownership stamping.

**Known residual risks (note, not fixed here):** mid-turn expiry has no rollback — a raised
`E_CONNECTION_EXPIRED` aborts the turn and any already-committed side effect (e.g. a step-2
`create_lead`) is not undone (prod has the same exposure); and two concurrent turns for one
counsellor can race the mint (mitigated by `upsert_connection` overwrite-in-place).

**Phasing:** Phase 1 uses an honest **static `rya connect` seed** for one test counsellor; real
login-minting ships once `upsert_connection` + fail-closed sealing land. **This whole section
(A3) is deferred to Phase 5** — Phase 3's `create_lead` remains a local leaf (no Crizac egress),
so `upsert_connection`, login-mint, fail-closed sealing, and `E_CONNECTION_EXPIRED`/reconnect all
land in Phase 5 alongside their `expired_token_surfaces_reconnect` eval and the switch of the
local leaves to live `url:` tools. Phase 3's non-negotiable gates are A1 + A5 only.

**SHIPPED (Phase 5).**
- **`upsert_connection(provider, scopes, secret, owner, label)`** on both `Store` and
  `PostgresStore`, overwrite-in-place keyed on `(provider, owner)` (FileStore scans the glob and
  rewrites the existing doc's file; Postgres does a `SELECT … FOR UPDATE` on `owner IS NOT
  DISTINCT FROM` then `UPDATE`/`INSERT`), preserving `id`/`createdAt` and stamping `updatedAt`.
  Exposed as **`ctx.connections.upsert(provider, *, secret, scopes, label)`** (owner ← `identity.sub`,
  secret vaulted). A re-login refreshes the one doc — no stale-token duplicate for `get_connection`
  to later inject.
- **Reconnect signal.** `_http_tool` maps a `401` **on a request we authenticated** to a typed
  `E_CONNECTION_EXPIRED` (a credential-less 401 stays a plain `E_TOOL_UPSTREAM`); the engine maps
  that code to a distinct **`needs_reconnect`** run status (its own `run.needs_reconnect` trace,
  not `run.failed`), added to `TERMINAL_RUN_STATUSES` + the export list + the manual status
  allow-list. No auto-refresh — mirrors prod's `CrizacAuthError`.
- **Fail closed.** A new `ToolDecl.require_user` makes `_authorize_connection` raise `E_NO_IDENTITY`
  when no verified `sub` is present (instead of silently falling through to a workspace-shared
  connection). `rya deploy --check` blocks `E_PLAINTEXT_SECRET_AT_REST` when a `require_user`
  provider's stored connection is unencrypted (`seal()` degraded to plaintext).
- **Credential enforcement scoped to egress.** The key refactor that keeps the live switch
  offline-safe: `_Tools.call` now resolves/authorizes a connection **only when the backend will
  actually egress with it** (url/mock, i.e. `handler is None`). A local `@agent.tool` leaf never
  receives the secret, so a `provider:`/`require_user:` on it is governance metadata — the Crizac
  tools declare a live `url:` **and** keep their local handler (handler wins per `context.py`
  precedence), so a real deploy routes to Crizac with the per-counsellor bearer while the whole
  offline suite stays green with no connection or identity.
- **Login-mint.** The event handler's `_maybe_mint_crizac` exchanges a `crizacLogin:{email,
  password}` turn for a bearer (`/v2/api/agentAuth/login`, `extractToken` order
  `data.token → data.accessToken → token → accessToken`) using `ctx.secrets` (`CRIZAC_BASE_URL`,
  `CRIZAC_BASIC_AUTH`, `CRIZAC_TENANT_ID`), upserts `(crizac, this-user)`, and stashes
  `agentId`/`counsellorNumber`/`tenantId` in identity-scoped memory. Best-effort: unconfigured or a
  login failure logs and never crashes the turn.

**Residual (unchanged from the design):** mid-turn expiry has no rollback (a raised
`E_CONNECTION_EXPIRED` aborts the turn; an already-committed side effect is not undone — prod has
the same exposure); the reconnect signal is proven by the core deterministic gate rather than a
live eval because no offline provider can synthesise an upstream `401`.

## A4. (Deferred) declarative tool caps

Candidate manifest constraint (`max_items`, `max_calls_per_run`). Deferred — enforce caps in
handlers until the need generalises.

## A5. Deterministic student-key adoption (new — the B3 fix)

**Why:** prod guarantees a same-message `shortlist_add` adopts a just-created lead's key by
**reordering `create_lead` first** within the message (`stream.ts:200-207`). Rya's loop runs
tool calls in emission order with no reordering (`context.py:519`), and the memory-pin approach
from rev 1 is unsound twice over: (a) an unresolved pin overwrites `camsId` with `None` and
calls the handler anyway (`context.py:894`) — there is no rejection primitive; (b)
`_resolve_pin("memory.user.camsId")` reads scope `"user"` *literally*, not the identity-bound
`user:{sub}` that `_Memory._scope` produces, so a pin cannot even read per-counsellor memory.

**Changes considered:**
- **(a) Tool-call priority in `ctx.llm.run`.** An optional per-agent hint (e.g.
  `run(..., priority=["create_lead"])`) that stably sorts the model's emitted calls so a
  lead-creating call runs before student-scoped calls in the same step — a direct port of prod's
  reorder.
- **(b) Hard-reject unresolved pins.** When a required pin resolves to `None`, raise
  `E_NO_STUDENT_KEY` surfaced as a **retryable tool-result error** (never let `None` silently
  overwrite a value). Weaker: relies on the model re-emitting after the error.

**SHIPPED — (c) declarative result→memory adoption + a non-identity memory pin.** Cleaner than
both, and needs no loop-reordering:
- `ToolDecl.adopt` (`schema.py`): a manifest map `{<result field>: <scope>.<key>}`. On a
  **successful** call, `_Tools._apply_adoption` (`context.py`) copies the field into scoped
  memory synchronously *inside the journaled tool step* and traces a `tool.adopt` event.
  `create_lead` declares `adopt: {camsId: student_state.camsId}`.
- The student-scoped pins switch from `event.payload.camsId` to
  **`memory.student_state.camsId`**. This deliberately uses the scope `student_state` (NOT
  `user`), sidestepping the rev-2 bug that `_resolve_pin` reads `memory.user.*` *literally*
  rather than the identity-bound `user:{sub}` — the event handler seeds `student_state.camsId`
  from the session and `create_lead`'s `adopt` overwrites it on success.
- The "empty pin → handler rejects" path is now **sound**, not weak (rev-2 concern (a)): a
  `shortlist_add` before any lead resolves an empty pin, the handler returns
  `{ok: false, reason: "…create the lead in CAMS first."}`, and once `create_lead` has adopted
  the key a re-emitted call resolves it. Order-independent, no forced reorder. Because
  `load_memory` reads fresh from disk, the adopted key is visible to a *later* pinned call in the
  **same turn**.

**Verify:** deterministic gates in `tests/test_retry_repair.py` (the `adopt`→pin primitive,
order-independence) and `tests/test_agent_phase3.py` (`create_lead`→`shortlist` same key; a
`shortlist_add` before `create_lead` rejects then adopts). Live eval: the self-heal run also
shows a `tool.adopt` frame (numeric camsId).

---

# Part 2 — CSA agent changes (Track B)

All in `csa-counsellor/`. Reference template: `../loan-renewal/src/agent.py` (wired
`@agent.tool` leaves, grounding-then-approval write, `ctx.emit_ui`, jobs). Note that template
orchestrates tools **imperatively** — csa-counsellor is the first consumer of `ctx.llm.run`'s
**model-driven** tool-loop path, so smoke-test Anthropic tool-use round-trips through it early.

**Acceptance oracle:** the existing `chatstudyabroad` **vitest suite** is the spec for each
ported handler (name-resolution cascade, Crizac body-shape parity, apply flows). Port its cases
as the acceptance tests for the Python handlers, alongside the new Rya evals.

## B1. Handler rewrite — the governed loop

`src/agent.py`:
- Replace `ctx.llm.respond(system=…, route="compose")` (line 34) with:
  ```python
  result = await ctx.llm.run(
      input={"message": body}, system=COUNSELLOR_SYSTEM,
      route="compose", max_steps=6,        # mirrors prod's 6-turn cap
  )
  ```
  `ctx.llm.run` auto-exposes only `allowed`/`read_only` tools and routes every model tool call
  back through `ctx.tools.call`; `disabled` tools are never exposed. (This is a handler edit —
  the loop already exists at `context.py:471`.)
- Port the real system prompt from `chatstudyabroad/lib/agent/system-prompt.ts` (tool-first,
  currency discipline, discovery-only-via-`course_catalogue`, presenting≠pinning, caps,
  master-id rule) built per-turn from the student profile block + `<student_memory>` block.
- Keep the Haiku memory sidecar (lines 39–45) → `student_facts`.
- Keep + extend the approval block (lines 49–58) per B5.

## B2. Tool handlers (grouped) — all `@agent.tool`

Each Crizac-backed handler: resolve the per-user bearer (A3) → call Crizac (**with prod's
timeouts: 20s request / 60s upload / 60s automation, via a cancel deadline** — a hung upstream
must not stall a durable turn) → return result (id-secrecy scrub applied by A2 after the body).

- **Discovery**
  - `course_catalogue` — port the name-resolution cascade (country → intake → courseType →
    university → eligible courses), degree-family filter, `isAppliable`. Two modes. No card.
  - `present_recommendations` — pure shaper: validate the model's ranked list **shape** (no hard
    8–15 count reject; prod schema is `.min(1).max(30)`), then
    `ctx.emit_ui("recommendations", {items:[…]})`. **No backend call** (so the shape is
    unbypassable here).
  - `programme_detail`, `university_search`, `university_recommendations`, `scholarship_search`
    (dedup per university + `emit_ui("scholarships", …)`), `crizac_filters`,
    `visa_requirements`.
- **CAMS lookups** — `cams_lookup_student` (masterId/CAMS-id/name cascade + the
  `"CAMS id pending"` labelling for id-less leads; A2 scrubs raw output after), `duplicates_check`
  (`emit_ui("duplicates", …)` **only when duplicates found**), `application_activity`.
- **Writes (non-gated)** — `create_lead` / `create_leads_bulk`: use A1 retry (raise typed
  transient errors; single retry owner), register `@agent.repair("create_lead")` with the
  domain repair map, run `passportAlreadyExists` idempotency on every attempt, and on success
  make the new key adoptable via A5.
- **Writes (gated)** — via approval actions (B5): `crizac_update_application`,
  `submit_scholarship_enquiry`, `loan_apply_for_student`, `cams_update_profile` (local
  `student_state` only, not Crizac — matches prod; `camsId` pinned), **`send_email` (real send
  via `sendAgentEmail`, behind the durable gate — not a stub)**.
- **App-local** — `shortlist_add` / `shortlist_remove` (`camsId` pinned; cap 15;
  `emit_ui("student_refresh")` only on a real change), `compare_programmes` (21-field PDF;
  `emit_ui("comparison", …)`), `collect_profile` (`emit_ui("profileForm", …)`),
  `draft_sop` / `draft_lor` (via `route="doc_analysis"`).
- **Prediction** — `offer_prediction`: Plexe `x-api-key` (`ctx.secrets.get("PLEXE_API_KEY")` —
  cannot be a Bearer `url:` tool) or `ctx.models.call("offer-prediction-xgb")`; graceful
  fallback on the intermittent endpoint.
- **Hidden** — `course_search`, `course_recommendations`, `apply_to_programme`: **no agent
  handler**, keep `permission: disabled`. NB "hidden ≠ dead": in prod these stay **REST-callable**
  (`course_search` powers the Shortlist "Suggested" rows). Correct to hide from the agent; if
  the console is ported, those non-agent call sites still need a home (out of scope here).

**SHIPPED (Phase 5).** The tail leaves landed over the local catalogue / curated seeds:
`programme_detail`, `university_search` + `university_recommendations` (country/level/budget/subject
filters, local fallback — prod's Plexe path is live-only), `scholarship_search` (curated seed,
deduped by name → a `scholarships` card via `_card_for`), `crizac_filters` (the four option kinds
off the catalogue), `visa_requirements` (curated `_VISA`, found + not-found). `offer_prediction` is
a leaf that reads `PLEXE_API_KEY` (`os.environ`, not `ctx.secrets` — leaves have no ctx), guards
egress with `check_egress`, and **degrades gracefully** (`{ok:false, reason}`) when unset or the
endpoint is down — it never raises, so the turn always completes. `draft_sop`/`draft_lor` return the
grounded `context` (no fabrication) for the compose model to draft from — prod's context-only path,
since a leaf has no `ctx.llm` to hit the `doc_analysis` route itself. All nine are added to
`EXPOSED_TOOLS`. **Title + passport-OCR sidecars:** the `title`/`passport_ocr` routes stay declared
in the manifest but are intentionally **not** wired into the chat loop — session-rename has no
runtime API, and OCR belongs to the human apply subsystem (out of scope per B7).

## B3. Loop-level safety mapping

- **Forced student-keying** → `pin:` (already declared). Done.
- **effectiveCamsId adoption** → **via A5** (tool-call priority, or hard-reject unresolved pin).
  The rev-1 "empty pin rejects → model retries" claim was wrong (pins overwrite with `None`);
  do **not** rely on it.
- **Master-id scrub** → core A2 (per string-leaf, after the handler body).
- **`student_refresh`** → `ctx.emit_ui("student_refresh", …)` **only after a successful state
  change** (`toolOk` semantics).

## B4. Cards → `ctx.emit_ui`

Prod cards are **client-rendered** from `tool_call_finish` events (`chat-view.tsx buildCard()`);
prod tools return plain data. **Correction (verified in Phase 1):** a `@agent.tool` handler
receives only `input` — **not `ctx`** — and is a leaf (no journaled `ctx` ops,
`sdk/agent.py:57`), so it **cannot** call `ctx.emit_ui`. Cards therefore emit from the **event
handler after the loop**, walking `ctx.llm.run`'s returned `toolCalls` (each `{tool, input,
result}`, `context.py:523`) and mapping card-producing tools → `ctx.emit_ui(component, data)`.
This is *closer* to prod than "emit inside the handler" — prod also builds cards from the
finished tool call, not from inside the tool. Map: `recommendations` ← `present_recommendations`;
`scholarships` ← `scholarship_search`; `duplicates` ← `duplicates_check` (only when found);
`profileForm` ← `collect_profile`; `comparison` ← `compare_programmes`. Frames arrive as SSE
`event: ui` (journaled/replay-safe). The tool result the event handler reads is already
A2-scrubbed (scrub runs at the tool boundary), so a card can never carry a master id.

## B5. Approval-gated writes

Per `loan-renewal:305`: gather via the loop, `ctx.guard.check_grounding(body)` on money-bearing
text (loan amounts), then `ctx.approvals.request(action={"tool": "<gated_tool>", "input": {…}})`.
**Governance caveat (important):** approved actions run through `engine._execute_action:351`, a
**separate path from `ctx.tools.call`** — it injects the scoped credential and applies the
egress guard, but does **not** re-apply server-side pins or the A2 secrecy scrub. So the
handler must build the action's stored `input` from the **already-resolved pinned value**
(so the correct `camsId` is baked in at request time), **or** `_execute_action` must be extended
to re-pin. Verify this for every gated Crizac write. Applies to `send_email` (real),
`crizac_update_application`, `submit_scholarship_enquiry`, `loan_apply_for_student`,
`cams_update_profile`.

**SHIPPED (Phase 4).** Chose the **bake-the-resolved-pin** route (not extending
`_execute_action`): the event handler reads `active_cams = ctx.memory.get("camsId",
scope="student_state")` and `_gated_action(payload, reply, active_cams)` fixes that camsId into
the action `input`, **stripping any `camsId` the counsellor payload sub-object carries** — so a
redirect attempt is ignored and the approved write provably lands on the pinned/adopted student
(`gated_action_uses_pinned_camsId`, unit-tested end-to-end through `run_event → approve`). At
most one write pauses a turn (prod's one-confirm-at-a-time). The grounding gate runs on the
**composed reply** before a loan (a model-invented `£` figure never reaches a loan write; hard
stop, mirroring `compose_report`). `send_email` delivers through the `providers.channels` email
seam (real Resend when `RESEND_API_KEY` is set, mock outbox otherwise) — a real send behind the
durable gate, not a no-op. The gated tools stay out of `EXPOSED_TOOLS` and a direct
`ctx.tools.call` is refused `E_TOOL_PERMISSION_DENIED`, so they run through the approval path
alone. **A2 scrub NOT re-applied on the action path** — acceptable here because the gated
writes' `input`/result carry only the numeric pinned camsId and counsellor-supplied fields, no
raw Crizac record; revisit if a gated write ever echoes a master-id-bearing upstream body.

## B6. Manifest / guard / eval edits

- `rya.agent.yaml`: add `provider: crizac` + `scopes:` to Crizac-backed tools; add `retry:`
  blocks to `create_lead`/`create_leads_bulk`; wire adoption via A5 (not memory pins).
  `present_recommendations` stays backend-less.
- `rya.guard.yaml`: replace the `crizac-api.example` placeholder (line 16) with the real Crizac
  host; add `api.plexe.ai`; add the `secrecy:` block with prod's exact regex (A2).
- `rya.evals.yaml`: extend from the current 2 evals with the per-phase + negative evals below.

## B7. Omissions now in scope

- **Observability is not 1:1.** Prod runs Langfuse per-turn traces with a `generation` per LLM
  call (token usage) and a `span` per tool call (`lib/observability/langfuse.ts`); Rya
  journaling ≠ per-call cost/latency. Reconcile against PRD §15 (map to Rya observability /
  Langfuse export).
- **Two extra LLM sidecars** beyond the Haiku memory extractor: session-title generation
  (`title.ts`, `title` route) and Sonnet document analysis / SOP-LOR / passport-OCR
  (`documents/analyze.ts`, `doc_analysis`/`passport_ocr` routes). B2 wires `draft_*` via
  `doc_analysis`; scope title + OCR paths explicitly.
- **Crizac client timeouts** (20s / 60s / 60s) — port them (see B2).
- **Human-driven apply subsystem** (`lib/crizac/instant-apply/*`, `regular-apply.ts`, automation
  form builder, passport OCR, document attach; PRD §10) is **out of scope** for the agent (apply
  stays `disabled`), but stated here so the omission is intentional, not accidental.

---

# Phased sequence (interleaved A/B)

| Phase | Track A (core) | Track B (agent) | New / gate evals |
| --- | --- | --- | --- |
| **1 — loop + discovery spine (+ A2)** ✅ **DONE** | **A2 id-secrecy guard** (pulled forward — Phase 1 exposes live Crizac results, must not leak a masterId); A3 as **static `rya connect` seed** | B1 loop + system prompt; `course_catalogue`, `present_recommendations`; manifest+guard host + timeouts | `discovery_uses_catalogue_then_cards`, `discovery_never_uses_hidden`; **GATE: passports/numeric-CAMS/phones preserved (`utils.test.ts:249-257`), `master_id_never_surfaced`** |
| **2 — lookups + workspace** ✅ **DONE** | — | CAMS lookups (`cams_lookup_student`, `duplicates_check`, `application_activity`); workspace tools (`shortlist_add`/`_remove`, `compare_programmes`, `collect_profile`) + conditional cards (`duplicates`, `comparison`, `profileForm`, `student_refresh`); camsId pins | `master_id_never_surfaced` (scrub token in trace) + `duplicates_renders_card` (live evals); **deterministic gate → `tests/test_agent_phase2.py`**: master-id-vs-CAMS-id separation + scrub-preserves-CAMS-id, shortlist cap/dedup/invented-id refusal, compare/profile/cards. `shortlist_forces_camsId` = the runtime pin (core-tested) |
| **3 — retry/repair + adoption + create_lead** ✅ **DONE** | **A1** declarative `retry:` (`RetryDecl`, classes `timeout`/`5xx`) + `@agent.repair` self-heal (`RyaRecoverableToolError`, `tool.repair`/`tool.retry` trace steps, unified across agent/http/mock backends in `_Tools.call`); **A5** declarative `adopt:` result→memory primitive + `memory.student_state.camsId` pins (option (c) above). **A3 `upsert_connection`/login-mint/reconnect deferred to Phase 5** (where its eval lives — Phase 3 gates are A1+A5 only; `create_lead` stays a local leaf) | `create_lead` + `create_leads_bulk` + `@agent.repair("create_lead")` (closest-destination/course-type/home-state spelling) + `passportAlreadyExists` idempotency; `_surface_cams` fix so lookups/compare echo the **numeric** CAMS id, never the masterId | **deterministic gates → `tests/test_retry_repair.py`** (A1 retry/exhaust/class-gating, repair self-heal, A5 adopt+pin, order-independence, YAML `on:` footgun) **+ `tests/test_agent_phase3.py`** (**GATE: timeout-then-success does NOT double-create**, **GATE: `shortlist_add` BEFORE `create_lead` adopts key**, `create_lead_then_shortlist_same_key`, self-heal end-to-end); live evals `create_lead_routes_and_completes` + `create_lead_selfheals_recoverable` (verified `tool.repair`+`tool.adopt` fire live) |
| **4 — gated writes real** ✅ **DONE** | — | **B5** all five gated writes wired as leaves that execute ONLY via an approved `ctx.approvals.request` action (`cams_update_profile`/`crizac_update_application`/`submit_scholarship_enquiry`/`loan_apply_for_student` mutate local CAMS; `send_email` is a **real** send through the email channel seam — Resend when keyed, mock outbox offline — not a stub). Event-handler intent dispatch (`_gated_action`) detects the write from the turn payload, **bakes the resolved `memory.student_state.camsId` into the action input** (since `engine._execute_action` does NOT re-pin), and runs the **grounding gate** on the composed reply before a loan write (hard stop on an ungrounded figure) | **deterministic gates → `tests/test_agent_phase4.py`** (**GATE: gated tool never in loop** — absent from `EXPOSED_TOOLS` + direct `ctx.tools.call` refused `E_TOOL_PERMISSION_DENIED`; **GATE: loan/update/email SUSPEND for a human**; **GATE: `gated_action_uses_pinned_camsId`** — payload redirect ignored, approved action + write land on the pinned student; **GATE: ungrounded loan figure blocked**; five gated-leaf write units + dispatcher units); live evals `loan_requires_human`, `update_application_requires_human`, `gated_tool_never_in_loop` + extended `outbound_email_requires_human` |
| **5 — prediction + sidecars + tail** ✅ **DONE** | **A3 in full**: `upsert_connection` (overwrite-in-place on `(provider, owner)`, both stores) + `ctx.connections.upsert`; `E_CONNECTION_EXPIRED` on a credentialed `401` → a distinct **`needs_reconnect`** run outcome (engine + turns/app terminal-status + status allow-list), no auto-refresh; **fail-closed** on missing identity (`require_user` on `ToolDecl` → `E_NO_IDENTITY`) and on plaintext-at-rest (`rya deploy --check` blocks `E_PLAINTEXT_SECRET_AT_REST` for a `require_user` provider). Credential enforcement **scoped to url/egress backends** — a `provider:` on a local `@agent.tool` leaf is governance metadata, so the Crizac tools carry a live `url:` yet the suite stays offline-deterministic (handler wins). Login-mint via `ctx.secrets` + `ctx.connections.upsert` in the event handler | `offer_prediction` (Plexe via `PLEXE_API_KEY`, graceful `{ok:false}` when unset/down), `draft_sop`/`draft_lor` (grounded context → compose model drafts), the discovery tail (`programme_detail`, `university_search`, `university_recommendations`, `scholarship_search` → `scholarships` card, `crizac_filters`, `visa_requirements`); title/OCR routes declared but intentionally unwired | **deterministic gates → `tests/test_connection_a3.py`** (upsert no-stale-duplicate + create→upsert migration, **GATE: `expired_token_surfaces_reconnect`** = `E_CONNECTION_EXPIRED` → `needs_reconnect` (+ credentialless-401 stays a plain error), **GATE: `require_user` fails closed without identity** / allows with, handler-backed provider tool runs with no connection, **GATE: plaintext crizac connection blocks deploy** / sealed is ready) **+ `tests/test_agent_phase5.py`** (nine tail-leaf units incl. **GATE: `offer_prediction` graceful when unconfigured**, scholarships-card, `_extract_crizac_token` order, `crizac_tools_declare_live_connection`, login-mint upserts the per-counsellor bearer / skips when unconfigured); live eval `offer_prediction_graceful_when_down` (completes, no `run.failed`) |

**Non-negotiable gates:** the regex-negative eval blocks Phase 2 sign-off; the duplicate-on-retry
and risky-order evals block Phase 3 sign-off; the gated-tool-never-in-loop and
pinned-camsId-on-the-action gates block Phase 4 sign-off; the reconnect-outcome, fail-closed-identity
and plaintext-at-rest gates block Phase 5 sign-off.

# Verification

- Per phase: `rya dev` (validate manifest + handlers), `rya events send --type
  message.received --payload …` per path, `rya approvals approve <id>` for gated writes, assert
  `ctx.emit_ui` frames on `POST /agents/csa-counsellor/events/stream`.
- Ported handlers pass their **vitest-derived** acceptance cases (§B2).
- `rya.evals.yaml` green per phase, gates enforced as above.
- `rya deploy --check` (blocks on ungated actions, missing evals, secrets in repo, **plaintext
  crizac connection**) before any real deploy.

# Critical files

- **Core Rya:** `src/rya/manifest/schema.py`, `src/rya/sdk/context.py`, `src/rya/guard.py`,
  `src/rya/store.py` + `store_postgres.py` (connections), `src/rya/runtime/engine.py`
  (`_execute_action`), `src/rya/seal.py`.
- **CSA agent:** `csa-counsellor/src/agent.py`, `rya.agent.yaml`, `rya.guard.yaml`,
  `rya.evals.yaml`; template `../loan-renewal/src/agent.py`.
- **Source of truth for business logic to port:** `chatstudyabroad/lib/agent/*`,
  `chatstudyabroad/lib/tools/impl/*`, `chatstudyabroad/lib/crizac/*`,
  `chatstudyabroad/lib/auth/*`, `chatstudyabroad/lib/utils.ts` (+ `utils.test.ts`), the vitest
  suite.
