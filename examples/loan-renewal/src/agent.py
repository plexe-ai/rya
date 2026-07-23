"""Loan Application renewal orchestrator - see rya.agent.yaml for the manifest.

Pipeline (the eight steps from the BBG spec, as durable Rya primitives):

1. message.received  -> intent parse (Bedrock haiku route) + CIF resolution
2. checklist         -> the case asks for AECB, spread, IDs, reference report
3. file.uploaded     -> checklist updated; when complete, extraction fans out
4. extract_document  -> one retryable job per document (Bedrock PDF blocks)
5. derive schema     -> the reference report defines which fields matter
6. filter            -> deterministic subset of the big JSON (no model)
7. compose_report    -> cited report; grounding gate blocks unsourced figures
8. approval          -> human signs; only then la.update_record writes the DB

Tools are leaves over data/bank_db.json - swap each for an HTTP tool (url: in
the manifest) to hit the real archive/LA systems without touching this file.
"""

import json
from pathlib import Path

from rya import define_agent

agent = define_agent()

# Seed archive ships in the repo (data/bank_db.json "archive" section).
SEED_DB = Path(__file__).resolve().parent.parent / "data" / "bank_db.json"
REQUIRED_DOCS = ["aecb", "spread", "id", "reference_report"]

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["start_renewal", "status", "other"]},
        "cif": {"type": "string", "description": "CIF number mentioned, or empty"},
        "product": {"type": "string", "enum": ["LA", "RBL", "TWC"]},
    },
    "required": ["action", "cif", "product"],
}

# Fixed field names per docType: independent chunk calls MUST share keys or
# the provenance merge can't align them (found live: chunk 0 said
# annual_revenue, chunk 1 said annual_revenue_aed - conflict undetected).
def _fields(*names):
    return {"type": "object",
            "properties": {n: {} for n in names},
            "required": list(names)}

EXTRACTION_SCHEMAS = {
    "aecb": _fields("credit_score", "total_outstanding_liabilities_aed",
                    "monthly_obligations_aed", "active_facilities", "defaults",
                    "bounced_cheques", "worst_delinquency_bucket"),
    "spread": _fields("annual_revenue_aed", "net_profit_aed", "total_assets_aed",
                      "total_liabilities_aed", "current_ratio", "leverage_ratio"),
    "id": _fields("legal_entity_name", "trade_license_number",
                  "license_expiry_date", "authorized_signatories"),
}

EXTRACTION_PROMPTS = {
    "aecb": "Extract from this AECB credit bureau report: credit score, total outstanding "
            "liabilities (AED), monthly obligations (AED), number of active facilities, "
            "any defaults or bounced cheques, and worst delinquency bucket.",
    "spread": "Extract from this financial spread: annual revenue (AED), net profit (AED), "
              "total assets (AED), total liabilities (AED), current ratio, and leverage ratio.",
    "id": "Extract from this identity document: legal entity name, trade license number, "
          "license expiry date, and authorized signatories.",
}


# ---- bank-system store: real SQL (Postgres in prod, SQLite locally) --------
# Domain data - archive customers, renewal cases, extractions, final reports -
# lives in proper tables: RYA_DATABASE_URL (the same RDS as the runtime) when
# present, else a local SQLite file. Durable, queryable, survives restarts.
# Swap any tool for a `url:` HTTP tool to hit the bank's real systems instead.
import os
import sqlite3

