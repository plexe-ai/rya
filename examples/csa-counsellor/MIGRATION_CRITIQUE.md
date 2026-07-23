# Critique of `MIGRATION.md` (ChatStudyAbroad → Rya / csa-counsellor)

> Review method: every load-bearing claim in `MIGRATION.md` was verified against the
> actual source in both trees (`chatstudyabroad/` prod and `rya/` runtime), across five
> parallel deep-research passes: Rya runtime internals, CSA production behaviour, the
> csa-counsellor shell's current state, Rya SDK/API feasibility, and an adversarial
> stress-test of the key technical decisions. File:line citations below are from that
> verification, not from the plan's own references.

---

## 1. Verdict

**The plan's architecture is sound; its facts are not all correct.** The "split-and-port"
thesis (cross-cutting concerns → core Rya primitives; business logic → `@agent.tool`
handlers) is the right shape and matches Rya's own core/example separation. Most of the
primitives the plan leans on **already exist** in the runtime, so the wiring work (Track B)
is smaller than the document implies.

But the plan is built on **four incorrect assumptions about existing behaviour** — two about
production, two about the Rya runtime — and each one turns into a real defect if implemented
as written. These are fixable, but they must be fixed *before* the phases they live in are
approved:

| # | Issue | Where | Severity |
|---|-------|-------|----------|
| 1 | The proposed id-secrecy regex is a looser re-invention that regresses prod's tested behaviour | A2 | **HIGH** |
| 2 | B3's "empty pin rejects → model retries" is not how pins behave; it nulls the arg | B3 | **HIGH** |
| 3 | A3 assumes a connection-*upsert* primitive that `store.py` doesn't have | A3 | **HIGH** |
| 4 | `create_lead` retry idempotency depends on `passportAlreadyExists`, which the plan omits | A1/B2 | **MED-HIGH** |
| 5 | `send_email` is described as a stub; prod actually sends real email | B2/B5 | **MED (factual)** |
| 6 | Evals skew to happy paths; the negative/idempotency cases are missing | B6 | **HIGH** |

---

## 2. What the plan gets right

Credit where due — these are verified accurate:

- **Current-state description of the shell is exact.** 28 tools with the stated permission
  tiers; exactly 4 student-scoped pins (`cams_update_profile`, `shortlist_add`,
  `shortlist_remove`, `compare_programmes`, all `{camsId: event.payload.camsId}`); exactly
  3 disabled tools (`course_search`, `course_recommendations`, `apply_to_programme`); no
  `provider`/`scopes`/`retry` on any tool; `src/agent.py` runs a single-turn
  `ctx.llm.respond(route="compose")` with zero tools wired; the Haiku memory sidecar and
  the email-approval block are where the plan says they are; the guard has the
  `crizac-api.example` placeholder, money-only grounding, no secrecy block, no Plexe host;
  exactly 2 evals. No factual errors in the plan's description of *today*.
- **The reference-template patterns exist** in `examples/loan-renewal/src/agent.py`: wired
  `@agent.tool` leaves, `ctx.guard.check_grounding` → `ctx.approvals.request` (line 305 as
  cited), `ctx.emit_ui`, and jobs.
- **The core file:line references are largely accurate** (minor drift on `engine.py:211`,
  which is the `else:` opener, not the retry body at 214-217).
- **The `secrecy:` guard block is correctly targeted** at the genuine gap: the tool loop
  appends the *raw* tool result to model context (`context.py:524`) with no per-result
  interception hook. That hook really is missing and A2 puts it in the right place.
- **Alignment with `PRD.md` §15** is real — same tool tiers, same gated-write set, same
  human-only apply, same grounding intent.

---

## 3. The plan overstates the core work (framing error)

Two of the plan's stated gaps **already exist in the runtime**. This is the opposite of a
risk — it means less work — but the framing should be corrected so effort isn't
mis-budgeted.

### 3.1 The governed tool loop already exists

