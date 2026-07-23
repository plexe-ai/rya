"""ChatStudyAbroad counsellor agent, on Rya.

Migration of the production agent onto Rya (see MIGRATION.md). The event handler
drives the **governed model loop** (``ctx.llm.run``): the model reasons and calls
tools, every call routed through ``ctx.tools.call`` so permissions, server-side
pins, the Action Guard, and the A2 id-secrecy scrub all apply. The **real system
prompt** is ported from ``lib/agent/system-prompt.ts``.

Wired so far:

- **Phase 1 — discovery spine.** ``course_catalogue`` (Crizac's name-resolution
  cascade over a local catalogue seed) + ``present_recommendations`` (a pure
  shaper). Discovery/recommendation flows use ONLY these two.
- **Phase 2 — CAMS lookups + student workspace.** ``cams_lookup_student``,
  ``duplicates_check``, ``application_activity`` (read the CAMS store);
  ``shortlist_add`` / ``shortlist_remove`` / ``compare_programmes`` (mutate this
  student's workspace — all server-side **pinned** to camsId so the model can
  never target another student); ``collect_profile`` (surface the enrichment
  form). CAMS reads return the numeric CAMS id but the alphanumeric master id is
  scrubbed at the tool boundary by A2 — never surfaced.
- **Phase 3 — lead creation + retry/repair + adoption.** ``create_lead`` /
  ``create_leads_bulk`` (the first CAMS writes). create_lead is passport-
  idempotent (a retry after a timeout-then-success never double-creates), declares
  an A1 ``retry:`` policy, and pairs with an ``@agent.repair`` callback that
  self-heals an unrecognised destination / course type / home state (A1). On
  success the manifest's ``adopt: {camsId: student_state.camsId}`` (A5) records the
  new student as the session's active one, so the pinned workspace tools — now
  pinned to ``memory.student_state.camsId`` — target it for the rest of the turn.
- **Phase 5 — prediction + sidecars + tail.** The discovery tail
  (``programme_detail``, ``university_search``, ``university_recommendations``,
  ``scholarship_search`` → a ``scholarships`` card, ``crizac_filters``,
  ``visa_requirements``), ``offer_prediction`` (Plexe via ``PLEXE_API_KEY``,
  graceful ``{ok: false}`` when unset / down), and ``draft_sop`` / ``draft_lor``
  (return grounded student context for the compose model to write from). A3
  lands in full: the Crizac-backed tools declare ``provider: crizac`` +
  ``scopes`` + ``require_user`` + a live ``url:`` (the local handler wins offline,
  so the suite stays deterministic; a real deploy routes to Crizac), a
  ``crizacLogin`` turn mints a per-counsellor bearer via ``ctx.connections.upsert``
  (overwrite-in-place, no stale duplicates), and an upstream ``401`` on a live
  Crizac call raises ``E_CONNECTION_EXPIRED`` → the run ends ``needs_reconnect``
  (no silent refresh). Title / passport-OCR routes are declared but intentionally
  not wired into the chat loop (OCR belongs to the human apply subsystem).
- **Phase 4 — gated writes real.** The confirm-before-write actions
  (``cams_update_profile``, ``crizac_update_application``,
  ``submit_scholarship_enquiry``, ``loan_apply_for_student``, and the one real
  outbound ``send_email``) are wired as leaves that execute ONLY through an
  approved ``ctx.approvals.request`` action. The event handler detects the write
  intent from the turn payload, bakes the **resolved pinned camsId** into the
  action input (``engine._execute_action`` does not re-pin), runs the **grounding
  gate** on the composed reply before a loan write, then suspends the run durably
  until a human approves.

Only the wired tools are exposed to the loop (EXPOSED_TOOLS); the rest stay
governed-but-unwired until their phase lands, so the model can never route to a
tool with no implementation. Gated writes (approval_required) are never exposed
to the loop at all — they run only via ``ctx.approvals.request`` actions.

Cards: tool handlers are leaves (no ``ctx``), so — exactly like production, which
builds cards client-side from ``tool_call_finish`` events — the event handler
emits UI frames AFTER the loop, from the tool calls it made.

Local stores (``data/*.json``) stand in for live Crizac; swap each leaf for a
manifest ``url:`` tool (Phase 5, with the per-counsellor login credential +
reconnect-on-401) without touching the loop or the cards.
"""

import difflib
import json
import re
from pathlib import Path

from rya import RyaRecoverableToolError, define_agent

agent = define_agent()

# Tools wired so far. ctx.llm.run is given this explicit list so the loop never
# exposes a declared-but-unwired (or gated) tool. Expand per phase; drop the
# argument entirely (auto-expose all allowed/read_only tools) once every one is
# wired. Gated tools (cams_update_profile, send_email, …) are deliberately absent
# — they execute only through approval actions, never a direct in-loop call.
EXPOSED_TOOLS = [
    # Phase 1 — discovery spine
    "course_catalogue", "present_recommendations",
    # Phase 2 — CAMS lookups + student workspace
    "cams_lookup_student", "duplicates_check", "application_activity",
    "shortlist_add", "shortlist_remove", "compare_programmes", "collect_profile",
    # Phase 3 — lead creation (retry + self-heal + camsId adoption)
    "create_lead", "create_leads_bulk",
    # Phase 5 — prediction + drafting + the discovery tail
    "programme_detail", "university_search", "university_recommendations",
    "scholarship_search", "crizac_filters", "visa_requirements",
    "offer_prediction", "draft_sop", "draft_lor",
]

MAX_SHORTLIST = 15   # lib/constants.ts
MAX_APPLICATIONS = 5

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CATALOGUE_PATH = _DATA_DIR / "catalogue.json"
_STUDENTS_PATH = _DATA_DIR / "students.json"


# ---------------------------------------------------------------------------
# System prompt — ported from lib/agent/system-prompt.ts (buildSystemPrompt).
# Built per-turn from the (optional) student profile + learned-memory blocks.
# ---------------------------------------------------------------------------
_BASE_RULES = f"""You are ChatStudyAbroad's counsellor assistant for study-abroad advisors.
Your job is to help the counsellor discover programmes, shortlist and compare them
for a specific student, and keep the student's record accurate.

Rules:
- Tool-first: always use tools instead of guessing. If a tool would answer it and
  you have not called it, call it. If you truly cannot answer, say so and name the
  tool that would.
- Currency discipline: every figure comes straight from the Crizac catalogue and is
  shown to the counsellor exactly as returned, in GBP (£). Never convert an amount
  to USD or any other currency, never round it into a range or "approx", and never
  invent a number. If a figure is missing, say "not available".
- How to recommend: shortlisting or recommending programmes uses ONLY
  course_catalogue (to gather candidates) and present_recommendations (to show the
  cards). Do NOT use any other discovery tool for a shortlist/recommendation
  request. course_catalogue returns DATA only; present_recommendations IS the
  presentation. Aim for a ranked list of about 8-15 programmes.
- course_catalogue has two modes: call it WITHOUT a university (with destination
  country + intake + course type) to list eligible universities, then WITH a
  university name to get that university's eligibility-checked courses. When a
  specific degree is requested, do not substitute a different degree.
- Presenting is not pinning: never call shortlist_add on your own. Only shortlist
  when the counsellor EXPLICITLY asks. If they ask for recommendations or a
  comparison, present the options and STOP.
- Never show a master id: never display a Crizac master id — the internal
  alphanumeric grouping key (a few letters then a long digit run, e.g.
  "IjmQ1782803306"). It is NOT a CAMS id. A CAMS id is ALWAYS purely numeric.
  Report only the numeric CAMS id(s). This is absolute.
- Caps: a student's shortlist holds at most {MAX_SHORTLIST} programmes and at most
  {MAX_APPLICATIONS} applications. Keep proposed sets to about 5. If the shortlist
  is full, do not retry — tell the counsellor.
- Comparisons use compare_programmes (a PDF), never inline tables."""


def _memory_block(memory: list) -> str:
    if not memory:
        return ""
    lines = "\n".join(f"- [{m.get('category', 'note')}] {m.get('text') or m.get('fact', '')}"
                      for m in memory)
    return (
        "\n\n<student_memory>\n"
        "Soft context you have learned about this student in past conversations. Use it\n"
        "naturally and do not re-ask what you already know. If a memory is now wrong,\n"
        "prefer the latest thing the counsellor says.\n"
        f"{lines}\n"
        "</student_memory>"
    )


def _profile_block(student: dict) -> str:
    if not student:
        return (
            "\n\nNo student is loaded yet. Identify the student first: ask for full name, "
            "passport, email, home state, destination country, course type and intake, then "
            "create_lead. You can still present recommendation cards. Never call shortlist_add "
            "until the student exists in CAMS."
        )
    # A "CAMS ID" is ALWAYS numeric; an alphanumeric value is the master id and is
    # never shown (mirrors the profile-block self-check in system-prompt.ts).
    cams = [c for c in (student.get("camsIds") or []) if str(c).isdigit()]
    return (
        "\n\nYou are loaded into a session for an active student. Call cams_update_profile "
        "whenever the counsellor reveals new facts.\n"
        "<student_profile>\n"
        f"Name: {student.get('name', '(unknown)')}\n"
        f"CAMS ID(s): {', '.join(cams) if cams else '(none on file)'}\n"
        f"Destination preference: {student.get('destination', '(not set)')}\n"
        f"Target programme: {student.get('target', '(not set)')}\n"
        "</student_profile>"
    )


