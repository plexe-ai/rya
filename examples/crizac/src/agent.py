"""Crizac agent runtime - the durable handler behind the fleet.

One runtime, many agents-as-workspaces (counsellor, student, insights,
visa & CAS, university, finance). The handler routes each message to the
matching flow; side-effectful actions (filing an application, prefiling a
loan) always pause for human approval. Tools are leaves over demo data -
each swaps for an HTTP tool (`url:` in the manifest) against CAMS or a
university API without touching this file.
"""

import json
from pathlib import Path

from rya import define_agent

agent = define_agent()

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_db.json"

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["search_courses", "ask_data", "screen_visa",
                            "file_application", "prefile_loan", "other"]},
        "studentId": {"type": ["string", "null"]},
        "query": {"type": ["string", "null"]},
    },
    "required": ["action"],
}


def _db() -> dict:
    return json.loads(DATA.read_text()) if DATA.is_file() else {"students": {}, "courses": []}


# ---- tools (demo leaves; production swaps these for url: tools) ------------
@agent.tool("cams.lookup_student")
async def cams_lookup(input: dict) -> dict:
    s = _db()["students"].get(input["studentId"])
    return {"found": s is not None, "student": s}


@agent.tool("courses.search")
async def courses_search(input: dict) -> dict:
    q = (input.get("query") or "").lower()
    hits = [c for c in _db()["courses"]
            if q in c["name"].lower() or q in c["university"].lower()
            or (input.get("country") or "").lower() == c["country"].lower()]
    return {"count": len(hits), "courses": hits[:5]}


@agent.tool("data.query")
async def data_query(input: dict) -> dict:
    db = _db()
    return {"question": input["question"],
            "totals": {"students": len(db["students"]), "courses": len(db["courses"]),
                       "applicationsThisMonth": db.get("applicationsThisMonth", 0)}}


@agent.tool("visa.genuineness_check")
async def visa_check(input: dict) -> dict:
    s = _db()["students"].get(input["studentId"]) or {}
    flags = [f for f in [
        None if s.get("fundsVerified") else "funds not verified",
        None if s.get("gapYears", 0) <= 2 else "study gap over 2 years",
    ] if f]
    return {"studentId": input["studentId"], "flags": flags,
            "recommendation": "clear" if not flags else "review"}


@agent.tool("application.file")
async def application_file(input: dict) -> dict:
    return {"filed": True, "applicationId": f"APP-{input.get('studentId', 'X')}-{input.get('courseId', '0')}"}


@agent.tool("loan.prefile")
async def loan_prefile(input: dict) -> dict:
    return {"prefiled": True, "lender": input.get("lender", "partner"),
            "reference": f"LN-{input.get('studentId', 'X')}"}


# ---- the flow --------------------------------------------------------------
@agent.on_event
async def main(ctx, event):
    text = event.payload.get("text") or ""
    intent = (await ctx.llm.respond(
        route="intent", schema=INTENT_SCHEMA,
        system="Classify this request from Crizac's education platform. "
               "file_application and prefile_loan only when explicitly asked.",
        input={"message": text},
    )).json

    action = intent["action"]
    if action == "search_courses":
        res = await ctx.tools.call("courses.search", {"query": intent.get("query") or text})
        return {"reply": f"{res['count']} matching courses.", "courses": res["courses"]}

    if action == "ask_data":
        res = await ctx.tools.call("data.query", {"question": text})
        return {"reply": "Here is what CAMS shows.", "data": res["totals"]}

    if action == "screen_visa" and intent.get("studentId"):
        res = await ctx.tools.call("visa.genuineness_check", {"studentId": intent["studentId"]})
        return {"reply": f"Genuineness screening: {res['recommendation']}.", "flags": res["flags"]}

    if action == "file_application" and intent.get("studentId"):
        student = await ctx.tools.call("cams.lookup_student", {"studentId": intent["studentId"]})
        if not student["found"]:
            return {"reply": f"Student {intent['studentId']} is not in CAMS."}
        await ctx.approvals.request(
            title=f"File application - student {intent['studentId']}",
            body=f"Request: {text}\nStudent: {json.dumps(student['student'])[:500]}",
            action={"tool": "application.file",
                    "input": {"studentId": intent["studentId"], "courseId": intent.get("query") or "unspecified"}},
        )
        return {"reply": "Application prepared - waiting for counsellor approval."}

    if action == "prefile_loan" and intent.get("studentId"):
        await ctx.approvals.request(
            title=f"Prefile education loan - student {intent['studentId']}",
            body=f"Request: {text}",
            action={"tool": "loan.prefile", "input": {"studentId": intent["studentId"]}},
        )
        return {"reply": "Loan prefile prepared - waiting for approval."}

    return {"reply": "I can search courses, answer CAMS data questions, screen a visa case, "
                     "or file an application (with approval)."}
