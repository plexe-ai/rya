# ChatStudyAbroad — Crizac Agent Console

**Product Requirements Document (agent product spec)**

> Status: reverse-engineered from the production Next.js codebase
> (`chatstudyabroad/`) as the source-of-truth spec for rebuilding the agent on
> **Rya**. This document describes *what the agent is and must do*; the "Building
> on Rya" section at the end maps every capability to a Rya primitive and lays
> out the port. It lives alongside the `csa-counsellor` Rya project in this
> folder (`rya.agent.yaml` + `src/agent.py`) — the spec that project is being
> built toward. csa-counsellor currently declares the full 28-tool registry with
> governance tiers and runs a conversation shell; wiring each tool to its live
> CSA endpoint (per-tool `url:`) is Phase 1 of the port described in §15.

---

## 1. Summary

ChatStudyAbroad is an **AI co-pilot embedded in a counsellor console** for study-abroad
recruitment. Its users are **recruitment counsellors at Crizac sub-agencies** who advise
students on studying abroad and file their university applications through Crizac's CAMS
CRM. The counsellor opens a student loaded from CAMS and chats with the co-pilot to:
recommend eligible universities and programmes, find scholarships, flag duplicate handling
when other sub-agents are working the same student, draft SOP/LOR, predict offer
probability, and persist new facts back to the CRM — all grounded strictly in real Crizac
catalogue data, never invented.

The agent is **profile-aware** (every reply is conditioned on the loaded student and a
long-term soft-memory of that student), **tool-first** (it must call a tool rather than
guess any fact, price, or eligibility rule), and **governed** (some tools are hidden from
the model entirely, some mutate the real CRM and require confirmation, and applications can
*never* be filed from chat — only by a human clicking Apply).

**One-liner:** *An AI co-pilot inside the Crizac CRM that turns a counsellor's chat into
grounded, eligibility-checked shortlists, scholarships, and CRM writes — with the risky
actions kept behind human hands.*

---

## 2. Background & domain

| Term | Meaning |
| --- | --- |
| **Crizac** | The B2B education-recruitment platform. Sub-agencies recruit students and file applications to universities through Crizac. Exposes a REST API at `api-prod.crizac.com`. |
| **CAMS** | The Crizac CRM ("CAMS console") where a counsellor sees students and applications. This app reads/writes CAMS via the Crizac API. |
| **Sub-agent / counsellor** | The end user — an advisor at a Crizac sub-agency. Authenticates with their *own* Crizac credentials; all CRM writes happen as that counsellor. |
| **Student (masterId)** | A person. Keyed by Crizac `masterId` (alphanumeric, e.g. `aiMs1760246966`). One student can have several applications. |
| **CAMS ID** | The **numeric** per-application id the counsellor sees in the console (e.g. `985129`). *Distinct from* the masterId. The distinction is safety-critical (see §7). |
| **Automation university** | A university whose application form is a Crizac dynamic "automation form builder"; filing drives that form. Others are "manual/standard". |
| **Plexe** | External ML inference (`api.plexe.ai`): NL catalogue search/recs (Claude + Weaviate) and an XGBoost offer-prediction model. |

### The problem
Counsellors juggle many students across intakes and destinations, inside a CRM whose
catalogue, eligibility rules, and per-university application forms are large and idiosyncratic.
Mistakes are costly: recommending an ineligible or wrongly-priced programme, filing to the
wrong per-home-country form, exposing an internal id as the student's CAMS id, or duplicating
a lead another sub-agent already owns. The co-pilot compresses this into a conversation while
**refusing to fabricate** and **keeping the irreversible actions gated**.

---

## 3. Users & personas

- **Primary — Sub-agency counsellor.** Logs in with Crizac email/password. Works a pipeline
  of students day-to-day (30-day session). Wants: fast, correct shortlists; scholarships;
  clean lead creation; to file applications without memorising each university's form.
- **Secondary — Agency admin / platform operator.** Uses the **admin tool console** to see
  every tool, what backend powers it, which ones mutate data, and to run a tool manually for
  debugging. (Governance/observability surface.)
- **Indirect — Student.** Never uses the app. Is the subject of the profile; receives emails
  the counsellor sends and applications the counsellor files.

**Commercial model** (`app/pricing`): **Professional Plan — $499/month per user**;
**Sponsored Account** (free for agencies sponsored by an aggregator). Feature set spans AI
guidance, shortlisting, document analysis, visa/scholarship support, test prep, and
pre-departure checklists.

---

## 4. Goals & non-goals