def build_system_prompt(student: dict = None, memory: list = None) -> str:
    return _BASE_RULES + _profile_block(student or {}) + _memory_block(memory or [])


# ---------------------------------------------------------------------------
# Discovery tools — leaves over the local catalogue seed. These mirror the
# Crizac name-resolution cascade from lib/tools/impl/course-catalogue.ts.
# ---------------------------------------------------------------------------
def _catalogue() -> dict:
    return json.loads(_CATALOGUE_PATH.read_text())


# A programme index over the catalogue: programmeId -> the course dict enriched
# with its university id/name/country. This is the port's form of production's
# `isKnownId` guardrail — shortlist_add pins a programme ONLY when it exists in
# the real catalogue, so the model can never pin an invented id (prod rejected
# bare slugs the same way; here the catalogue ids ARE the real ids).
def _programme_index() -> dict:
    idx = {}
    for u in _catalogue()["universities"]:
        for c in u.get("courses", []):
            idx[c["programmeId"]] = {
                **c,
                "universityId": u["universityId"],
                "universityName": u["university"],
                "country": u["country"],
            }
    return idx


# ---------------------------------------------------------------------------
# CAMS student store — a local stand-in for Crizac CAMS. Keyed by masterId (the
# alphanumeric grouping key); numeric CAMS ids live under applications[].camsId.
# Leaf tools have no ctx, so mutations land in this in-process dict (mirrors
# production's getStudentByCamsId / updateStudent local fallback). Swap for
# Crizac `url:` tools in Phase 3.
# ---------------------------------------------------------------------------
def _load_students() -> dict:
    raw = json.loads(_STUDENTS_PATH.read_text())
    return {s["masterId"]: s for s in raw["students"]}


_STUDENTS = _load_students()


def _cams_ids(student: dict) -> list:
    """The numeric CAMS ids for a student — the ONLY id ever surfaced."""
    return [str(a["camsId"]) for a in student.get("applications", []) if a.get("camsId")]


def _surface_cams(query, student: dict):
    """The numeric CAMS id to echo back to the counsellor. Never the alphanumeric
    masterId (the grouping key): if the query itself is numeric it IS a CAMS id,
    otherwise fall back to the student's first numeric CAMS id. Returns None rather
    than ever surfacing the masterId — the A2 scrub is the backstop, this is the
    correctness rule."""
    q = str(query or "")
    if q.isdigit():
        return q
    ids = _cams_ids(student or {})
    return ids[0] if ids else None


def _find_student(query: str):
    """Resolve a student by masterId, numeric CAMS id, or name (the cascade
    cams_lookup_student walks). Returns ``(masterId, student)`` or ``(None, None)``."""
    q = (query or "").strip()
    if not q:
        return None, None
    if q in _STUDENTS:                                   # direct masterId hit
        return q, _STUDENTS[q]
    if q.isdigit():                                      # numeric CAMS id -> owning student
        for mid, s in _STUDENTS.items():
            if q in _cams_ids(s):
                return mid, s
    ql = q.lower()                                       # name scan
    for mid, s in _STUDENTS.items():
        if (s.get("name") or "").strip().lower() == ql:
            return mid, s
    return None, None


_COUNTRY_ALIASES = {
    "uk": "united kingdom", "u.k.": "united kingdom",
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "uae": "united arab emirates", "nz": "new zealand",
}


def _expand_alias(q: str) -> str:
    return _COUNTRY_ALIASES.get((q or "").strip().lower(), q or "")