The plan's B1 premise — "`src/agent.py` calls single-turn `ctx.llm.respond` … never the
tool loop" — is true only of the *example*, not of Rya. `ctx.llm.run(*, input, system,
tools, max_steps=6, route)` is a fully implemented, journaled, replay-safe governed loop at
`context.py:471-525`. It already:

- auto-exposes only `allowed`/`read_only` tools (`context.py:487-490`);
- routes every model tool call back through `ctx.tools.call` (`context.py:522`), so
  permissions, pins, scoped creds, and the guard all apply;
- is durable across the approval pause (each turn is a `_step("llm.chat")`).

**So B1 is a handler edit (`respond` → `run`), not a core build.** The signature the plan
writes matches the real one exactly. Note, though, that neither example demonstrates the
*model-driven* loop — `loan-renewal` orchestrates tools **imperatively** via `ctx.tools.call`.
csa-counsellor would be the first consumer of `ctx.llm.run`'s tool-loop path, so it's worth
an early smoke test that Anthropic tool-use round-trips correctly through it.

### 3.2 Per-user connections already exist (but not the write path)

A3 is framed as replacing "a static out-of-band `rya connect`." In fact the runtime already
has a mature **per-user** connection model: `_authorize_connection` (`context.py:301-337`)
resolves per-`(provider, owner)` where `owner = identity.sub`, enforces the
connection∩user scope intersection, and seals the secret. `rya connect` already stores
per-user tokens (`cli/main.py:441`). What is genuinely missing is narrower than "mirror the
whole mechanism": a **runtime write path** and a **reconnect signal** (see §5.3).

**Recommendation:** Re-frame Part 1. The net-new core work is really: A1 (retry + repair,
medium), A2 (secrecy scrub + the per-result hook, medium), and A3's two specific pieces
(connection upsert + `E_CONNECTION_EXPIRED`, medium). The tool loop, approvals durability,
`emit_ui`, memory scopes, and pin-from-memory are all already there.

---

## 4. Factual errors about production (fix the plan text)

These are places where the plan mis-states what prod does. They matter because prod is the
**spec** — porting a wrong description ports a wrong behaviour.

### 4.1 `send_email` is NOT a stub — this one is a downgrade risk

The plan says (B2/B5) "`send_email` (real logged no-op — prod is a stub)" and "make
`send_email`'s action real." **Prod already sends real email**: `lib/tools/impl/email.ts` →
`client.sendAgentEmail(...)` → `POST /v1/api/userMailMaster/incoming-email/sendAgentMail`
(`lib/crizac/client.ts:2520`), with a preview/confirm gate, firing when `confirm:true`.
Implementing the plan literally would **regress a working feature to a no-op**. Decide
explicitly whether the Rya port sends real mail (behind the durable approval gate) or
deliberately stubs it for now — but don't do it on the false premise that prod is a stub.

### 4.2 The id-secrecy regex is wrong (see also §5.1)

Plan A2 proposes `\b[A-Za-z]+[0-9]{6,}\b`. Prod's real scrub is
`/\b[A-Za-z]{3,8}\d{8,}\b/g` → `"(id hidden)"` (`lib/utils.ts:135`), and `utils.test.ts:249-257`
**asserts** that passports (`Z1234567`, `MEGHA1234`), phone numbers, and numeric CAMS ids
are *not* touched. Also: prod applies the scrub in **three** places (before-model at
`stream.ts:249`, at render `chat-view.tsx:965`, at persist `chat-view.tsx:603`), not two.

### 4.3 `create_lead` self-heal is a prompt convention, not code

Plan A1 presents the repair callback as mirroring prod's self-heal. Prod's *retry-once on
transient 5xx* is real code (`leads.ts`, `isTransient` excludes 4xx/auth). But the
domain self-heal (closest valid destination/course type, home-state spelling) is **a system-
prompt instruction to the model** — there is no repair map in the codebase. A1 is therefore
a *redesign* (moving LLM-driven self-heal into deterministic code), which may be a genuine
improvement, but the plan should own that rather than call it a mirror. Prod also has a
**passport duplicate guard** (`passportAlreadyExists`) the plan never mentions (§5.4).

### 4.4 Caps don't match prod

- shortlist 15: enforced in tool code (`shortlist.ts:137`). ✓
- apps 5: **not enforced in the agent loop** — only in the UI and prompt. Porting
  "apps 5 in handlers" *adds* enforcement prod's agent never had (fine, but call it new).
- recs "8–15": **soft only** — the schema is `min(1).max(30)` and the system prompt
  actually says "about **5**." There is no hard 8–15 cap in prod. The plan's "8–15 cap in
  handler" contradicts the source.

### 4.5 Cards are client-rendered, not tool-emitted

Prod tools return plain data; `chat-view.tsx buildCard()` constructs cards from
`tool_call_finish` events. The plan's B4 (`ctx.emit_ui` from inside the handler) is a
correct *re-architecture*, but the sentence describing prod as tools that "emit cards" is
inverted. Minor, but the `duplicates` card is only built when `hasDuplicates`, and
`student_refresh` only fires after a real state change (`toolOk` guard, `stream.ts:236`) —
nuances B3/B4 currently flatten into unconditional emits.

### 4.6 "No silent refresh" is only true for the counsellor path

The per-counsellor (pre-authed) client throws `CrizacAuthError` on 401 (`client.ts:353,590`),
so the plan's claim holds for the chat agent. But the **service-account** mode
(`!preAuthed`) *does* transparently re-login (`client.ts:561`). And the clean 401→reconnect
banner lives on the REST data routes (`app/page.tsx:480`), not the chat SSE — in the chat
loop a 401 becomes a scrubbed error string the model relays. There is no typed reconnect
signal in the chat path today; A3b is building something prod's chat path lacks, not
mirroring it.

---

## 5. Design risks (from the adversarial stress-test)

### 5.1 A2 id-secrecy regex — HIGH

- **False positives that regress prod.** `[A-Za-z]+` (≥1) + `[0-9]{6,}` (≥6) scrubs values
  prod deliberately preserves: passport `Z1234567`, order/tracking ids, and — worst — a
  **CAMS id written without a space** (`CAMS1472802`) would be replaced with `(id hidden)`,
  hiding the exact value the counsellor is meant to see. Prod avoids this by requiring
  3–8 letters *and* 8+ digits.
- **No false-negative on the real masterId format** (`aiMs1760246966`, `IjmQ1782803306`) —
  the plan's pattern does match those; looseness is the problem, not tightness.
- **The "CAMS id pending" case can't be covered by a blanket scrub.** For a brand-new lead
  with no numeric id yet, prod substitutes the label `"CAMS id pending"` (`utils.ts:117`).
  A blanket scrub leaves the model with `(id hidden)` and no way to reference the student.
  This logic must live in the lookup handler; A2 alone can't do it.
- **Type mismatch.** A2 is specified as `secrecy_scrub(text) -> (scrubbed, hits)`, but tool
  results in Rya are **dicts** (`context.py:524`). Scrub must walk string leaves of the
  parsed dict, not regex a serialized blob, and the replacement token must be quote-safe.
  This is unaddressed and is exactly where JSON corruption creeps in.

**Fix:** adopt prod's exact pattern `\b[A-Za-z]{3,8}\d{8,}\b` verbatim; port
`utils.test.ts:249-257` as **negative** evals; scrub per-string-leaf; define the token; port
"CAMS id pending" into the handler. Confirm the scrub runs strictly *after* the handler body
so the `create_lead` adoption (B3) still sees the raw id.

### 5.2 B3 effectiveCamsId adoption via memory pin — HIGH (reasoning unsound)

The plan claims a `shortlist_add` before `create_lead` "resolves an empty pin and rejects,
so the model retries." **That is not how pins work.** `_resolve_pin` for
`memory.student_state.camsId` returns `None` when unset (`context.py:378-383`), and
`tools.call` merges the pin over the input (`context.py:895`) — so an empty pin **overwrites
`camsId` with `None` and calls the handler anyway.** There is no rejection primitive; a
"reject" only happens if the handler validates `None`, and nothing guarantees that error is
surfaced in a form the model retries. Three unproven assumptions are stacked.

Meanwhile prod does **not** rely on model retry — it *reorders* `create_lead` to run first
within a message (`stream.ts:200-207`, with an explicit comment) so a same-message
`shortlist_add` adopts the new key. Rya's loop executes in emission order with **no
reordering** (`context.py:519`). The plan discards a proven deterministic safeguard for an
unproven behaviour.

**Fix:** either (a) port the reorder-`create_lead`-first behaviour, or (b) make the empty-pin
case a hard typed rejection (`E_NO_STUDENT_KEY`) surfaced as a retryable tool error — and
never let a `None` pin silently overwrite a model-supplied value. Add an eval for the
*shortlist-before-create_lead* order specifically (the listed
`create_lead_then_shortlist_same_key` only tests the easy order).

### 5.3 A3 per-counsellor auth — HIGH (missing primitive + storage)

- **The upsert primitive does not exist.** `store.py` has `create_connection`,
  `get_connection`, `revoke_connection`, `reseal_connections` — no update/upsert.
  `create_connection` always writes a **new** doc, and `get_connection` returns the *first*
  user-owned active match in filesystem-glob order. So every login mints another connection
  for the same `(crizac, sub)`, and the runtime may later inject a **stale token**. The
  plan's "programmatic connection upsert" is genuinely new core work; it must overwrite in
  place keyed on `(provider, owner)`.
- **Plaintext-at-rest hole.** `seal()` silently falls back to plaintext when `cryptography`
  is unavailable or no key is set (`seal.py:85-90`), and provisioning only *warns*. Prod
  always encrypts the bearer (A256GCM cookie). For a Crizac bearer this must **fail closed**
  or be a deploy-check block.
- **"Fails closed" isn't automatic.** `_authorize_connection` keys on `identity.sub`; if the
  webhook channel doesn't forward the verified `X-Rya-User-Token`, `owner=None` and
  `get_connection` falls through to a **workspace-shared** connection
  (`store.py:418-420`) rather than erroring — a silent attribution leak, not a hard failure.
  Make a missing `sub` fail closed for Crizac tools.
- **Mid-turn expiry has no rollback.** `ctx.llm.run` has no try/except around `tools.call`
  (`context.py:519-524`); a raised `E_CONNECTION_EXPIRED` aborts the whole turn, and any
  side effect already committed (e.g. a `create_lead` in step 2) is not rolled back. Prod
  has the same exposure, but note it.
- **Concurrency.** Two concurrent turns for one counsellor + a re-mint = racing
  `create_connection` writes + non-deterministic `get_connection` — compounds the no-upsert
  bug.

**Fix:** build `upsert_connection(provider, owner)` (overwrite-in-place) before wiring A3;
make plaintext connection storage a deploy-check **block** for `provider: crizac`; fail
closed on missing `sub`.

### 5.4 A1 retry idempotency — MED-HIGH

- Prod guards duplicates *before* creation via `passportAlreadyExists` (`leads.ts:360-380`),
  which the plan never mentions. Declarative retry on a WRITE without porting this guard
  **will create duplicate leads** on the POST-succeeds-then-response-times-out path.
- **The retry may never fire.** Prod handlers *catch* transient errors and return
  `{ok:false, reason}` — they don't throw. A ported handler that returns a structured
  failure bypasses a retry that "classifies the raised error." Conversely, keeping prod's
  inner retry *and* the declarative wrapper gives 2×2 = 4 attempts.
- **Ordering/accounting undefined** — does a repaired retry consume a `max_attempts` slot?
  Worst case under `max_attempts:2` + one repair is 3 upstream writes.

**Fix:** port `passportAlreadyExists` as the idempotency key, run it on every attempt; pick
one retry owner (handler *or* runtime); restrict the classifier to exclude 4xx/auth exactly
as `isTransient` does; add an eval that timeout-then-success does not double-create.

### 5.5 Caps coupled to pin correctness — MED

shortlist-15 / apps-5 are *stateful* caps (count existing + new under the pinned `camsId`).
If the pin resolves empty (§5.2), the count is read against the wrong/empty key and the cap
is meaningless. Handler enforcement is otherwise bypass-proof on the write path (good), and
`present_recommendations` has no backend so the 8–15 shape is unbypassable there. Add an
eval and ensure the count read uses the *resolved* pin.

### 5.6 Approved-action path skips pins and secrecy — MED (governance)

The plan says gated tools "execute with full governance on approval." Approved actions run
through `Engine._execute_action` (`engine.py:351-391`), a **separate** path from
`ctx.tools.call`: it injects the scoped credential and applies the egress guard, but does
**not** re-apply server-side pins or the A2 secrecy scrub. For gated Crizac writes
(`crizac_update_application`, `cams_update_profile`, `submit_scholarship_enquiry`,
`loan_apply_for_student`) that means the pin that protects student-scoping in the loop is
*not* re-enforced at approval-execution time. Confirm the action's stored input already
carries the pinned value, or extend `_execute_action` to re-pin.

### 5.7 Memory scope `student_state` is agent-global — MED (footgun)

The plan writes `camsId` to scope `"student_state"`. Scopes are arbitrary strings; `"user"`
is identity-bound (`user:{sub}`) but any other string (including `"student_state"`) is
**agent-global** (`context.py:560-564`) — shared across turns and *across counsellors*
unless the key encodes the student. For a multi-counsellor deployment this is a cross-talk
risk. Prefer scope `"user"` (or a per-session scope) for the adopted `camsId`.

---

## 6. Omissions — prod behaviour the plan doesn't scope

- **Observability is not a 1:1.** Prod runs Langfuse per-turn traces with a `generation` per
  LLM call (token usage) and a `span` per tool call (`lib/observability/langfuse.ts`). Rya
  journaling is not the same as per-call cost/latency tracing; the plan's B-track doesn't
  address it. PRD §15 maps this to Rya observability — reconcile.
- **Two extra LLM sidecars** beyond the Haiku memory extractor: session-title generation
  (`lib/agent/title.ts`) and document analysis / SOP-LOR / passport-OCR on **Sonnet**
  (`lib/documents/analyze.ts`), which back `draft_sop`/`draft_lor` and instant-apply. The
  manifest declares `title`/`doc_analysis`/`passport_ocr` routes, but B2 only wires
  `draft_*` via `doc_analysis` and doesn't scope the OCR/title paths.
- **Crizac client timeouts** (20s request / 60s upload / 60s automation, with AbortSignal)
  aren't mentioned; without them a hung upstream stalls a durable turn.
- **The existing vitest suite is the real spec** for the ports (name-resolution cascade,
  Crizac body-shape parity, regular/instant-apply flows). The plan tracks only new Rya evals
  and never references these as porting oracles — they should be the acceptance tests for the
  ported handlers.
- **The instant/normal apply subsystem** (`lib/crizac/instant-apply/*`, `regular-apply.ts`,
  automation form builder, passport OCR, document attach) is where real application filing
  lives. The plan treats apply as merely "disabled from chat," which is correct for the
  agent but leaves the human-driven subsystem entirely out of scope — fine if intentional,
  but it should be stated, since PRD §10 treats it as core product.
- **"Hidden ≠ disabled."** The three `AGENT_HIDDEN_TOOLS` remain *callable* via REST routes
  (`course_search` powers the Shortlist "Suggested" rows). The plan's "no handler /
  permission disabled" framing drops the fact that these are live-but-agent-invisible; if
  the console is ported, those non-agent call sites still need a home.
- **No rate limiting** exists in prod — nothing to port, but worth stating since it was a
  candidate.

---

## 7. Phasing critique

- **Phase 1 is lighter than feared** — the tool loop already exists, so B1 is not secretly
  blocked on unbuilt Track-A work. Good.
- **A3 in Phase 1 is not "minimal."** Real login-mint needs the connection-upsert primitive
  that doesn't exist (§5.3). Either add "connection upsert primitive" as an explicit Phase 1
  Track-A line, or make Phase 1 A3 an honestly-static `rya connect` seed and move real
  minting later. As written the phase table hides a core dependency.
- **A2 lands one phase too late.** Phase 1 already exposes `course_catalogue` /
  `present_recommendations` through the live loop, returning real Crizac results to the
  model with **no scrub**. If any discovery record carries a masterId, Phase 1 leaks it
  before A2 arrives in Phase 2. Pull A2 to accompany the first live Crizac tool.
- A1 in Phase 3 with `create_lead` is correctly ordered.

---

## 8. Eval coverage — HIGH gap

The ~13 planned evals skew to happy paths and approval gating. Load-bearing risks that are
currently untested:

| Missing eval | Guards | Priority |
|---|---|---|
| Passport / CAMS-id / phone **preserved** (not scrubbed) — port `utils.test.ts:249-257` | §5.1 regex over-redaction | **HIGH** |
| `create_lead` timeout-then-success does **not** double-create | §5.4 idempotency | **HIGH** |
| `shortlist_add` **before** `create_lead` (the risky order) | §5.2 pin adoption | **HIGH** |
| 16th shortlist / 6th app / >15 recs rejected at the boundary | §5.5 caps | MED |
| Token expiry *after* a committed side effect in a multi-step turn; two concurrent turns | §5.3 auth | MED |
| Model reports a CAMS id without calling the lookup (fabrication beyond money) | grounding gap | MED |

The regex-negative and duplicate-on-retry evals should be non-negotiable gates before
Phase 2 and Phase 3 respectively.

---

## 9. Prioritized action list

**Before Phase 1:**
1. Re-frame Part 1 to reflect that the tool loop, approvals, `emit_ui`, memory scopes, and
   pin-from-memory already exist (§3). Correct the `send_email`, caps, self-heal, and card
   factual errors (§4).
2. Pull A2 into Phase 1 and adopt prod's exact regex + negative evals (§5.1, §8).
3. Resolve the B3 pin-adoption design: reorder or hard-reject, not "model retries" (§5.2).
4. Choose memory scope `user`, not agent-global `student_state`, for the adopted key (§5.7).

**Before Phase 3 (writes):**
5. Add `upsert_connection` + fail-closed plaintext/`sub` handling for A3 (§5.3).
6. Port `passportAlreadyExists`; define a single retry owner and classifier (§5.4).
7. Re-pin (or verify pinned input) in the approved-action path (§5.6).

**Cross-cutting:**
8. Adopt the existing vitest suite as the acceptance oracle for ported handlers (§6).
9. Scope observability (Langfuse → Rya traces), the title/OCR sidecars, and client timeouts
   (§6).

---

*Nothing in either repository was modified during this review.*