DB_URL = os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL")
SQLITE_PATH = SEED_DB.with_name("bank.sqlite3")
_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS la_archive (cif TEXT PRIMARY KEY, data TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS la_renewals (case_id TEXT PRIMARY KEY, cif TEXT NOT NULL,
       product TEXT, status TEXT, final_report TEXT, data_summary TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS la_cases (case_id TEXT PRIMARY KEY, cif TEXT NOT NULL,
       extractions TEXT DEFAULT '{}', report_schema TEXT,
       compose_scheduled INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS la_extractions (case_id TEXT NOT NULL, doc_type TEXT NOT NULL,
       data TEXT NOT NULL, PRIMARY KEY (case_id, doc_type))""",
]
_ready = False


def _connect():
    if DB_URL:
        import psycopg
        return psycopg.connect(DB_URL, autocommit=True), "%s"
    c = sqlite3.connect(SQLITE_PATH)
    c.isolation_level = None
    return c, "?"


def _exec(sql, params=(), fetch=False):
    global _ready
    conn, ph = _connect()
    try:
        cur = conn.cursor()
        if not _ready:
            for ddl in _SCHEMA:
                cur.execute(ddl)
            cur.execute(f"SELECT COUNT(*) FROM la_archive")
            if cur.fetchone()[0] == 0:  # seed the demo archive once
                for cif, data in json.loads(SEED_DB.read_text())["archive"].items():
                    cur.execute(f"INSERT INTO la_archive (cif, data) VALUES ({ph}, {ph})",
                                (cif, json.dumps(data)))
            _ready = True
        cur.execute(sql.replace("%s", ph), params)
        return cur.fetchall() if fetch else None
    finally:
        conn.close()


@agent.tool("archive.lookup_cif")
async def archive_lookup(input: dict) -> dict:
    cif = str(input.get("cif", "")).strip()
    rows = _exec("SELECT data FROM la_archive WHERE cif = %s", (cif,), fetch=True)
    return {"cif": cif, "found": bool(rows),
            "customer": json.loads(rows[0][0]) if rows else None}


@agent.tool("la.create_file")
async def la_create_file(input: dict) -> dict:
    cif = str(input["cif"])
    data = json.dumps({"cif": cif, "customerName": input.get("customerName") or "Unknown",
                       "status": "new"})
    rows = _exec("SELECT 1 FROM la_archive WHERE cif = %s", (cif,), fetch=True)
    if not rows:
        _exec("INSERT INTO la_archive (cif, data) VALUES (%s, %s)", (cif, data))
    return {"ok": True, "fileId": f"file-{cif}"}


@agent.tool("la.create_renewal")
async def la_create_renewal(input: dict) -> dict:
    cif, product = str(input["cif"]), input.get("product", "LA")
    case_id = f"{product}-{cif}"
    if not _exec("SELECT 1 FROM la_renewals WHERE case_id = %s", (case_id,), fetch=True):
        _exec("INSERT INTO la_renewals (case_id, cif, product, status) VALUES (%s, %s, %s, 'draft')",
              (case_id, cif, product))
    if not _exec("SELECT 1 FROM la_cases WHERE case_id = %s", (case_id,), fetch=True):
        _exec("INSERT INTO la_cases (case_id, cif) VALUES (%s, %s)", (case_id, cif))
    return {"ok": True, "caseId": case_id}


@agent.tool("case.save_extraction")
async def case_save_extraction(input: dict) -> dict:
    rows = _exec("SELECT extractions FROM la_cases WHERE case_id = %s", (input["caseId"],), fetch=True)
    if not rows:
        return {"ok": False, "error": f"unknown case {input['caseId']}"}
    if input.get("docType") == "reference_report":
        _exec("UPDATE la_cases SET report_schema = %s WHERE case_id = %s",
              (json.dumps(input["data"]), input["caseId"]))
    if DB_URL:
        _exec("INSERT INTO la_extractions (case_id, doc_type, data) VALUES (%s, %s, %s) "
              "ON CONFLICT (case_id, doc_type) DO UPDATE SET data = EXCLUDED.data",
              (input["caseId"], input["docType"], json.dumps(input["data"])))
    else:
        _exec("INSERT OR REPLACE INTO la_extractions (case_id, doc_type, data) VALUES (%s, %s, %s)",
              (input["caseId"], input["docType"], json.dumps(input["data"])))
    n = _exec("SELECT COUNT(*) FROM la_extractions WHERE case_id = %s",
              (input["caseId"],), fetch=True)[0][0]
    return {"ok": True, "caseId": input["caseId"], "docType": input.get("docType"),
            "extractedCount": n}


@agent.tool("case.load")
async def case_load(input: dict) -> dict:
    cif = str(input.get("cif", "")).strip()
    rows = _exec("SELECT case_id, extractions, report_schema FROM la_cases WHERE cif = %s",
                 (cif,), fetch=True)
    if not rows:
        return {"found": False, "cif": cif}
    case_id, _unused, schema = rows[0]
    ext_rows = _exec("SELECT doc_type, data FROM la_extractions WHERE case_id = %s "
                     "AND doc_type != 'reference_report'", (case_id,), fetch=True)
    ext = json.dumps({d: json.loads(v) for d, v in ext_rows})
    ren = _exec("SELECT product, status FROM la_renewals WHERE case_id = %s", (case_id,), fetch=True)
    arch = _exec("SELECT data FROM la_archive WHERE cif = %s", (cif,), fetch=True)
    return {"found": True,
            "case": {"caseId": case_id, "cif": cif,
                     "extractions": json.loads(ext or "{}"),
                     "reportSchema": json.loads(schema) if schema else None},
            "renewal": {"caseId": case_id, "product": ren[0][0], "status": ren[0][1]} if ren else {},
            "archive": json.loads(arch[0][0]) if arch else None}


@agent.tool("la.update_record")
async def la_update_record(input: dict) -> dict:
    """Step 8 - the single gated write. Idempotent (same UPDATE, keyed by case)
    and CIF-guarded: a confused caller cannot redirect it to another customer."""
    rows = _exec("SELECT cif FROM la_renewals WHERE case_id = %s", (input["caseId"],), fetch=True)
    if not rows:
        return {"ok": False, "error": f"unknown case {input['caseId']}"}
    if str(input.get("cif")) != rows[0][0]:
        return {"ok": False, "error": "cif does not match the case record - write refused"}
    from datetime import datetime, timezone
    _exec("UPDATE la_renewals SET status='submitted', final_report=%s, data_summary=%s, updated_at=%s "
          "WHERE case_id = %s",
          (input["report"], json.dumps(input.get("summary") or {}),
           datetime.now(timezone.utc).isoformat(), input["caseId"]))
    return {"ok": True, "caseId": input["caseId"], "status": "submitted"}


# ---- step 1-2: intent -> case -> document checklist -------------------------
async def handle_message(ctx, event):
    text = event.payload.get("text") or event.payload.get("body") or ""
    intent = (await ctx.llm.respond(
        route="intent", schema=INTENT_SCHEMA,
        system="Classify this business-banking request. start_renewal needs an explicit "
               "renewal/application request with a CIF number; product defaults to LA.",
        input={"message": text},
    )).json

    if intent["action"] != "start_renewal" or not intent["cif"]:
        ctx.logs.info("no actionable intent", action=intent["action"])
        return {"reply": "Tell me e.g. 'start an LA renewal application for CIF 884411'."}

    cif, product = intent["cif"], intent.get("product") or "LA"
    lookup = await ctx.tools.call("archive.lookup_cif", {"cif": cif})
    if lookup["found"]:
        ctx.logs.info("CIF found in archive", cif=cif)
    else:
        await ctx.tools.call("la.create_file", {"cif": cif})
    created = await ctx.tools.call("la.create_renewal", {"cif": cif, "product": product})

    await ctx.memory.set(f"case:{cif}", {
        "caseId": created["caseId"], "cif": cif, "product": product,
        "required": REQUIRED_DOCS, "received": {}, "status": "collecting",
    }, scope="agent")

    checklist = ", ".join(REQUIRED_DOCS)
    reply = (f"Renewal case {created['caseId']} opened for CIF {cif} "
             f"({(lookup.get('customer') or {}).get('customerName', 'new customer')}). "
             f"Please upload: {checklist}. Tag each upload with cif={cif} and its docType.")
    ctx.emit_ui("checklist", {"caseId": created["caseId"], "required": REQUIRED_DOCS,
                              "received": []})
    return {"reply": reply, "caseId": created["caseId"]}


# ---- step 3: uploads tick the checklist; complete -> fan out ----------------
async def handle_upload(ctx, event):
    tags = event.payload.get("tags") or {}
    cif, doc_type = tags.get("cif"), tags.get("docType")
    if not cif or not doc_type:
        return {"ignored": "upload without cif/docType tags"}
    case = await ctx.memory.get(f"case:{cif}", scope="agent")
    if case is None:
        return {"ignored": f"no open case for CIF {cif}"}

    case["received"][doc_type] = event.payload["fileId"]
    missing = [d for d in case["required"] if d not in case["received"]]
    if missing:
        await ctx.memory.set(f"case:{cif}", case, scope="agent")
        ctx.emit_ui("checklist", {"caseId": case["caseId"], "required": case["required"],
                                  "received": sorted(case["received"])})
        return {"caseId": case["caseId"], "missing": missing}

    # Checklist complete - fan out one retryable extraction job per document
    # (step 3-4), then the reference-report schema derivation (step 5 input).
    case["status"] = "extracting"
    await ctx.memory.set(f"case:{cif}", case, scope="agent")
    # Splitting long PDFs is heavy - plan in a job, never in the event path.
    await ctx.jobs.schedule("plan_extraction", {"cif": cif, "caseId": case["caseId"],
                                                "received": case["received"]})
    return {"caseId": case["caseId"], "status": "extracting",
            "jobs": len(case["received"])}


@agent.on_event
async def main(ctx, event):
    if event.type == "file.uploaded":
        return await handle_upload(ctx, event)
    return await handle_message(ctx, event)


# ---- step 4-5: plan -> chunked unit extraction (deep-document capable) ------
@agent.job("plan_extraction")
async def plan_extraction(ctx, job):
    """Split oversized PDFs into page-range chunk files, then fan out ONE job
    group over every unit (small doc = one unit; long doc = many chunk units).
    Compose fires exactly once when every unit succeeds."""
    from rya.documents import split_pdf

    p = job.payload
    units = []
    for doc_type, file_id in p["received"].items():
        doc = await ctx.files.as_document(file_id)
        if doc["format"] == "pdf" and (len(doc["bytes"]) > 3_000_000):
            chunks = split_pdf(doc["bytes"])
        else:
            chunks = split_pdf(doc["bytes"]) if doc["format"] == "pdf" else \
                [{"bytes": doc["bytes"], "pages": (1, 1), "part": 0}]
        if len(chunks) == 1:
            units.append({"cif": p["cif"], "caseId": p["caseId"], "docType": doc_type,
                          "fileId": file_id, "part": 0, "pages": list(chunks[0]["pages"])})
            continue
        for c in chunks:
            meta = await ctx.files.save(
                f"{doc_type}.p{c['pages'][0]}-{c['pages'][1]}.pdf", c["bytes"],
                content_type="application/pdf",
                tags={"cif": p["cif"], "docType": doc_type, "chunk": str(c["part"])})
            units.append({"cif": p["cif"], "caseId": p["caseId"], "docType": doc_type,
                          "fileId": meta["id"], "part": c["part"],
                          "pages": list(c["pages"])})
    await ctx.jobs.schedule_group(
        [("extract_unit", u) for u in units],
        on_complete=("compose_report", {"cif": p["cif"], "caseId": p["caseId"]}))
    return {"units": len(units), "documents": len(p["received"])}


@agent.job("extract_unit")
async def extract_unit(ctx, job):
    """Extract one unit (a whole small doc, or one chunk of a long one).
    Two-pass per chunk: a cheap relevance gate first, deep extraction only
    when the pages actually contain target data - most pages of a long credit
    file are irrelevant to any given field."""
    p = job.payload
    doc = await ctx.files.as_document(p["fileId"])
    pages = f"p{p['pages'][0]}-{p['pages'][1]}"

    if p["docType"] == "reference_report":
        res = await ctx.llm.respond(
            route="schema",
            system="This is a reference credit-renewal report. List the sections it contains "
                   "and, for each, the data fields it uses. Respond as JSON: "
                   '{"sections": [{"title": str, "fields": [str]}]}.',
            input={"task": "derive the report schema"}, documents=[doc],
            schema={"type": "object", "properties": {"sections": {"type": "array"}},
                    "required": ["sections"]},
        )
        data = res.json
    else:
        prompt = EXTRACTION_PROMPTS.get(p["docType"], "Extract all financially material data.")
        data = {}
        relevant = True
        if p["part"] or p["pages"][1] - p["pages"][0] > 20:  # chunked doc: gate first
            gate = await ctx.llm.respond(
                route="intent",
                system=f"Task: {prompt} Do these pages contain ANY of that data? "
                       'Respond as JSON {"relevant": true|false}.',
                input={"pages": pages}, documents=[doc],
                schema={"type": "object", "properties": {"relevant": {"type": "boolean"}},
                        "required": ["relevant"]},
            )
            relevant = bool(gate.json.get("relevant"))
        if relevant:
            res = await ctx.llm.respond(
                route="extract",
                system=prompt + " Respond as a flat JSON object with EXACTLY the schema's "
                                "field names. Use numbers for amounts (no currency "
                                "symbols). Use null for anything not on these pages.",
                input={"docType": p["docType"], "pages": pages}, documents=[doc],
                schema=EXTRACTION_SCHEMAS.get(p["docType"], {"type": "object"}),
            )
            data = res.json

    await ctx.tools.call("case.save_extraction",
                         {"caseId": p["caseId"],
                          "docType": f"{p['docType']}#part{p['part']}",
                          "data": {"_pages": pages, **data}}
                         if p["docType"] != "reference_report" else
                         {"caseId": p["caseId"], "docType": "reference_report", "data": data})
    return {"docType": p["docType"], "part": p["part"], "pages": pages,
            "extracted": len(data)}


# ---- step 6-8: filter -> cited compose -> grounding -> approval -------------
def filter_to_schema(extractions: dict, schema: dict) -> dict:
    """Step 6, deterministic: keep only fields the reference report needs.
    Matching is fuzzy-by-name (schema field names vs extraction keys); if the
    schema derivation produced nothing usable, keep everything."""
    wanted = {f.lower().replace(" ", "_") for s in (schema or {}).get("sections", [])
              for f in (s.get("fields") or []) if isinstance(f, str)}
    if not wanted:
        return extractions
    out = {}
    for doc_type, data in extractions.items():
        if not isinstance(data, dict):
            out[doc_type] = data
            continue
        kept = {k: v for k, v in data.items()
                if any(w in k.lower() or k.lower() in w for w in wanted)}
        out[doc_type] = kept or data  # never filter a document to nothing
    return out


@agent.job("compose_report")
async def compose_report(ctx, job):
    p = job.payload
    # Loaded via a TOOL so the data enters this run's trace - which is exactly
    # what the grounding gate checks the report's figures against.
    loaded = await ctx.tools.call("case.load", {"cif": p["cif"]})
    if not loaded.get("found"):
        raise RuntimeError(f"case for CIF {p['cif']} disappeared")
    case = loaded["case"]

    # Provenance merge: chunk parts per document, conflicts FLAGGED not
    # silently last-wins - a conflicting figure is information for the
    # reviewer, never a coin flip.
    from rya.documents import merge_extractions
    by_doc: dict = {}
    for key, data in case["extractions"].items():
        doc_type = key.split("#part")[0]
        pages = (data or {}).pop("_pages", "?") if isinstance(data, dict) else "?"
        by_doc.setdefault(doc_type, []).append({"data": data, "source": pages})
    merged, all_conflicts = {}, []
    for doc_type, parts in by_doc.items():
        m = merge_extractions(parts)
        merged[doc_type] = {k: v["value"] for k, v in m["fields"].items()}
        for c in m["conflicts"]:
            all_conflicts.append({"docType": doc_type, **c})

    filtered = filter_to_schema(merged, case.get("reportSchema"))

    res = await ctx.llm.respond(
        route="compose",
        system="Write the final credit renewal report following the reference report's "
               "sections. Cite every figure inline as [source: <docType>.<field>] - "
               "step 7 of the pipeline. Use ONLY the provided data; if a required field "
               "is missing, write 'not available' rather than estimating. If any "
               "conflicts are provided, add a 'DATA CONFLICTS - REVIEW REQUIRED' section "
               "listing each conflicting field with both values and their page sources. NEVER compute derived figures (differences, sums, percentages) - state only values verbatim from the provided data.",
        input={"customer": loaded.get("archive"), "renewal": loaded.get("renewal"),
               "reportSchema": case.get("reportSchema"), "data": filtered,
               "conflicts": all_conflicts or "none"},
    )
    report = res.text

    verdict = ctx.guard.check_grounding(report)
    if not verdict["ok"]:
        # An unsourced financial figure is a hard stop, not a warning.
        ctx.logs.error("grounding violations in composed report",
                             violations=verdict["violations"])
        raise RuntimeError(f"report contains unsourced figures: {verdict['violations']}")

    await ctx.approvals.request(
        title=f"Final {loaded['renewal'].get('product', 'LA')} renewal report - CIF {p['cif']}",
        body=report[:2000],
        action={"tool": "la.update_record",
                "input": {"cif": p["cif"], "caseId": p["caseId"], "report": report,
                          "summary": {"conflicts": all_conflicts,
                                      "documents": sorted(case["extractions"]),
                                      "filteredFields": {k: sorted(v) if isinstance(v, dict) else []
                                                         for k, v in filtered.items()}}}},
    )
    return {"caseId": p["caseId"], "status": "awaiting_approval"}