def _resolve(options: list, query: str) -> dict:
    """Port of resolveId: exact -> shortest startsWith -> shortest includes ->
    reverse (an option name >3 chars contained in the query, longest first)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    named = [o for o in options if o.get("name")]
    exact = [o for o in named if o["name"].lower() == q]
    if exact:
        return exact[0]
    starts = sorted((o for o in named if o["name"].lower().startswith(q)),
                    key=lambda o: len(o["name"]))
    if starts:
        return starts[0]
    incl = sorted((o for o in named if q in o["name"].lower()),
                  key=lambda o: len(o["name"]))
    if incl:
        return incl[0]
    rev = sorted((o for o in named if len(o["name"]) > 3 and o["name"].lower() in q),
                 key=lambda o: len(o["name"]), reverse=True)
    return rev[0] if rev else None


# Degree-family matching (lib/degree.ts, condensed). Specific families first;
# whole-token match. degreeTypeMatches keeps a course unless it confidently
# belongs to a different family than the one requested.
_DEGREE_PATTERNS = [
    ("mba", r"\bmba\b"), ("mbbs", r"\bmbbs\b"), ("llm", r"\bllm\b"),
    ("phd", r"\bphd\b"), ("meng", r"\bmeng\b"), ("beng", r"\bbeng\b"),
    ("bba", r"\bbba\b"), ("llb", r"\bllb\b"),
    ("ms", r"\b(?:ms|msc|master|masters)\b"), ("ma", r"\bma\b"),
    ("bs", r"\b(?:bs|bsc|bachelor|bachelors)\b"), ("ba", r"\bba\b"),
]


def _degree_family(text: str):
    t = (text or "").lower()
    for family, pat in _DEGREE_PATTERNS:
        if re.search(pat, t):
            return family
    return None


def _degree_matches(requested, programme_name: str) -> bool:
    if not requested:
        return True
    fam = _degree_family(programme_name)
    return fam is None or fam == requested


def _subject_terms(keyword: str) -> list:
    if not keyword:
        return []
    stripped = re.sub(
        r"\b(?:ma|msc|ms|mba|meng|llm|mfa|march|mres|mphil|mph|phd|ba|bsc|bs|beng|bba|llb|"
        r"masters?|bachelors?|of|in|the|for)\b", " ", keyword.lower())
    return [t for t in re.split(r"[^a-z0-9]+", stripped) if len(t) >= 2]


@agent.tool("course_catalogue", input_schema={
    "type": "object",
    "properties": {
        "destinationCountry": {"type": "string", "description": "Destination country, e.g. 'UK'."},
        "intake": {"type": "string", "description": "Intake, e.g. 'September 2026'."},
        "courseType": {"type": "string", "description": "'Postgraduate' or 'Undergraduate'."},
        "university": {"type": "string", "description": "Optional: a university name switches to course mode."},
        "programmeKeyword": {"type": "string", "description": "Optional subject/degree keyword to narrow courses."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["destinationCountry", "intake", "courseType"],
})
async def course_catalogue(inp: dict) -> dict:
    """The ONLY discovery tool. No `university` -> list eligible universities;
    with `university` -> that university's eligibility-checked courses. Returns
    DATA only (present_recommendations does the presenting)."""
    cat = _catalogue()
    limit = inp.get("limit")

    dest = _resolve(cat["countries"], _expand_alias(inp.get("destinationCountry", "")))
    if dest is None:
        return {"found": False,
                "reason": f'Couldn\'t match destination country "{inp.get("destinationCountry", "")}".',
                "options": [c["name"] for c in cat["countries"]][:40]}

    intake = _resolve(cat["intakes"], inp.get("intake", ""))
    if intake is None:
        return {"found": False,
                "reason": f'Couldn\'t match intake "{inp.get("intake", "")}" for {dest["name"]}.',
                "options": [i["name"] for i in cat["intakes"]][:40]}

    ctype = _resolve(cat["courseTypes"], inp.get("courseType", ""))
    course_type = ctype["name"] if ctype else inp.get("courseType", "")

    unis = [u for u in cat["universities"]
            if u["country"] == dest["id"]
            and intake["id"] in u.get("intakes", [])
            and course_type in u.get("courseTypes", [])]

    if not inp.get("university"):
        named = [{"universityId": u["universityId"], "university": u["university"],
                  "currencyCode": u.get("currencyCode"), "rating": u.get("rating")}
                 for u in unis]
        return {"found": True, "source": "crizac-catalogue", "destination": dest["name"],
                "intake": intake["name"], "courseType": course_type, "count": len(named),
                "universities": named[:(limit or 40)],
                "hint": "Pass a university name to get its eligibility-checked courses."}

    matched = _resolve([{"id": u["universityId"], "name": u["university"]} for u in unis],
                       inp["university"])
    if matched is None:
        return {"found": False,
                "reason": f'"{inp["university"]}" isn\'t offering {course_type} courses for '
                          f'{intake["name"]} in {dest["name"]} (per the contracted catalogue).',
                "universities": [u["university"] for u in unis][:30]}

    uni = next(u for u in unis if u["universityId"] == matched["id"])
    requested_degree = _degree_family(inp.get("programmeKeyword", ""))
    eligible = [c for c in uni["courses"]
                if c.get("isAppliable", True)
                and _degree_matches(requested_degree, c["name"])]

    terms = _subject_terms(inp.get("programmeKeyword", ""))
    if terms:
        narrowed = [c for c in eligible if any(t in c["name"].lower() for t in terms)]
        courses = narrowed or eligible  # never narrow to nothing
    else:
        courses = eligible

    out = {"found": True, "source": "crizac-catalogue", "destination": dest["name"],
           "intake": intake["name"], "courseType": course_type, "university": uni["university"],
           "count": len(courses), "courses": courses[:(limit or 25)]}
    if requested_degree:
        out["requestedDegree"] = requested_degree
    return out


@agent.tool("present_recommendations", input_schema={
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array", "minItems": 1, "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "universityName": {"type": "string"},
                    "programmeName": {"type": "string"},
                    "country": {"type": "string"},
                    "annualTuition": {"type": "number"},
                    "currencySymbol": {"type": "string", "description": "Defaults to £"},
                    "durationMonths": {"type": "number"},
                    "city": {"type": "string"},
                    "applicationFee": {"type": "number"},
                    "intake": {"type": "string"},
                    "fit": {"type": "string", "enum": ["ambitious", "target", "safe"]},
                    "rationale": {"type": "string"},
                },
                "required": ["universityName", "programmeName"],
            },
        }
    },
    "required": ["recommendations"],
})
async def present_recommendations(inp: dict) -> dict:
    """Pure shaper (no backend call): validate the ranked list's SHAPE and echo it
    back. The event handler turns this into a `recommendations` card. Shape is
    unbypassable here — only well-formed items (universityName + programmeName)
    survive."""
    items = inp.get("recommendations") or []
    clean = [r for r in items
             if isinstance(r, dict) and r.get("universityName") and r.get("programmeName")]
    return {"ok": True, "count": len(clean), "recommendations": clean}


# ---------------------------------------------------------------------------
# CAMS lookups — read the student store. cams_lookup_student returns the master
# id and the numeric CAMS id(s) SEPARATELY (never the master id masquerading as
# the CAMS id — the production bug that told a counsellor the CAMS id was
# "KGOr1760073259"); A2 then scrubs the master id at the tool boundary, so only
# the numeric CAMS id survives into the model and the reply. Port of
# lib/tools/impl/cams-lookup.ts.
# ---------------------------------------------------------------------------
@agent.tool("cams_lookup_student", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "CAMS ID (numeric) or Crizac masterId, if known."},
        "name": {"type": "string", "description": "Student's full name, when the CAMS ID is unknown."},
    },
    "required": [],
})
async def cams_lookup_student(inp: dict) -> dict:
    """Resolve a student by CAMS id / masterId / name. Returns ``masterId`` and
    ``camsIds`` as separate fields — report ONLY the numeric camsIds. (The master
    id is redacted to `(id hidden)` by the A2 guard before you ever see it.)"""
    query = inp.get("camsId") or inp.get("name") or ""
    mid, student = _find_student(query)
    if student is None:
        which = f'name "{inp["name"]}"' if inp.get("name") else f'CAMS ID {inp.get("camsId")}'
        return {"found": False, "reason": f"No student found for {which}."}
    matched_by = "name" if (inp.get("name") and not inp.get("camsId")) else "camsId"
    # `student` intentionally excludes the raw masterId; the numeric camsIds are
    # the safe id. (A2 scrubs the masterId field below regardless.)
    safe = {k: v for k, v in student.items() if k not in ("masterId", "duplicates")}
    return {"found": True, "source": "crizac", "matchedBy": matched_by,
            "masterId": mid, "camsIds": _cams_ids(student), "student": safe}


@agent.tool("duplicates_check", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "CAMS ID / masterId of the student to check."},
        "matchKeys": {"type": "array", "items": {"type": "string", "enum": ["passport", "email", "mobile"]}},
    },
    "required": ["camsId"],
})
async def duplicates_check(inp: dict) -> dict:
    """Check whether other Crizac sub-agents are also handling this student.
    Returns last-touch info so the counsellor avoids wasted effort. Port of
    lib/tools/impl/duplicates.ts (local store stands in for the cross-agency API)."""
    mid, student = _find_student(inp.get("camsId", ""))
    if student is None:
        return {"camsId": inp.get("camsId"), "hasDuplicates": False, "duplicates": [],
                "source": "skipped",
                "reason": "No student on file for that id — the duplicate check runs once the "
                          "lead is filed in CAMS."}
    dups = student.get("duplicates", []) or []
    return {"camsId": _surface_cams(inp.get("camsId"), student),
            "passportChecked": bool(student.get("passport")),
            "hasDuplicates": bool(dups), "duplicates": dups, "source": "local"}


@agent.tool("application_activity", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "Student CAMS ID / masterId."},
        "applicationId": {"type": "string", "description": "Crizac application id, if known."},
    },
    "required": [],
})
async def application_activity(inp: dict) -> dict:
    """Status-change history + counsellor comments for a student's application, as
    a timeline. Port of lib/tools/impl/activity.ts (deterministic local timeline)."""
    mid, student = _find_student(inp.get("camsId") or inp.get("applicationId") or "")
    app = (student.get("applications") or [{}])[0] if student else {}
    who = student.get("name") if student else "student"
    timeline = [
        {"kind": "status", "label": f"Status: {app.get('status', 'Application Pending')}",
         "status": app.get("status", "Application Pending"), "at": "2026-07-21T09:00:00Z", "by": "system"},
        {"kind": "comment", "label": "Comment added",
         "comment": f"Profile reviewed for {who}.", "at": "2026-07-20T14:30:00Z", "by": "counsellor"},
        {"kind": "status", "label": "Status: Enquiry", "status": "Enquiry",
         "at": "2026-07-12T11:15:00Z", "by": "system"},
    ]
    return {"applicationId": inp.get("applicationId") or (app.get("camsId") if app else None),
            "camsId": _surface_cams(inp.get("camsId") or inp.get("applicationId"), student),
            "timeline": timeline, "source": "local"}


# ---------------------------------------------------------------------------
# Student workspace — mutate THIS student's record. camsId is server-side pinned
# in the manifest (pin: {camsId: event.payload.camsId}), so the value the handler
# receives is always the session's student — the model cannot target another.
# Ports of lib/tools/impl/{shortlist,compare}.ts + profile-form.ts.
# ---------------------------------------------------------------------------
# Fields the enrichment form tracks (lib/profile-fields.ts PROFILE_FIELDS).
_PROFILE_FIELDS = [
    ("englishWaiverNeeded", "English waiver needed?"),
    ("visaHistory", "Visa history"),
    ("budgetCapUsd", "Budget cap (USD)"),
    ("budgetCovers", "Budget covers"),
    ("interviewWaiverNeeded", "Interview waiver?"),
    ("backlogs", "Backlogs?"),
    ("gapYears", "Gap years"),
    ("intakeUrgency", "Intake urgency"),
    ("brandConscious", "Brand-conscious?"),
    ("academicPercentage", "Academic percentage"),
    ("preferredCity", "Preferred city"),
    ("specificCourse", "Specific course / module"),
    ("preferredCountries", "Preferred countries"),
    ("testsTaken", "Entrance tests taken"),
]


def _missing_profile_fields(profile: dict) -> list:
    out = []
    for key, label in _PROFILE_FIELDS:
        val = (profile or {}).get(key)
        if val is None or (isinstance(val, str) and not val.strip()) \
                or (isinstance(val, (list, tuple)) and not val):
            out.append(label)
    return out


@agent.tool("shortlist_add", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string"},
        "programmeId": {"type": "string", "description": "A programmeId from a course_catalogue result."},
        "fit": {"type": "string", "enum": ["ambitious", "target", "safe"]},
        "notes": {"type": "string"},
        "universityName": {"type": "string"},
        "programmeName": {"type": "string"},
    },
    "required": ["camsId", "programmeId", "fit"],
})
async def shortlist_add(inp: dict) -> dict:
    """Add a programme to THIS student's shortlist. Use ONLY when the counsellor
    explicitly asks — never auto-shortlist after a recommendation. programmeId
    must come from a course_catalogue result. Port of shortlist_add: rejects
    unknown ids, dedups, and enforces the 15-programme cap (MAX_SHORTLIST)."""
    cams_id = inp.get("camsId")
    mid, student = _find_student(cams_id or "")
    if student is None:
        return {"ok": False, "reason": "No student is loaded; create the lead in CAMS first."}

    prog = _programme_index().get(inp.get("programmeId", ""))
    if prog is None:
        return {"ok": False,
                "reason": f'Refusing to pin unrecognized programmeId "{inp.get("programmeId")}". '
                          "Use a real id from a course_catalogue result — do not invent one."}

    shortlist = student.setdefault("shortlist", [])
    if any(e["programmeId"] == prog["programmeId"] for e in shortlist):
        return {"ok": False, "reason": "Already on shortlist", "shortlistSize": len(shortlist)}
    if len(shortlist) >= MAX_SHORTLIST:
        return {"ok": False, "shortlistSize": len(shortlist),
                "reason": f"Shortlist is full ({MAX_SHORTLIST} max). Remove one before adding another."}

    entry = {
        "programmeId": prog["programmeId"],
        "universityId": prog["universityId"],
        "fit": inp.get("fit"),
        "universityName": inp.get("universityName") or prog["universityName"],
        "programmeName": inp.get("programmeName") or prog["name"],
        "annualTuition": prog.get("annualTuition"),
        "currencyCode": prog.get("currencyCode"),
    }
    if inp.get("notes"):
        entry["notes"] = inp["notes"]
    shortlist.append(entry)
    return {"ok": True, "shortlistSize": len(shortlist), "added": entry}


@agent.tool("shortlist_remove", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string"},
        "programmeId": {"type": "string"},
    },
    "required": ["camsId", "programmeId"],
})
async def shortlist_remove(inp: dict) -> dict:
    """Remove a programme from THIS student's shortlist. Port of shortlist_remove."""
    mid, student = _find_student(inp.get("camsId") or "")
    if student is None:
        return {"ok": False, "reason": "Student not found"}
    shortlist = student.get("shortlist", [])
    if not any(e["programmeId"] == inp.get("programmeId") for e in shortlist):
        return {"ok": False, "reason": "Programme not on shortlist"}
    student["shortlist"] = [e for e in shortlist if e["programmeId"] != inp.get("programmeId")]
    return {"ok": True, "shortlistSize": len(student["shortlist"])}