### Goals
1. **Grounded advice.** Every number (tuition, fee, deadline, scholarship amount) comes
   verbatim from a Crizac tool output, in GBP, never converted/rounded/invented.
2. **Eligibility-correct discovery.** Programme discovery goes *only* through Crizac's
   contracted, eligibility-checked catalogue — no degree-type drift, no hallucinated courses.
3. **Profile-aware continuity.** Each reply uses the loaded student's structured profile plus
   a long-term soft memory of what the counsellor has revealed across sessions.
4. **Safe CRM mutation.** Writes (lead creation, visa history, scholarship/loan enquiries,
   email) are explicit, confirmed, and attributed to the logged-in counsellor. Self-healing on
   recoverable lead-creation errors.
5. **Human-gated applications.** Applications are filed only through the Instant/Normal Apply
   UI after shortlisting — never by the model.
6. **Full auditability.** Every tool call, duration, and outcome is traced.

### Non-goals
- The agent does **not** file applications, and does not offer a "just apply" chat command.
- The agent does **not** give absolute visa/immigration/financial/English-test answers — it
  says what to verify and with whom.
- The agent does **not** convert currencies or estimate missing figures.
- No student-facing chat; this is a counsellor tool.

---

## 5. Product surfaces (the console)

A single-page console (Next.js App Router) behind a login. Layout: a **pipeline rail** of
students, a **student workspace** with tabs, and the **co-pilot chat** alongside.

| Surface | Route / component | What it is |
| --- | --- | --- |
| **Login** | `/login`, `app/api/auth/login` | Crizac email/password → encrypted session cookie. |
| **Pipeline rail** | `components/pipeline/pipeline-rail.tsx` | The counsellor's students by funnel stage (discovery → shortlisting → applying → visa → pre-departure) with heat (hot/warm/cold). |
| **Command palette / new-lead modal** | `components/shell/*` | Quick nav; create a new student lead. |
| **Workspace** | `components/workspace/workspace.tsx` + `sub-tabs.tsx` | Per-student sub-tabs: **Chat**, **Shortlist**, **Scholarships**, and **Loans** / **Pre-Departure** / **Test-Prep** (marked *Soon*). ChatView stays mounted across tab switches. (Overview, Course-canvas, Activity, Documents views also exist as components.) |
| **Co-pilot chat** | `components/workspace/chat-view.tsx`, `components/copilot/copilot-panel.tsx`, `app/api/chat` | The streaming agent conversation. Renders assistant text, collapsed tool-call traces, **inline cards**, thumbs up/down feedback (→ Langfuse score), file attach, and **voice input** (mic → `/api/transcribe`). |
| **Apply modals** | `instant-apply-modal.tsx`, `instant-apply-v2/*` | Instant Apply (passport-OCR) and Regular Apply against the university's real Crizac form. |
| **Admin tool console** | `/admin/tools`, `app/api/admin/*` | Lists all tools, their backend (Plexe/Crizac/App), which mutate data, and a manual tester. |
| **Pricing** | `/pricing` | Plan/pricing page. |
| **Dashboard / feedback / transcribe** | `app/api/dashboard`, `/api/feedback`, `/api/transcribe` | Pipeline metrics, in-app feedback, and voice-to-text input for chat. |

### Inline cards (agent-rendered UI)
The chat is not text-only. A message can carry `inlineCards: WorkspaceCard[]`, rendered as
rich, interactive cards. The card kinds (`lib/types.ts` `WorkspaceCard`):

| Kind | Rendered by | Produced by tool | Interaction |
| --- | --- | --- | --- |
| `recommendations` | `recommendations-card.tsx` | `present_recommendations` | 8–15 ranked programme cards, top few shown + "See more"; each **pinnable** to the shortlist. |
| `scholarships` | scholarships view | `scholarship_search` | Scholarship results. |
| `duplicates` | — | `duplicates_check` | Other sub-agents touching this student. |
| `profileForm` | profile form | `collect_profile` | Inline editable form for the student's *missing* fields; counsellor edits + saves (never guessed). |
| `comparison` | — | `compare_programmes` | Downloadable comparison PDF (Crizac 21-field format) surfaced with a Download button. |