@agent.tool("compare_programmes", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string"},
        "programmeIds": {"type": "array", "items": {"type": "string"},
                         "description": "Subset to compare; defaults to the whole shortlist."},
    },
    "required": ["camsId"],
})
async def compare_programmes(inp: dict) -> dict:
    """Assemble THIS student's shortlist for the comparison PDF (Crizac's 21-field
    format). Use on comparison intent — never write an inline table. The event
    handler renders a Download-PDF card. Port of compare_programmes."""
    mid, student = _find_student(inp.get("camsId") or "")
    if student is None:
        return {"ok": False, "reason": "Student not found"}
    shortlist = student.get("shortlist", [])
    ids = inp.get("programmeIds") or []
    selected = [e for e in shortlist if e["programmeId"] in ids] if ids else shortlist
    if not selected:
        return {"ok": False,
                "reason": "No shortlisted programmes to compare. Shortlist a few first."}
    return {"ok": True, "camsId": _surface_cams(inp.get("camsId"), student), "count": len(selected),
            "universities": [f'{e.get("universityName", e["universityId"])} - '
                             f'{e.get("programmeName", e["programmeId"])}' for e in selected],
            "programmeIds": [e["programmeId"] for e in selected]}


@agent.tool("collect_profile", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "CAMS ID; defaults to the active student."},
    },
    "required": [],
})
async def collect_profile(inp: dict) -> dict:
    """Surface the inline profile-enrichment form. READ-ONLY: returns the current
    values + which tracked fields are missing; the counsellor edits and saves it.
    Never guess or write values yourself. Port of collect_profile."""
    mid, student = _find_student(inp.get("camsId") or "")
    if student is None:
        return {"ok": False, "reason": "No student is loaded; ask the counsellor which student this is for."}
    profile = student.get("profile", {})
    return {"ok": True, "camsId": _surface_cams(inp.get("camsId"), student),
            "studentName": student.get("name"),
            "values": profile, "missing": _missing_profile_fields(profile)}


# ---------------------------------------------------------------------------
# Lead creation (Phase 3) — the first WRITE into "CAMS". Three production
# guarantees become runtime primitives here:
#
#  * A1 retry — create_lead declares `retry: {on: [timeout, 5xx]}`; the runtime
#    re-invokes it on a transient upstream failure (prod retried once on 5xx).
#  * passport-idempotency — the handler is keyed by passport, so a retry after a
#    *timeout-then-success* (lead committed upstream, response lost) finds the
#    passport already on file and returns it: the retry can NEVER double-create.
#  * A1 self-heal — an unrecognised destination / course type / home state raises
#    RyaRecoverableToolError; the @agent.repair callback snaps the field to the
#    closest valid value (prod's "closest valid destination / spelling fix") and
#    the runtime retries once. Journaled as a `tool.repair` step.
#  * A5 adoption — on success the manifest's `adopt: {camsId: student_state.camsId}`
#    records the new numeric camsId as the session's active student, so the pinned
#    workspace tools target it for the rest of the turn (order-independent).
#
# Local store stands in for Crizac's lead API (swap for a `url:` tool in Phase 5,
# where the per-counsellor login credential + reconnect-on-401 also land).
# ---------------------------------------------------------------------------
# Known Indian home states, for the spelling-repair path (condensed from the prod
# home-state list). A close but misspelled value is snapped to the nearest here.
_HOME_STATES = [
    "Andhra Pradesh", "Bihar", "Delhi", "Gujarat", "Haryana", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Punjab", "Rajasthan",
    "Tamil Nadu", "Telangana", "Uttar Pradesh", "West Bengal",
]

# Deterministic numeric CAMS-id minting for newly created leads (numeric — the
# only id ever surfaced). A list cell so it is mutable at module scope; reset on
# every fresh module load (so tests never collide).
_LEAD_SEQ = [0]

# Deterministic fault-injection seam for the idempotency gate: set
# _FAULTS["create_lead_timeout_after_write"] = 1 to make the NEXT fresh create
# commit the record and then raise E_TIMEOUT — standing in for "Crizac created
# the lead but the HTTP response was lost". The declared retry then re-invokes
# create_lead, which must return the existing lead (no duplicate). Never set in
# production paths; only the Phase-3 gate touches it.
_FAULTS = {"create_lead_timeout_after_write": 0}


def _country_names() -> list:
    return [c["name"] for c in _catalogue()["countries"]]


def _course_type_names() -> list:
    return [c["name"] for c in _catalogue()["courseTypes"]]


def _closest_name(value: str, options: list):
    """Closest valid option for a near-miss value (the self-heal core). Returns
    None when nothing is close enough, so the repair can fall back to a default."""
    if not value:
        return None
    hit = difflib.get_close_matches(value.strip(), options, n=1, cutoff=0.6)
    return hit[0] if hit else None


def _find_by_passport(passport: str):
    p = (passport or "").strip().upper()
    if not p:
        return None, None
    for mid, s in _STUDENTS.items():
        if (s.get("passport") or "").strip().upper() == p:
            return mid, s
    return None, None


def _mint_cams_id() -> str:
    _LEAD_SEQ[0] += 1
    return str(1900000 + _LEAD_SEQ[0])


@agent.tool("create_lead", input_schema={
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Student's full name."},
        "passport": {"type": "string", "description": "Passport number — the idempotency key."},
        "email": {"type": "string"},
        "mobile": {"type": "string"},
        "homeState": {"type": "string", "description": "Home state (India)."},
        "destinationCountry": {"type": "string", "description": "Destination country."},
        "courseType": {"type": "string", "description": "'Postgraduate' or 'Undergraduate'."},
        "intake": {"type": "string"},
        "target": {"type": "string", "description": "Target programme, if known."},
    },
    "required": ["name", "passport", "destinationCountry", "courseType"],
})
async def create_lead(inp: dict) -> dict:
    """Create a student lead in CAMS. Idempotent by passport (a re-run never
    double-creates). Raises a recoverable error for an unrecognised destination /
    course type / home state so the @agent.repair callback can snap it to the
    closest valid value and the runtime can retry. Port of lib/tools/impl/
    create-lead.ts (retry + closest-match self-heal + passportAlreadyExists)."""
    passport = (inp.get("passport") or "").strip()
    # Idempotency FIRST: if this passport is already on file, return it unchanged.
    # This is what makes a timeout-then-success retry safe (passportAlreadyExists).
    mid, existing = _find_by_passport(passport)
    if existing is not None:
        cams = _cams_ids(existing)
        return {"ok": True, "alreadyExists": True, "camsId": cams[0] if cams else None,
                "name": existing.get("name"), "reason": "passportAlreadyExists"}

    # Validate against the catalogue; a near-miss is RECOVERABLE (repair heals it).
    dest = _resolve(_catalogue()["countries"], _expand_alias(inp.get("destinationCountry", "")))
    if dest is None:
        raise RyaRecoverableToolError(
            "invalid_destination",
            f'Unrecognised destination "{inp.get("destinationCountry")}".',
            detail={"value": inp.get("destinationCountry"), "options": _country_names()})
    ctype = _resolve(_catalogue()["courseTypes"], inp.get("courseType", ""))
    if ctype is None:
        raise RyaRecoverableToolError(
            "invalid_course_type",
            f'Unrecognised course type "{inp.get("courseType")}".',
            detail={"value": inp.get("courseType"), "options": _course_type_names()})
    home = inp.get("homeState")
    if home and home not in _HOME_STATES:
        raise RyaRecoverableToolError(
            "invalid_home_state", f'Unrecognised home state "{home}".',
            detail={"value": home})

    # Persist the new lead (numeric camsId; internal alphanumeric masterId never
    # surfaced), THEN honour a simulated lost-response fault so the retry path is
    # exercised end-to-end. The record is already committed, so the retry finds
    # the passport and returns it — no duplicate.
    cams_id = _mint_cams_id()
    new_mid = f"M{cams_id}"
    _STUDENTS[new_mid] = {
        "masterId": new_mid, "name": inp.get("name"), "passport": passport,
        "email": inp.get("email"), "mobile": inp.get("mobile"),
        "destination": dest["name"], "target": inp.get("target"),
        "applications": [{"camsId": cams_id, "programme": inp.get("target"),
                          "university": None, "status": "Enquiry"}],
        "shortlist": [], "profile": {}, "duplicates": [],
    }
    if _FAULTS.get("create_lead_timeout_after_write", 0) > 0:
        _FAULTS["create_lead_timeout_after_write"] -= 1
        from rya import RyaError
        raise RyaError("E_TIMEOUT", "upstream lead API timed out after commit",
                       hint="Retry is safe — the lead is idempotent by passport.")
    return {"ok": True, "created": True, "camsId": cams_id, "name": inp.get("name"),
            "destination": dest["name"], "courseType": ctype["name"]}


@agent.repair("create_lead")
def repair_create_lead(inp: dict, err) -> dict:
    """Self-heal a recoverable create_lead failure by snapping the offending field
    to the closest valid value (mirrors prod's closest-destination / course-type /
    home-state-spelling repair), then the runtime retries once with this input."""
    patched = dict(inp)
    if err.reason == "invalid_destination":
        patched["destinationCountry"] = _closest_name(inp.get("destinationCountry"),
                                                       _country_names()) or "United Kingdom"
    elif err.reason == "invalid_course_type":
        patched["courseType"] = _closest_name(inp.get("courseType"),
                                              _course_type_names()) or "Postgraduate"
    elif err.reason == "invalid_home_state":
        fixed = _closest_name(inp.get("homeState"), _HOME_STATES)
        if fixed:
            patched["homeState"] = fixed
        else:
            patched.pop("homeState", None)  # unknown state → drop rather than block
    return patched


@agent.tool("create_leads_bulk", input_schema={
    "type": "object",
    "properties": {
        "leads": {"type": "array", "minItems": 1, "maxItems": 50,
                  "items": {"type": "object"}},
    },
    "required": ["leads"],
})
async def create_leads_bulk(inp: dict) -> dict:
    """Create several leads at once. Each is passport-idempotent (an already-filed
    passport is returned, not duplicated). Unlike create_lead, bulk does not
    self-heal per row — a row with an unrecognised destination is reported as
    skipped so the counsellor can fix and resubmit it."""
    out = []
    for lead in inp.get("leads") or []:
        passport = (lead.get("passport") or "").strip()
        mid, existing = _find_by_passport(passport)
        if existing is not None:
            cams = _cams_ids(existing)
            out.append({"ok": True, "alreadyExists": True, "passport": passport,
                        "camsId": cams[0] if cams else None})
            continue
        dest = _resolve(_catalogue()["countries"], _expand_alias(lead.get("destinationCountry", "")))
        if dest is None:
            out.append({"ok": False, "passport": passport, "reason": "invalid_destination"})
            continue
        cams_id = _mint_cams_id()
        new_mid = f"M{cams_id}"
        _STUDENTS[new_mid] = {
            "masterId": new_mid, "name": lead.get("name"), "passport": passport,
            "destination": dest["name"], "target": lead.get("target"),
            "applications": [{"camsId": cams_id, "status": "Enquiry"}],
            "shortlist": [], "profile": {}, "duplicates": [],
        }
        out.append({"ok": True, "created": True, "passport": passport, "camsId": cams_id})
    return {"ok": True, "count": sum(1 for r in out if r.get("ok")), "results": out}


# ---------------------------------------------------------------------------
# Phase 5 — prediction + drafting + the discovery tail. Read-only leaves over
# the local catalogue / curated seeds: production hits Crizac/Plexe with a
# local fallback, and offline we ARE that fallback (the manifest declares these
# `provider: crizac` + `url:` so a live deploy routes to Crizac; the handler
# wins locally, so offline stays deterministic — see MIGRATION §A3). Two
# exceptions: offer_prediction talks to Plexe via PLEXE_API_KEY and degrades
# gracefully when unset or the endpoint is down (prod's isPlexeConfigured +
# try/catch); draft_sop/draft_lor return the grounded student context for the
# compose model to write from (prod's context-only path — a leaf has no
# ctx.llm to call the doc_analysis route itself).
# ---------------------------------------------------------------------------
# Curated home countries for the lead / filters flows (prod: Crizac options).
_HOME_COUNTRIES = ["India", "Nepal", "Bangladesh", "Sri Lanka", "Pakistan", "Nigeria"]

# Curated student-visa essentials per destination (prod: lib/data/visa.ts). Keyed
# by lower-cased canonical country name (see _expand_alias). General guidance only.
_VISA = {
    "united kingdom": {
        "country": "United Kingdom", "visaType": "Student visa (formerly Tier 4)",
        "financialProof": "First-year tuition + 9 months' maintenance, held 28 consecutive days",
        "processingTime": "~3 weeks after biometrics",
        "documents": ["CAS from the university", "valid passport", "financial evidence",
                      "English proficiency (IELTS/equivalent)", "TB test (if applicable)"],
        "officialSource": "https://www.gov.uk/student-visa"},
    "united states": {
        "country": "United States", "visaType": "F-1 student visa",
        "financialProof": "Proof of funds for first year on the I-20",
        "processingTime": "Varies; book the consular interview early",
        "documents": ["Form I-20 from the SEVP school", "SEVIS fee receipt", "DS-160 confirmation",
                      "valid passport", "financial evidence"],
        "officialSource": "https://travel.state.gov/content/travel/en/us-visas/study.html"},
    "canada": {
        "country": "Canada", "visaType": "Study permit",
        "financialProof": "Tuition + CAD 20,635/yr living (GIC common)",
        "processingTime": "Varies by country; SDS is faster where eligible",
        "documents": ["letter of acceptance (DLI)", "proof of funds / GIC", "valid passport",
                      "medical exam (if required)"],
        "officialSource": "https://www.canada.ca/en/immigration-refugees-citizenship.html"},
    "australia": {
        "country": "Australia", "visaType": "Student visa (subclass 500)",
        "financialProof": "Tuition + living costs (currently ~AUD 29,710/yr)",
        "processingTime": "Varies; apply well before the intake",
        "documents": ["CoE (Confirmation of Enrolment)", "GTE statement", "OSHC health cover",
                      "valid passport", "financial evidence"],
        "officialSource": "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500"},
}

# Curated scholarships (prod: lib/data/scholarships-live with catalogue fallback).
_SCHOLARSHIPS = [
    {"id": "chevening", "name": "Chevening Scholarship", "university": None,
     "country": "United Kingdom", "amountUsd": None, "amountText": "Full tuition + stipend",
     "deadline": "November 2026"},
    {"id": "commonwealth-masters", "name": "Commonwealth Master's Scholarship", "university": None,
     "country": "United Kingdom", "amountUsd": None, "amountText": "Full funding",
     "deadline": "December 2026"},
    {"id": "gates-cambridge", "name": "Gates Cambridge Scholarship",
     "university": "University of Cambridge", "country": "United Kingdom", "amountUsd": None,
     "amountText": "Full cost of study", "deadline": "October 2026"},
    {"id": "fulbright", "name": "Fulbright Foreign Student Program", "university": None,
     "country": "United States", "amountUsd": None, "amountText": "Tuition + living stipend",
     "deadline": "May 2026"},
    {"id": "vanier", "name": "Vanier Canada Graduate Scholarship", "university": None,
     "country": "Canada", "amountUsd": 37000, "amountText": "CAD 50,000 / year",
     "deadline": "November 2026"},
    {"id": "australia-awards", "name": "Australia Awards", "university": None,
     "country": "Australia", "amountUsd": None, "amountText": "Full tuition + allowances",
     "deadline": "April 2026"},
]


def _country_name_by_id(cid: str) -> str:
    for c in _catalogue()["countries"]:
        if c.get("id") == cid:
            return c["name"]
    return cid


def _university_rows(unis: list, limit: int) -> list:
    return [{"universityId": u["universityId"], "name": u["university"],
             "country": _country_name_by_id(u["country"]), "rating": u.get("rating"),
             "courseCount": len(u.get("courses", []))} for u in unis][:limit]


@agent.tool("programme_detail", input_schema={
    "type": "object",
    "properties": {"programmeId": {"type": "string",
                                   "description": "A programmeId from a course_catalogue result."}},
    "required": ["programmeId"],
})
async def programme_detail(inp: dict) -> dict:
    """Full detail for one programme (tuition, duration, city, fee, eligibility).
    programmeId must come from a course_catalogue result. Port of programme_detail."""
    prog = _programme_index().get(inp.get("programmeId", ""))
    if prog is None:
        return {"found": False,
                "reason": f'No programme "{inp.get("programmeId")}" in the catalogue. '
                          "Use an id from a course_catalogue result."}
    return {"found": True, "source": "crizac-catalogue", "programme": prog}