**Key UX principle: presenting is not pinning.** The agent presents recommendation cards; the
*counsellor* pins them. The agent only calls `shortlist_add` when explicitly asked ("shortlist
the Leeds MSc", "pin #2").

---

## 6. The agent

### 6.1 Identity & responsibilities
System prompt (`lib/agent/system-prompt.ts`) frames the agent as *"ChatStudyAbroad, the AI
co-pilot inside the Crizac Agent Console"* helping counsellors advise a specific loaded
student. Its four jobs: recommend universities/programmes matching the profile; identify
scholarships; flag duplicate handling; persist new facts back to CAMS.

### 6.2 Profile-awareness (prompt assembly)
The system prompt is **built per turn** from:
- **Student profile block** — name, CAMS IDs, passport, mobile, education, target programme,
  destination, and any known enrichment (academic %, budget cap, preferred countries, intake
  urgency, backlogs, gap years, tests). If no student is loaded, a different block instructs
  the agent to identify the student first (collect lead details → `create_lead`) before
  shortlisting.
- **Student memory block** — soft facts learned in past conversations, injected as `- [category] text`
  with instruction to use them naturally and prefer the counsellor's latest statement if a
  memory is now wrong.

### 6.3 Behavioural rules (must-haves, verbatim intent)
- **Tool-first, never guess.** No invented rankings, tuition, deadlines, or scholarship
  amounts — always source from tool output.
- **Currency discipline.** Every figure shown exactly as returned, in **GBP (£)** — never
  convert to USD, never round into a range/"approx", never fabricate. Missing figure → say
  "not available".
- **Hedge on visa/finance/English tests.** No absolute answers; say what to verify and remind
  to confirm with consulate/university.
- **Discovery path is fixed.** Shortlisting/recommending uses **only** `course_catalogue`
  (gather candidates as data) → `present_recommendations` (render cards). Do not use
  `scholarship_search` / `university_search` for a shortlist request.
- **Economical catalogue use.** List eligible universities once (call catalogue *without* a
  university), then pull courses only for the handful you'll recommend from.
- **Confirm durable preference changes.** If the counsellor implies a durable filter (city,
  budget cap, brand/ranking, target course, intake, funding), ask one short confirming
  question, then `cams_update_profile` (append, don't replace), then re-run the search.
- **Comparisons → PDF.** Never write an inline comparison table; call `compare_programmes`.
- **Caps.** Shortlist ≤ 15 programmes, ≤ 5 applications (`lib/constants.ts`). On a full
  shortlist, don't retry — tell the counsellor and offer to remove a weaker option.
- **Self-heal `create_lead`.** On failure, attempt a fix before surfacing: pick the closest
  valid destination/course type from the error, re-check home-state spelling, retry once on
  transient 5xx; only surface genuinely user-blocking errors (bad passport/email, expired
  session). On `alreadyExists`, do not duplicate — tell the counsellor and offer to open the
  student.
- **Tight style.** Short paragraphs, tight bullets, no emojis/em-dashes, one-sentence "why"
  per recommendation.

### 6.4 The master-id / CAMS-id safety rule (critical)
A **CAMS ID is always purely numeric**. The Crizac **master id** is alphanumeric (e.g.
`IjmQ1782803306`) — an internal grouping key that must **never** be shown as the student's
CAMS ID. Self-check before showing any id; if it contains a letter, it is the master id.
When `create_lead` returns, the numeric camsId/applicationId is the new CAMS id — never its
masterId. This rule is marked "absolute" in the prompt. Note: `StudentProfile.camsId` is in
fact the **masterId** internally; the user-facing numeric CAMS IDs live on `applications[]`.

The rule is enforced in **three layers**, not by prompt alone: (1) the system prompt;
(2) `scrubMasterIds()` applied to every tool result *before the model sees it* (`stream.ts`);
and (3) a render-layer scrub in the chat UI. Any port should preserve all three (or fold
them into a runtime output guard).

### 6.5 Conversation lifecycle
- **Transport:** `POST /api/chat` streams **SSE** (`text/event-stream`), driven by
  `lib/agent/stream.ts` (`streamChat`). Frames stream token-by-token (typed `StreamEvent`s:
  `text_delta`, `tool_call_*`, `trace`, `memory_update`, `student_refresh`, `error`) and
  terminate with `[DONE]`. Payload carries `camsId`, the message history, and the counsellor's
  identity (email, tenant, agentId, Crizac token) pulled from the session.
- **Tool loop:** an agentic loop **capped at 6 turns**; each turn calls
  `client.messages.stream(...)`, and on `stop_reason: "tool_use"` executes the tools, appends
  `tool_result` blocks, and loops. Inline cards are emitted as structured events.
- **Model:** Claude **`claude-sonnet-4-6`** for the main compose loop (`max_tokens` 8192);
  **`claude-haiku-4-5`** for sidecars (memory extraction, titles).
- **Post-turn memory extraction** (`lib/memory/extract.ts`): a Haiku sidecar reads the latest
  exchange and extracts 0–3 durable soft facts (categories: `preference`, `constraint`,
  `concern`, `context`, `counselor_note`) into the student's memory. Best-effort — never
  blocks the turn. It deliberately does *not* capture formal CRM fields (exact budget/scores);
  those go through `cams_update_profile`.
- **Titles:** `lib/agent/title.ts` (Haiku) names conversations.
- **Persistence:** PostgreSQL + Drizzle — `students` (profile JSONB by cams_id, incl.
  shortlist), `conversations` (one thread per student), `messages` (role, content, tool traces,
  inline cards).

---

## 7. Tool catalogue

The registry (`lib/tools/registry.ts`) declares **28 tools**. Each has a Zod input schema, an
Anthropic tool spec, a `category`, a `source` (mock/live), and a **backend/provider** and a
**mutation flag** (`lib/tools/admin-meta.ts`). Three tools are **hidden from the model**
(`AGENT_HIDDEN_TOOLS`) — callable only by API routes/human UI, never surfaced to the LLM.

**Provider legend:** `crizac` = Crizac CAMS CRM REST API as the logged-in counsellor ·
`plexe` = Plexe inference (Claude+Weaviate / XGBoost) · `app` = local DB / curated data.
**W** = mutates data (`WRITE_TOOLS`).

### Student lookup & profile (CAMS)
| Tool | Provider | W | Purpose |
| --- | --- | --- | --- |
| `cams_lookup_student` | crizac | | Fetch CRM profile by masterId or by name scan. |
| `cams_update_profile` | app | ✅ | Write counsellor-captured fields (budget, waivers, preferences) to the local profile store (not synced to Crizac). |
| `collect_profile` | app | | Render inline form of the student's current + **missing** fields for the counsellor to complete. Read-only (counsellor saves). |
| `duplicates_check` | crizac | | Other agencies' applications sharing this passport (duplicatePassport API). |
| `application_activity` | crizac | | Status-change history + counsellor comments timeline. |

### Discovery (Crizac catalogue + Plexe)
| Tool | Provider | W | Purpose |
| --- | --- | --- | --- |
| `course_catalogue` | crizac | | **The only discovery tool the agent may use.** Contracted, eligibility-checked catalogue; returns candidate courses as **data** (GBP tuition, app fee, duration, city, intake, exact degree type). Called without a university → lists eligible universities; with a university → its eligible courses. |
| `present_recommendations` | app | | **The only tool that renders recommendation cards.** Takes the agent's ranked list (8–15), fields copied verbatim from the catalogue + a fit tag + one-line rationale. |
| `programme_detail` | crizac | | Deep info on one programme (deadlines, requirements). |
| `university_search` | plexe | | NL university search (Claude→Weaviate; local fallback). |
| `university_recommendations` | plexe | | Ranked universities for the student's preferences (scholarship/IELTS/entry info). |
| `scholarship_search` | plexe | | Scholarship-flagged courses (deduped per university; static fallback). Used **only** when scholarships are explicitly asked for. |
| `crizac_filters` | crizac | | Available eligibility filters (intakes, course types, countries, states). |
| `visa_requirements` | app | | Curated in-app visa dataset per destination (verify externally). |
| `offer_prediction` | plexe | | XGBoost offer-probability (`crizac-offer-prediction-xgb-prod-v2`, AUC ~0.73) from raw application fields; returns probability (0–1), class, threshold. **Read-only.** |

### Hidden from the model (API-route / human-UI only)
| Tool | Provider | W | Why hidden |
| --- | --- | --- | --- |
| `course_search` | plexe | | Plexe `crizac-nl-search` (Claude→Weaviate). Used by the Shortlist "Suggested" rows via `executeTool`, but the agent must discover only via `course_catalogue`. |
| `course_recommendations` | plexe | | Plexe `crizac-recommendations` (ambitious/target/safe). API-route only. |
| `apply_to_programme` | crizac | ✅ | Files a **real application**. Per Crizac policy, applications are created **only** through the Instant/Normal Apply UI after shortlisting — the model may never call this. |

### Writes into Crizac / external actions (confirm-gated)
| Tool | Provider | W | Purpose |
| --- | --- | --- | --- |
| `create_lead` | crizac | ✅ | Create a student as an **enquiry** against the destination's Enquiry university/course. Passport required; mobile optional. Does *not* file an application. Self-healing on failure. |
| `create_leads_bulk` | crizac | ✅ | Bulk lead creation (loop). |
| `crizac_update_application` | crizac | ✅ | Write visa history (`studentDetails.visaRefusal`) back to the CRM. Requires confirmation. Only visa history syncs today. |
| `submit_scholarship_enquiry` | crizac | ✅ | Submit a scholarship enquiry (needs Crizac `scholarshipId`; contact details from CAMS). Confirm first. |
| `loan_apply_for_student` | crizac | ✅ | Submit a loan eligibility request (loan-enquiry API). |
| `send_email` | crizac | ✅ | Email the student from the agent's address (sendAgentMail). Preview → confirm convention. |

### Student workspace (student-scoped)
| Tool | Provider | W | Purpose |
| --- | --- | --- | --- |
| `shortlist_add` | app | ✅ | Pin a programme to *this* student's shortlist. Only on explicit request; must pass real university/programme names. |
| `shortlist_remove` | app | ✅ | Unpin a programme. |
| `compare_programmes` | app | | Build the downloadable comparison PDF (Crizac 21-field format) for the shortlist. |

### Drafting
| Tool | Provider | W | Purpose |
| --- | --- | --- | --- |
| `draft_sop` | crizac | | Draft a statement of purpose (Crizac generator when available; else returns structured facts to draft from). |
| `draft_lor` | crizac | | Draft a letter of recommendation (same fallback). |

> **Governance tiers implied by the code** (used in the `csa-counsellor` Rya manifest):
> read-only lookups (`allowed`/`read_only`), confirm-gated writes (`approval_required`:
> `cams_update_profile`, `crizac_update_application`, `submit_scholarship_enquiry`,
> `loan_apply_for_student`, `send_email`), hidden tools (`disabled`: `course_search`,
> `course_recommendations`, `apply_to_programme`), and student-scoped tools whose `camsId`
> must be **server-pinned** so the model can never target another student (`shortlist_*`,
> `compare_programmes`, `cams_update_profile`).

---

## 8. Governance & safety requirements

1. **Grounding.** No figure may appear in a reply unless a tool returned it. (In Rya this
   becomes the runtime grounding gate blocking any money figure not sourced from a tool.)
2. **Hidden tools are enforced, not just omitted.** `apply_to_programme`, `course_search`,
   `course_recommendations` are stripped from the model's tool specs; only server code calls
   them. Applications are never filable from chat.
3. **Confirmation before mutation.** Write tools that touch the CRM (visa history, scholarship
   enquiry, loan, email) require explicit counsellor confirmation; `create_lead` is explicit by
   flow. Today confirmation is a **prompt convention**; the target is a **runtime approval gate**.
4. **Student scoping.** `camsId` on student-scoped tools (`STUDENT_SCOPED_TOOLS`:
   `shortlist_add`, `shortlist_remove`, `cams_update_profile`, `compare_programmes`) is
   **force-overridden in the loop** to the effective student key — the model's supplied
   `camsId` is never trusted. `create_lead` is processed first so a same-message
   `shortlist_add` targets the right key. (In Rya this becomes declarative arg pinning.)
5. **Master-id secrecy** (§6.4).
6. **Caps** — shortlist 15, applications 5.
7. **Self-healing writes** — `create_lead` retries/repairs before surfacing errors; never
   creates duplicates.
8. **Attribution & audit** — every action runs as the logged-in counsellor's Crizac identity;
   every tool call is traced with duration and outcome (`lib/observability`). Tool failures are
   logged for CloudWatch.

---

## 9. Key workflows

### 9.1 Identify → recommend → shortlist → apply (the spine)
1. Counsellor opens/loads a student, or (no student yet) the agent collects lead details
   (name, passport, email, gender, home state, destination, course type, intake) and calls
   `create_lead`.
2. Counsellor asks for options → agent calls `course_catalogue` (universities, then courses),
   ranks, gives a short text summary, and calls `present_recommendations` → pinnable cards.
3. Counsellor pins specific programmes → agent calls `shortlist_add` (only on explicit ask).
4. Counsellor clicks **Instant Apply** or **Normal Apply** on a shortlist row → the *human UI*
   files the application (see §10). The agent never files.

### 9.2 Scholarships
Only when explicitly asked: `scholarship_search` → scholarship card; optional
`submit_scholarship_enquiry` (confirm-gated write).

### 9.3 Duplicate handling
`duplicates_check` surfaces other sub-agents on the same passport so counsellors don't collide.

### 9.4 Profile enrichment
`collect_profile` renders an inline form for missing fields → counsellor saves → `cams_update_profile`.
Durable preference changes are confirmed before being written (append semantics).

### 9.5 Comparison
`compare_programmes` → downloadable PDF in Crizac's 21-field format (no inline tables).

### 9.6 Offer prediction, drafting, loan, email
`offer_prediction` (read-only ML), `draft_sop`/`draft_lor`, `loan_apply_for_student`,
`send_email` — the last three are confirm-gated writes.

---

## 10. Apply flows (deep dive)

Applications are filed by a **human clicking Apply**, in one of two flows, both driving
Crizac's **automation form builder** (see `docs/apply-flows.md`, `docs/automation-form-builder.md`).

| | **Regular Apply** | **Instant Apply (v2, doc-extraction)** |
| --- | --- | --- |
| Personal fields | typed / prefilled from CAMS | **passport OCR** (`uploadDocToExtract`) |
| University questions | the uni's real form, all fields | the form minus passport + contact fields |
| Submit | `POST /v2/api/application` | `POST /v3/api/application` (create → attach docs → finalize) |

**Automation form builder** — the non-obvious core:
- A university publishes **several form builders per (university, courseType)**, each scoped to
  a set of **student home countries** (`countryId`). `pickFormBuilder` prefers the builder
  whose `countryId` includes the student's home country (else course-type + most-recent).
  Picking by courseType alone can hand, e.g., a Nepal student the Ghana form (wrong state
  list/dial code).
- Fields carry `systemKey`/`automationKey`, `agentKey` (CAMS data path), `superMandatory`+
  `mandatory` (both required for Crizac to reject a blank), `showAgent`/`showConsole` (render
  rules), inline or option-group dropdowns, and `advancedRules` (conditional visibility).
- **The defaultId rule (classic opaque 400):** id-backed dropdowns must submit both the option
  **name** (`defaultValue`) and its **id** (`defaultId`); an empty id → generic
  `400 "Please enter all required feilds"`. `fillFormConfig` resolves ids from names.
- A **JavaScript overlay** (`form-runtime.ts`) re-implements the portal's `api.*` field-logic
  shim (hide/show/required/computed) sandboxed; `findMissingRequiredFields` does a pre-flight
  so the counsellor gets `missing: X, Y` instead of Crizac's opaque 400.
- Documents come from the builder; each is OCR-extracted then attached one-per-request (Lambda
  6 MB cap). Passport OCR (Haiku-class extraction) fills the personal block; academic docs are
  attached but not yet parsed.

> This subsystem is a large surface of real-world CRM behaviour that the co-pilot orchestrates
> but does not *decide* — it is human-driven. Any port must preserve "the agent proposes,
> the human files."

---

## 11. Data model

- **StudentProfile** (`lib/types.ts`): stable key = Crizac `masterId`; `applications[]` each
  with its own numeric `camsId`, university, course, intake, primary/secondary status.
  Identity + contact (passport, mobile, email, DOB, home state/country), destination,
  current/target education, `stage` (funnel), `heat`, timestamps. Enrichment: `academicPercentage`,
  `budgetCapUsd`/`budgetCovers`, `preferredCountries`/`preferredCity`, `intakeUrgency`/`intake`,
  `backlogs`, `gapYears`, `visaHistory`, `testsTaken[]`, `brandConscious`, fees
  (gross/net/scholarship). `shortlist: ShortlistEntry[]`.
- **ProfileFormValues**: the counsellor-editable subset (`collect_profile`).
- **MemoryItem**: `{ category, text }` over 5 categories; long-term soft facts per student.
- **ShortlistEntry**: `{universityId, programmeId, fit (ambitious|target|safe), notes?, addedAt,
  universityName?, programmeName?, country?, annualTuitionUsd?, intake?, appliedApplicationId?,
  appliedAt?}` — display fields captured at pin time; `appliedApplicationId` marks "Applied".
- **WorkspaceCard** union: `recommendations | scholarships | duplicates | profileForm | comparison`.
- **Persistence** (Postgres + Drizzle, `lib/db/schema.ts`):
  - `student_state` — **authoritative, app-owned** counsellor workflow state (owner, stage, heat,
    lastTouchedAt, dueDate, nextBestAction, `shortlist` jsonb, notes). Never overwritten by Crizac sync.
  - `student_cache` — a **truncatable projection** of Crizac data (`profile` jsonb), refilled on next fetch.
  - `student_memory` — soft per-student facts `{camsId, category, text, sourceExcerpt}`, deduped in app, never synced to Crizac.
  - `conversations` — one thread per student (`camsId` nullable = a pre-student "lead chat"), with `counselorId` + `agencyId`.
  - `messages` — `{role, content, toolCalls jsonb, inlineCards jsonb, createdAt}`.
  - (`students` — a deprecated single-blob JSONB table, retained but unread.)

---

## 12. Integrations

| Integration | Used for | Status |
| --- | --- | --- |
| **Anthropic** (`claude-sonnet-4-6` + `claude-haiku-4-5`) | Compose loop, memory extraction, titles, NL query parsing, document analysis | Live (required) |
| **Crizac CAMS CRM** (`api-prod.crizac.com`) | Auth, student lookup, catalogue, filters, leads, apply, visa/scholarship/loan/email writes, activity, form builders | Live (per-counsellor token); local mock fallback when unconfigured |
| **Plexe** (`api.plexe.ai`) | NL course/university search & recommendations (Claude+Weaviate), offer-prediction XGBoost (`crizac-offer-prediction-xgb-prod-v2`, AUC ~0.73) | Live, with local fallbacks; offer-prediction endpoint intermittent |
| **OpenAI** (`gpt-4o-transcribe`) | Voice-to-text for chat input (`/api/transcribe`) | Live when `OPENAI_API_KEY` set; else 501 |
| **Langfuse** (self-hosted, EC2/AWS) | Per-turn tracing, tokens, latency, sessions; thumbs feedback as scores | Live when configured; no-op otherwise |
| **PostgreSQL** | Persistence | Live (Docker) |

Tool outputs carry a **runtime source** (`crizac` / `plexe` / `local` / `skipped` / `error`),
decided per-call by whether Crizac/Plexe is configured (`isCrizacConfigured()` /
`isPlexeConfigured()`) — so the same tool degrades to local seed/static data gracefully.
The current code is substantially wired to live Crizac/Plexe. Note the **search-rewire
roadmap** (`docs/crizac-search-rewire.md`): retire Plexe for search/recommendations in favour
of Crizac's native course-search + eligibility endpoints, while keeping Plexe for offer
prediction.

---

## 13. Auth & multi-tenancy

- **Per-counsellor Crizac login** (`lib/auth/crizac-login.ts`): server-side
  `POST /v1/api/agentAuth/login` (email+password; `/v1` avoids the device-first `/v2` OTP that
  a headless Lambda can't complete). Returns a Crizac bearer token, `tenantId`, `agentId`,
  `counsellorNumber`.
- **Session** (`lib/auth/session.ts`): an **encrypted JWT** (`jose`, A256GCM) in a
  `__Host-csa_session` cookie, holding the counsellor's email, Crizac token, device token,
  tenant, agentId, counsellorNumber. 30-day TTL (a shorter TTL silently logged people out
  mid-write). `SESSION_SIGNING_KEY` required.
- **Middleware** (`middleware.ts`, Node runtime): everything except `/login` and auth routes
  requires a session; API routes get `401 JSON`, pages redirect to `/login`.
- **Scoping:** all CRM reads/writes run as the logged-in counsellor's token; tenant = agency.
  `create_lead` auto-fills counsellor identity from the session.

---

## 14. Non-functional requirements

- **Streaming:** token-by-token SSE; the UI must render partial text and inline cards live.
- **Durability (gap today):** a chat turn is a synchronous SSE stream — a mid-turn crash
  strands the run and a dropped connection loses the stream. Confirmation gates are prompt
  conventions, not durable pauses. (This is the primary motivation for the Rya port; see §15.)
- **Observability:** structured logs + tool traces (name, duration, status); tool errors
  surfaced to CloudWatch. Admin console exposes per-tool backend + mutation flags.
- **Resilience:** Plexe tools fall back to local catalogue/ranking; `create_lead` self-heals;
  offer-prediction degrades gracefully when its endpoint is down.
- **Safety:** grounding, currency discipline, master-id secrecy, caps (§8).

---

## 15. Building on Rya (port target)

The eventual goal is to rebuild this agent on **Rya**, moving the safety/durability guarantees
from prompt conventions and app code into **runtime primitives**. A first mapping already
exists as the `csa-counsellor` example (`rya.agent.yaml` + `src/agent.py`).

| ChatStudyAbroad today | Rya primitive |
| --- | --- |
| SSE `streamChat` loop, synchronous | **Durable chat turns** — leased, crash-reclaimed, resumable token streams (`POST /agents/{id}/turns`). |
| Anthropic Sonnet compose + Haiku sidecars | **Model routes** — `compose: sonnet`, `extract/title/ocr: haiku`, cost visible per route. |
| Post-turn Haiku memory extraction; profile in prompt | **`ctx.memory`** collections (`conversations`, `student_facts`, `student_state`) + memory blocks. |
| 28-tool registry with categories/providers | **Tools manifest** — each tool declared with a permission; wired to its live Crizac/Plexe endpoint via `url:`. |
| `AGENT_HIDDEN_TOOLS` (stripped from specs) | **`permission: disabled`** — runtime-enforced, not prompt-stripped. Kill switches can disable any tool live without redeploy. |
| Confirm-before-write prompt convention | **`permission: approval_required`** — `ctx.approvals.request` pauses the run *durably* until a human approves; the model never sees a gated tool call execute. Applies to `send_email`, `crizac_update_application`, `submit_scholarship_enquiry`, `loan_apply_for_student`, and (as policy) profile writes. |
| `camsId` trusted from context | **Server-side arg pinning** — `pin: {camsId: event.payload.camsId}` on `shortlist_*`, `compare_programmes`, `cams_update_profile`: the model can never target another student. |
| "Never invent a number" prompt rule | **Grounding gate** — `ctx.guard.check_grounding` / guard config blocks any money figure not returned by a tool this run. |
| Per-counsellor Crizac token in cookie | **Scoped credentials / secrets** — `ctx.secrets`, egress firewall, per-caller identity. |
| CloudWatch logs + tool traces | **Observability** — logs, traces, audit as first-class (`observability: {logs, traces, audit}`). |
| Plexe XGBoost offer-prediction | **Custom model** — `models: [{id: offer-prediction-xgb, type: custom}]`. |
| Apply flows (human-filed) | Stay human-driven; `apply_to_programme` remains **disabled** for the agent. |

### Suggested phased port
1. **Phase 1 — Governance shell first.** Stand up the Rya agent with the full 28-tool manifest
   (permissions, pins, kill switches), model routes, memory collections, and the durable
   approval gate on outbound email — tools declared for governance, handler runs the
   conversation shell and calls no unwired tool. (This is exactly what `csa-counsellor` does.)
2. **Phase 2 — Wire tools one at a time.** Add each tool's live `url:` (Crizac/Plexe),
   read-only lookups first, then confirm-gated writes behind runtime approvals.
3. **Phase 3 — Durable turns + grounding gate + custom offer-prediction model** in production;
   migrate the console to talk to `rya serve` over HTTP (no SDK lock-in).
4. **Phase 4 — Keep apply flows human-driven**, orchestrated by the app UI, with the agent
   proposing shortlists only.

---

## 16. Open questions & risks

- **`cams_update_profile` is app-local, not synced to Crizac** — is that intended long-term, or
  should enrichment write back to CAMS?
- **Offer-prediction endpoint availability** — the XGBoost inference is intermittently down;
  needs a stable deployment or a clearer "unavailable" UX.
- **Confirmation is a prompt convention today** — until moved to a runtime approval gate, a
  model mistake could trigger a write; the Rya port closes this.
- **Passport-OCR / academic-doc parsing** — academic docs are attached but not parsed into
  qualification/subject; those fields stay manual.
- **Grounding is prompt-only today** — no runtime enforcement that every figure came from a
  tool (master-id scrubbing exists, but figure-grounding does not); the Rya grounding gate is
  the intended backstop.
- **Admin console has no RBAC** — any logged-in counsellor can reach `/admin/tools` (which can
  run write tools with an arm-then-confirm gate). Needs a role check.
- **Search-rewire in flight** — some tool descriptions say "live Crizac catalogue" while still
  calling Plexe; the rewire (`docs/crizac-search-rewire.md`) fixes this. A port should target
  the post-rewire discovery path.
- **`cams_update_profile` is app-local** (see above) — the biggest data-sync gap to resolve.

---

### Source references
- Agent: `lib/agent/system-prompt.ts`, `lib/agent/stream.ts`, `lib/agent/title.ts`
- Tools: `lib/tools/registry.ts`, `lib/tools/admin-meta.ts`, `lib/tools/impl/*`
- Memory: `lib/memory/extract.ts`, `lib/memory/store.ts`
- Data: `lib/types.ts`, `lib/constants.ts`, `lib/db/schema.ts`, `lib/data/*`
- Integrations: `lib/crizac/client.ts`, `lib/plexe/client.ts`, `lib/observability/langfuse.ts`, `lib/documents/analyze.ts`, `app/api/transcribe`
- UI: `app/page.tsx`, `components/workspace/*`, `components/shell/*`, `app/admin/tools/page.tsx`, `app/pricing/page.tsx`
- Roadmap: `docs/crizac-search-rewire.md`
- Apply: `docs/apply-flows.md`, `docs/automation-form-builder.md`, `lib/crizac/*`
- Auth: `lib/auth/session.ts`, `lib/auth/crizac-login.ts`, `middleware.ts`
- Rya mapping: `examples/csa-counsellor/rya.agent.yaml`, `examples/csa-counsellor/src/agent.py`
</content>
</invoke>