@agent.tool("university_search", input_schema={
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Natural-language university search."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["query"],
})
async def university_search(inp: dict) -> dict:
    """Search universities by natural-language query (name, country, subject).
    Local catalogue fallback (prod uses the live Crizac catalogue when configured)."""
    limit = inp.get("limit") or 15
    unis = _catalogue()["universities"]
    terms = _subject_terms(inp.get("query", "")) + [
        t for t in re.split(r"[^a-z0-9]+", _expand_alias(inp.get("query", "")).lower())
        if len(t) >= 2]
    if terms:
        def hay(u):
            courses = " ".join(c["name"] for c in u.get("courses", []))
            return f'{u["university"]} {_country_name_by_id(u["country"])} {courses}'.lower()
        matched = [u for u in unis if any(t in hay(u) for t in terms)]
        unis = matched or unis
    return {"total": len(unis), "source": "local", "universities": _university_rows(unis, limit)}


@agent.tool("university_recommendations", input_schema={
    "type": "object",
    "properties": {
        "universityCountry": {"type": "string"},
        "courseLevel": {"type": "string", "description": "e.g. Postgraduate"},
        "courseName": {"type": "string", "description": "e.g. Computer Science"},
        "tuitionFeeMax": {"type": "number"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": [],
})
async def university_recommendations(inp: dict) -> dict:
    """Recommend universities for a student's preferences (country, level, subject,
    budget). Local catalogue fallback (prod uses the live Crizac recommender)."""
    cat = _catalogue()
    limit = inp.get("limit") or 15
    unis = cat["universities"]
    if inp.get("universityCountry"):
        dest = _resolve(cat["countries"], _expand_alias(inp["universityCountry"]))
        if dest:
            unis = [u for u in unis if u["country"] == dest["id"]]
    level = _resolve(cat["courseTypes"], inp.get("courseLevel", "")) if inp.get("courseLevel") else None
    if level:
        unis = [u for u in unis if level["name"] in u.get("courseTypes", [])]
    max_fee = inp.get("tuitionFeeMax")
    if max_fee:
        unis = [u for u in unis
                if any((c.get("annualTuition") or 0) <= max_fee for c in u.get("courses", []))]
    terms = _subject_terms(inp.get("courseName", ""))
    if terms:
        unis = [u for u in unis
                if any(any(t in c["name"].lower() for t in terms) for c in u.get("courses", []))] or unis
    return {"total": len(unis), "source": "local", "universities": _university_rows(unis, limit)}


@agent.tool("scholarship_search", input_schema={
    "type": "object",
    "properties": {
        "country": {"type": "string", "description": "Destination country, e.g. 'United Kingdom'."},
        "minAmountUsd": {"type": "number"},
    },
    "required": [],
})
async def scholarship_search(inp: dict) -> dict:
    """Find scholarships for a destination. Deduped by name; the event handler
    renders a `scholarships` card. Curated seed (prod reads the live catalogue with
    a static fallback). Port of scholarship_search."""
    rows = _SCHOLARSHIPS
    if inp.get("country"):
        want = _expand_alias(inp["country"]).lower()
        filtered = [s for s in rows if (s.get("country") or "").lower() == want]
        rows = filtered or rows
    if inp.get("minAmountUsd") is not None:
        rows = [s for s in rows if (s.get("amountUsd") or 0) >= inp["minAmountUsd"]]
    seen, deduped = set(), []
    for s in rows:
        if s["name"] not in seen:
            seen.add(s["name"])
            deduped.append(s)
    return {"count": len(deduped), "source": "local", "scholarships": deduped}


@agent.tool("crizac_filters", input_schema={
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["destination-countries", "home-countries", "intakes", "course-types"]},
        "universityCountry": {"type": "string", "description": "Destination country id (optional)."},
    },
    "required": ["kind"],
})
async def crizac_filters(inp: dict) -> dict:
    """List Crizac filter options (destination/home countries, intakes, course
    types) with their ids — use these ids when creating a lead. Port of
    crizac_filters (local catalogue stands in for the Crizac options endpoint)."""
    cat = _catalogue()
    kind = inp.get("kind")
    if kind == "destination-countries":
        opts = [{"id": c["id"], "name": c["name"]} for c in cat["countries"]]
    elif kind == "home-countries":
        opts = [{"id": None, "name": n} for n in _HOME_COUNTRIES]
    elif kind == "intakes":
        opts = [{"id": i["id"], "name": i["name"]} for i in cat["intakes"]]
    elif kind == "course-types":
        opts = [{"id": c["id"], "name": c["name"]} for c in cat["courseTypes"]]
    else:
        return {"ok": False, "reason": f'Unknown filter kind "{kind}".'}
    return {"ok": True, "kind": kind, "count": len(opts), "options": opts}


@agent.tool("visa_requirements", input_schema={
    "type": "object",
    "properties": {"country": {"type": "string", "description": "Destination country."}},
    "required": ["country"],
})
async def visa_requirements(inp: dict) -> dict:
    """Student-visa essentials for a destination (type, financial proof, processing
    time, documents, official source). Curated general guidance — always tell the
    student to verify against the official source. Port of visa_requirements."""
    info = _VISA.get(_expand_alias(inp.get("country", "")).lower())
    if not info:
        return {"found": False,
                "reason": f'No curated visa guidance for "{inp.get("country")}".',
                "availableCountries": [v["country"] for v in _VISA.values()]}
    return {"found": True, **info,
            "disclaimer": "General guidance only — verify current requirements against the "
                          "official government source before advising."}


@agent.tool("offer_prediction", input_schema={
    "type": "object",
    "properties": {"application": {"type": "object",
                                   "description": "Raw Crizac application fields (marks, english "
                                                  "score, course/university ids, intake, cost, "
                                                  "visa history, document flags)."}},
    "required": ["application"],
})
async def offer_prediction(inp: dict) -> dict:
    """Predict a student's offer probability via the Plexe Crizac XGBoost model.
    Read-only. Degrades gracefully: returns {ok: false, reason} when Plexe is not
    configured (PLEXE_API_KEY unset) or the endpoint is down — never raises. Port
    of offer_prediction (x-api-key auth, not a Bearer url: tool)."""
    import os
    key = os.environ.get("PLEXE_API_KEY")
    if not key:
        return {"ok": False, "reason": "Plexe is not configured (set PLEXE_API_KEY)."}
    url = "https://api.plexe.ai/predict/offer"
    try:
        import json as _json
        import urllib.request
        from rya.guard import check_egress
        check_egress(url, "POST")  # Action Guard — Plexe host must be allow-listed
        req = urllib.request.Request(
            url, data=_json.dumps({"application": inp.get("application") or {}}).encode(),
            method="POST", headers={"content-type": "application/json", "x-api-key": key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read().decode())
        if isinstance(result, dict) and result.get("error"):
            return {"ok": False, "reason": str(result["error"])}
        return {"ok": True, **(result if isinstance(result, dict) else {"result": result})}
    except Exception as err:  # intermittent endpoint / network — degrade, don't fail the turn
        return {"ok": False, "reason": str(err)}


def _student_context(student: dict) -> dict:
    """The facts an SOP/LOR must be grounded in (prod's studentContext)."""
    p = (student or {}).get("profile", {})
    return {"name": student.get("name"), "target": student.get("target"),
            "destination": student.get("destination"),
            "academicPercentage": p.get("academicPercentage"), "testsTaken": p.get("testsTaken"),
            "preferredCountries": p.get("preferredCountries"), "preferredCity": p.get("preferredCity")}


@agent.tool("draft_sop", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "Student CAMS ID."},
        "workExperience": {"type": "string"},
        "resumeSummary": {"type": "string"},
    },
    "required": ["camsId"],
})
async def draft_sop(inp: dict) -> dict:
    """Assemble the grounded student context for a Statement of Purpose. Returns the
    facts (no fabrication); the compose model writes the SOP from them. Port of
    draft_sop's context path (Crizac's SOP generator is the live-only path)."""
    _, student = _find_student(inp.get("camsId") or "")
    context = _student_context(student) if student else {"camsId": inp.get("camsId")}
    return {"ok": True, "source": "context",
            "note": "Write a tailored SOP grounded ONLY in `context` (do not invent achievements). "
                    "Cover motivation, academic background, why this programme/university, career "
                    "goals, and fit.",
            "context": context, "workExperience": inp.get("workExperience"),
            "resumeSummary": inp.get("resumeSummary")}


@agent.tool("draft_lor", input_schema={
    "type": "object",
    "properties": {
        "camsId": {"type": "string", "description": "Student CAMS ID."},
        "recommenderName": {"type": "string"},
        "recommenderTitle": {"type": "string", "description": "e.g. 'Professor of CS'"},
        "relationship": {"type": "string", "description": "How the recommender knows the student."},
    },
    "required": ["camsId"],
})
async def draft_lor(inp: dict) -> dict:
    """Assemble the grounded student context + recommender framing for a Letter of
    Recommendation. Returns the facts (no fabrication); the compose model writes the
    letter. Port of draft_lor (Crizac has no LOR generator — context only)."""
    _, student = _find_student(inp.get("camsId") or "")
    context = _student_context(student) if student else {"camsId": inp.get("camsId")}
    return {"ok": True, "source": "context",
            "note": "Write a formal LOR grounded ONLY in `context`. Use the recommender details if "
                    "provided; otherwise write from a generic academic referee. Do not fabricate "
                    "specific anecdotes — keep claims consistent with the facts.",
            "context": context,
            "recommender": {"name": inp.get("recommenderName"), "title": inp.get("recommenderTitle"),
                            "relationship": inp.get("relationship")}}


# ---------------------------------------------------------------------------
# Crizac login-mint (A3) — exchange a counsellor's credentials for a per-user
# bearer and store it as a connection. Called from the event handler (leaves have
# no ctx). Mirrors client.ts login() (/v2/api/agentAuth/login, device mode). The
# minted token is upserted for (crizac, identity.sub) so the live `url:` Crizac
# tools inject it; a later upstream 401 raises E_CONNECTION_EXPIRED → the run ends
# `needs_reconnect` (no silent refresh — the counsellor logs in again).
# ---------------------------------------------------------------------------
def _extract_crizac_token(payload):
    """extractToken order: data.token → data.accessToken → token → accessToken."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for cand in (data.get("token"), data.get("accessToken"),
                 payload.get("token"), payload.get("accessToken")):
        if cand:
            return str(cand)
    return None


def _crizac_login(base_url: str, basic_auth: str, email: str, password: str,
                  tenant_id: str = "1"):
    """POST the Crizac device-login; return (token, account_dict). Raises on a
    non-2xx or a missing token so the caller can surface the failure."""
    import json as _json
    import urllib.request
    from rya.guard import check_egress
    url = base_url.rstrip("/") + "/v2/api/agentAuth/login"
    check_egress(url, "POST")
    body = {"email": email, "password": password, "tenantId": tenant_id,
            "security": True, "trustDevice": True, "isCheckedTandC": True,
            "currentVersion": "0.0.14"}
    req = urllib.request.Request(url, data=_json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "Authorization": basic_auth})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = _json.loads(resp.read().decode())
    token = _extract_crizac_token(payload)
    if not token:
        raise RuntimeError("Crizac login returned no token (device not trusted?).")
    account = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return token, account


async def _maybe_mint_crizac(ctx, payload):
    """If the turn carries `crizacLogin: {email, password}` and Crizac is configured
    (CRIZAC_BASE_URL + CRIZAC_BASIC_AUTH), mint the per-counsellor bearer and upsert
    it as the (crizac, this-user) connection; stash agentId/counsellorNumber/tenantId
    in identity-scoped memory for lead-ownership stamping. Best-effort: a login
    failure is logged, never crashes the turn."""
    login = payload.get("crizacLogin")
    if not isinstance(login, dict):
        return
    base = ctx.secrets.get("CRIZAC_BASE_URL")
    basic = ctx.secrets.get("CRIZAC_BASIC_AUTH")
    tenant = ctx.secrets.get("CRIZAC_TENANT_ID") or "1"
    if not (base and basic):
        ctx.logs.warning("crizac login skipped: CRIZAC_BASE_URL / CRIZAC_BASIC_AUTH unset")
        return
    try:
        token, account = _crizac_login(base, basic, login.get("email"), login.get("password"), tenant)
        await ctx.connections.upsert("crizac", secret=token, scopes=["crm:read", "crm:write"],
                                     label="Crizac CAMS")
        await ctx.memory.set("agentId", str(account.get("agentId") or account.get("id") or ""),
                             scope="user")
        await ctx.memory.set("counsellorNumber", str(account.get("counsellorNumber") or ""),
                             scope="user")
        await ctx.memory.set("tenantId", tenant, scope="user")
        ctx.logs.info("crizac connection minted", provider="crizac")
    except Exception as e:
        ctx.logs.warning("crizac login failed", error=str(e))


# ---------------------------------------------------------------------------
# Gated writes (Phase 4) — the confirm-before-write actions. Each is declared
# `permission: approval_required` in the manifest, so the runtime blocks BOTH a
# direct ctx.tools.call (E_TOOL_PERMISSION_DENIED) AND loop exposure (they are
# absent from EXPOSED_TOOLS and ctx.llm.run never surfaces an approval-gated
# tool). They run through exactly ONE path: an approved `ctx.approvals.request`
# action, executed post-approval by engine._execute_action.
#
# Two production guarantees become runtime facts here:
#  * confirm-before-write — the run suspends durably until a human approves; the
#    write cannot fire inside the model turn.
#  * server-side keying — engine._execute_action does NOT re-apply the manifest
#    `pin:`, so the CAMS-targeting writes take the camsId the event handler
#    resolved from `memory.student_state.camsId` and BAKED into the action input
#    at request time (a camsId in the counsellor's payload sub-object is ignored;
#    see _gated_action). The model/counsellor can never redirect the write to
#    another student.
#
# All but send_email mutate the local CAMS store only (matches prod:
# cams_update_profile writes local student_state, never Crizac). Leaves (no ctx).
# ---------------------------------------------------------------------------
@agent.tool("cams_update_profile")
async def cams_update_profile(inp: dict) -> dict:
    """Write enrichment fields onto THIS student's local profile (prod writes
    student_state only, never Crizac). Gated: runs only via an approved action."""
    cams = inp.get("camsId")
    mid, student = _find_student(cams or "")
    if student is None:
        return {"ok": False, "reason": "No student loaded for that CAMS id."}
    updates = inp.get("updates") or {}
    profile = student.setdefault("profile", {})
    profile.update({k: v for k, v in updates.items() if v is not None})
    return {"ok": True, "camsId": _surface_cams(cams, student),
            "updated": sorted(updates), "values": profile}


@agent.tool("crizac_update_application")
async def crizac_update_application(inp: dict) -> dict:
    """Write application status / visa-history updates onto THIS student's record.
    Gated: runs only via an approved action. Port of crizac_update_application."""
    cams = inp.get("camsId")
    mid, student = _find_student(cams or "")
    if student is None:
        return {"ok": False, "reason": "No student loaded for that CAMS id."}
    app = (student.setdefault("applications", []) or [{}])[0] if student.get("applications") \
        else student.setdefault("applications", [{}])[0]
    if inp.get("status"):
        app["status"] = inp["status"]
    if inp.get("visaHistory") is not None:
        app["visaHistory"] = inp["visaHistory"]
    if inp.get("comment"):
        app.setdefault("comments", []).append(inp["comment"])
    return {"ok": True, "camsId": _surface_cams(cams, student),
            "status": app.get("status"), "applicationId": app.get("camsId")}


@agent.tool("submit_scholarship_enquiry")
async def submit_scholarship_enquiry(inp: dict) -> dict:
    """Register a scholarship enquiry on THIS student's behalf. Gated: runs only
    via an approved action. Port of submit_scholarship_enquiry."""
    cams = inp.get("camsId")
    mid, student = _find_student(cams or "")
    if student is None:
        return {"ok": False, "reason": "No student loaded for that CAMS id."}
    enquiries = student.setdefault("scholarshipEnquiries", [])
    enquiries.append({"scholarship": inp.get("scholarship"), "note": inp.get("note"),
                      "status": "submitted"})
    return {"ok": True, "camsId": _surface_cams(cams, student),
            "scholarship": inp.get("scholarship"), "enquiryCount": len(enquiries)}


@agent.tool("loan_apply_for_student")
async def loan_apply_for_student(inp: dict) -> dict:
    """Start a loan application for THIS student. Gated: runs only via an approved
    action, and the event handler runs the grounding gate on the composed reply
    BEFORE requesting approval (a money figure the model invented never reaches a
    loan write). Port of loan_apply_for_student."""
    cams = inp.get("camsId")
    mid, student = _find_student(cams or "")
    if student is None:
        return {"ok": False, "reason": "No student loaded for that CAMS id."}
    student["loan"] = {"amount": inp.get("amount"), "currency": inp.get("currency", "GBP"),
                       "lender": inp.get("lender"), "status": "submitted"}
    return {"ok": True, "camsId": _surface_cams(cams, student),
            "amount": inp.get("amount"), "currency": inp.get("currency", "GBP"),
            "status": "submitted"}


@agent.tool("send_email")
async def send_email(inp: dict) -> dict:
    """The one real outbound. Gated: runs ONLY via an approved action (never
    exposed to the loop, never a direct ctx.tools.call). Delivers through the
    email channel seam — real Resend when RESEND_API_KEY is set, otherwise the
    runtime's mock outbox (recorded in the trace). Mirrors prod's sendAgentEmail
    behind the confirm gate (a real send, not a stub)."""
    if not inp.get("to"):
        return {"ok": False, "reason": "no recipient"}
    from rya.providers.channels import send as channel_send
    receipt = channel_send("email", {"to": inp["to"], "subject": inp.get("subject", "(no subject)"),
                                     "body": inp.get("body", "")})
    return {"ok": True, **receipt}


# The gated-write intents, detected from the turn's payload. Returns the single
# approval action to request (at most one write pauses a turn, mirroring prod's
# one-confirm-at-a-time convention), with the RESOLVED pinned camsId baked into
# the action input — never a camsId the payload sub-object carries, because
# engine._execute_action does not re-pin. `ground` marks a money write whose
# composed reply must pass the grounding gate before the approval is requested.
def _gated_action(payload: dict, reply_text: str, active_cams):
    name = payload.get("studentName") or "the student"
    if payload.get("sendEmail"):
        return {"tool": "send_email", "title": "Send email to student", "body": reply_text,
                "ground": False,
                "input": {"to": payload.get("studentEmail", "student@example.com"),
                          "subject": payload.get("emailSubject", "From your counsellor"),
                          "body": reply_text}}
    # Every remaining gated write targets a specific student — it needs a resolved
    # pinned camsId. Without one, there is nothing safe to write, so request nothing.
    if not active_cams:
        return None
    cams = str(active_cams)
    if payload.get("loanApply"):
        return {"tool": "loan_apply_for_student", "title": f"Start a loan application for {name}",
                "body": reply_text, "ground": True,
                "input": {"camsId": cams, "amount": payload.get("loanAmount"),
                          "currency": payload.get("loanCurrency", "GBP"),
                          "lender": payload.get("lender")}}
    if isinstance(payload.get("updateApplication"), dict):
        fields = {k: v for k, v in payload["updateApplication"].items() if k != "camsId"}
        return {"tool": "crizac_update_application", "title": f"Update {name}'s application in CAMS",
                "body": reply_text, "ground": False, "input": {"camsId": cams, **fields}}
    if isinstance(payload.get("scholarshipEnquiry"), dict):
        s = payload["scholarshipEnquiry"]
        return {"tool": "submit_scholarship_enquiry",
                "title": f"Submit a scholarship enquiry for {name}", "body": reply_text,
                "ground": False,
                "input": {"camsId": cams, "scholarship": s.get("scholarship"), "note": s.get("note")}}
    if isinstance(payload.get("updateProfile"), dict):
        updates = {k: v for k, v in payload["updateProfile"].items() if k != "camsId"}
        return {"tool": "cams_update_profile", "title": f"Update {name}'s profile in CAMS",
                "body": reply_text, "ground": False, "input": {"camsId": cams, "updates": updates}}
    return None


# Map a tool call the loop made -> a UI card frame (component, data). Production
# builds these client-side from tool_call_finish; here the event handler emits
# them post-loop. Extend as card-producing tools land in later phases.
def _card_for(call: dict):
    tool, result = call.get("tool"), call.get("result") or {}
    if not isinstance(result, dict):
        return None
    if tool == "present_recommendations" and result.get("ok"):
        return "recommendations", {"items": result.get("recommendations", [])}
    if tool == "scholarship_search" and result.get("scholarships"):
        return "scholarships", {"scholarships": result.get("scholarships", []),
                                "count": result.get("count")}
    if tool == "duplicates_check" and result.get("source") != "skipped":
        return "duplicates", {"camsId": result.get("camsId"),
                              "hasDuplicates": result.get("hasDuplicates"),
                              "duplicates": result.get("duplicates", [])}
    if tool == "compare_programmes" and result.get("ok"):
        return "comparison", {"camsId": result.get("camsId"),
                              "universities": result.get("universities", []),
                              "programmeIds": result.get("programmeIds", [])}
    if tool == "collect_profile" and result.get("ok"):
        return "profileForm", {"camsId": result.get("camsId"),
                               "studentName": result.get("studentName"),
                               "values": result.get("values", {}),
                               "missing": result.get("missing", [])}
    # A successful shortlist mutation refreshes the student workspace pane
    # (decision #3: student_refresh is a UI frame, not a tool).
    if tool in ("shortlist_add", "shortlist_remove") and result.get("ok"):
        return "student_refresh", {"reason": tool, "shortlistSize": result.get("shortlistSize")}
    # A created (or adopted) lead loads that student into the workspace.
    if tool == "create_lead" and result.get("ok"):
        return "student_refresh", {"reason": "create_lead", "camsId": result.get("camsId"),
                                   "alreadyExists": bool(result.get("alreadyExists"))}
    return None


def _loaded_student(payload: dict) -> dict:
    """Build the system-prompt profile block for the session's student. An explicit
    ``student`` dict wins; otherwise resolve the pinned ``camsId`` against the CAMS
    store so the loaded-student block matches what the workspace tools will target."""
    if payload.get("student"):
        return payload["student"]
    cams_id = payload.get("camsId")
    if not cams_id:
        return {}
    _, student = _find_student(cams_id)
    if student is None:
        return {}
    return {"name": student.get("name"), "camsIds": _cams_ids(student),
            "destination": student.get("destination"), "target": student.get("target")}


@agent.on_event
async def handle_event(ctx, event):
    channel = event.payload.get("channel", "web")
    counsellor = event.payload.get("externalId") or event.payload.get("email") or "counsellor"
    body = event.payload.get("body") or "(empty message)"

    session = await ctx.sessions.get_or_create(channel, counsellor, title=counsellor)
    await ctx.sessions.append(session["id"], "user", body)

    # A3 login-mint: if the counsellor logged in this turn, exchange credentials for
    # a per-user Crizac bearer and upsert it as the (crizac, this-user) connection,
    # so the live `url:` Crizac tools inject the right counsellor's token. Best-effort.
    await _maybe_mint_crizac(ctx, event.payload)

    # A5 adoption seed: the session's active student lives in student_state.camsId
    # — the single source the pinned workspace tools resolve. Seed it from the
    # event payload; create_lead's `adopt:` overrides it when a new lead is filed,
    # so the pins always target the right student regardless of call order.
    if event.payload.get("camsId"):
        await ctx.memory.set("camsId", str(event.payload["camsId"]), scope="student_state")

    memory = await ctx.memory.search("student_facts", body, limit=8) if body else []
    system = build_system_prompt(student=_loaded_student(event.payload), memory=memory)
    await ctx.memory.block_set("persona", system)

    # The governed model loop: the model reasons and calls the wired tools; each
    # call is routed through ctx.tools.call, so permissions, pins, the Action
    # Guard, and the A2 id-secrecy scrub all apply. Mirrors prod's 6-turn cap.
    result = await ctx.llm.run(input={"message": body}, system=system,
                               tools=EXPOSED_TOOLS, max_steps=6)

    # Cards from the calls the loop made (prod: client-side from tool_call_finish).
    for call in result.get("toolCalls", []):
        card = _card_for(call)
        if card:
            ctx.emit_ui(card[0], card[1])

    reply_text = result.get("text", "")
    await ctx.sessions.append(session["id"], "assistant", reply_text)

    # Sidecar fact extraction (production: Haiku post-turn memory extraction).
    facts = await ctx.llm.respond(
        system="Extract at most one durable fact about the student from this exchange. "
               "Reply with the fact as one short sentence, or 'none'.",
        input={"message": body}, route="extract")
    if facts.text.strip().lower() not in ("none", ""):
        await ctx.memory.append("student_facts", {"fact": facts.text.strip(),
                                                   "source": counsellor})

    # Gated writes are approval-gated BY THE RUNTIME (production: preview ->
    # confirm prompt convention). At most one write pauses a turn. The action
    # input carries the RESOLVED pinned camsId (student_state.camsId — what the
    # workspace tools target), baked in here because engine._execute_action does
    # NOT re-apply the manifest pin. The run then pauses durably until a human
    # decides; on approval the write executes with full governance.
    active_cams = await ctx.memory.get("camsId", scope="student_state")
    gate = _gated_action(event.payload, reply_text, active_cams)
    if gate:
        # A loan is a money write: the composed reply must clear the grounding
        # gate first (no model-invented figure reaches a loan). Hard stop, not a
        # warning — mirrors loan-renewal's compose_report grounding check.
        if gate["ground"]:
            verdict = ctx.guard.check_grounding(gate["body"])
            if not verdict["ok"]:
                ctx.logs.error("gated write blocked: ungrounded money figure",
                               tool=gate["tool"], violations=verdict["violations"])
                return {"session": session["id"], "reply": reply_text, "blocked": "grounding",
                        "violations": verdict["violations"],
                        "toolCalls": [c.get("tool") for c in result.get("toolCalls", [])]}
        await ctx.approvals.request(title=gate["title"], body=gate["body"],
                                    action={"tool": gate["tool"], "input": gate["input"]})
        ctx.logs.info("gated write approved and executed", tool=gate["tool"])

    return {"session": session["id"], "reply": reply_text,
            "toolCalls": [c.get("tool") for c in result.get("toolCalls", [])]}
