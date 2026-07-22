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

# Seed data ships in the repo; all writes go to a runtime copy so demo runs
# never dirty the checked-in fixture.
SEED_DB = Path(__file__).resolve().parent.parent / "data" / "bank_db.json"
BANK_DB = SEED_DB.with_name("bank_db.runtime.json")
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

EXTRACTION_PROMPTS = {
    "aecb": "Extract from this AECB credit bureau report: credit score, total outstanding "
            "liabilities (AED), monthly obligations (AED), number of active facilities, "
            "any defaults or bounced cheques, and worst delinquency bucket.",
    "spread": "Extract from this financial spread: annual revenue (AED), net profit (AED), "
              "total assets (AED), total liabilities (AED), current ratio, and leverage ratio.",
    "id": "Extract from this identity document: legal entity name, trade license number, "
          "license expiry date, and authorized signatories.",
}


# ---- bank-system tools (leaves over the bank DB) ----------------------------
def _db() -> dict:
    src = BANK_DB if BANK_DB.exists() else SEED_DB
    return json.loads(src.read_text())


def _save_db(db: dict) -> None:
    BANK_DB.write_text(json.dumps(db, indent=2, default=str))


@agent.tool("archive.lookup_cif")
async def archive_lookup(input: dict) -> dict:
    cif = str(input.get("cif", "")).strip()
    customer = _db()["archive"].get(cif)
    return {"cif": cif, "found": customer is not None, "customer": customer}


@agent.tool("la.create_file")
async def la_create_file(input: dict) -> dict:
    db = _db()
    cif = str(input["cif"])
    db["la"]["files"].setdefault(cif, {"cif": cif, "customerName": input.get("customerName") or "Unknown",
                                       "status": "new"})
    _save_db(db)
    return {"ok": True, "fileId": f"file-{cif}"}


@agent.tool("la.create_renewal")
async def la_create_renewal(input: dict) -> dict:
    db = _db()
    cif, product = str(input["cif"]), input.get("product", "LA")
    case_id = f"{product}-{cif}"
    db["la"]["renewals"][case_id] = {"caseId": case_id, "cif": cif, "product": product,
                                     "status": "draft"}
    db["la"]["cases"].setdefault(case_id, {"caseId": case_id, "cif": cif, "extractions": {},
                                           "reportSchema": None})
    _save_db(db)
    return {"ok": True, "caseId": case_id}


@agent.tool("case.save_extraction")
async def case_save_extraction(input: dict) -> dict:
    db = _db()
    case = db["la"]["cases"].get(input["caseId"])
    if case is None:
        return {"ok": False, "error": f"unknown case {input['caseId']}"}
    if input.get("docType") == "reference_report":
        case["reportSchema"] = input["data"]
    else:
        case["extractions"][input["docType"]] = input["data"]
    _save_db(db)
    return {"ok": True, "caseId": input["caseId"], "docType": input.get("docType")}


@agent.tool("case.load")
async def case_load(input: dict) -> dict:
    db = _db()
    cif = str(input.get("cif", "")).strip()
    case = next((c for c in db["la"]["cases"].values() if c["cif"] == cif), None)
    if case is None:
        return {"found": False, "cif": cif}
    renewal = db["la"]["renewals"].get(case["caseId"], {})
    return {"found": True, "case": case, "renewal": renewal,
            "archive": db["archive"].get(cif)}


@agent.tool("la.update_record")
async def la_update_record(input: dict) -> dict:
    """Step 8 - the single gated write: final report + summary into the LA DB."""
    db = _db()
    case_id = input["caseId"]
    renewal = db["la"]["renewals"].get(case_id)
    case = db["la"]["cases"].get(case_id)
    # Defense in the tool itself: the write must match the case's own CIF -
    # a confused caller (or model) cannot redirect it to another customer.
    if renewal is None or case is None:
        return {"ok": False, "error": f"unknown case {case_id}"}
    if str(input.get("cif")) != renewal["cif"]:
        return {"ok": False, "error": "cif does not match the case record - write refused"}
    renewal["status"] = "submitted"
    renewal["finalReport"] = input["report"]
    renewal["dataSummary"] = input.get("summary") or {}
    _save_db(db)
    return {"ok": True, "caseId": case_id, "status": "submitted"}


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
    for doc_type, file_id in case["received"].items():
        await ctx.jobs.schedule("extract_document", {
            "cif": cif, "caseId": case["caseId"], "docType": doc_type, "fileId": file_id,
        })
    return {"caseId": case["caseId"], "status": "extracting",
            "jobs": len(case["received"])}


@agent.on_event
async def main(ctx, event):
    if event.type == "file.uploaded":
        return await handle_upload(ctx, event)
    return await handle_message(ctx, event)


# ---- step 4-5: per-document extraction jobs ---------------------------------
@agent.job("extract_document")
async def extract_document(ctx, job):
    p = job.payload
    doc = await ctx.files.as_document(p["fileId"])

    if p["docType"] == "reference_report":
        # Step 5: the reference report defines the FORMAT and FIELDS of the
        # final output - derive a required-fields schema from it.
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
        res = await ctx.llm.respond(
            route="extract",
            system=prompt + " Respond as a flat JSON object. Use numbers for amounts "
                            "(no currency symbols). Use null for anything not present.",
            input={"docType": p["docType"]}, documents=[doc],
            schema={"type": "object"},
        )
        data = res.json

    await ctx.tools.call("case.save_extraction",
                         {"caseId": p["caseId"], "docType": p["docType"], "data": data})

    # Fan-in: the job that completes the set schedules the compose step.
    case = await ctx.memory.get(f"case:{p['cif']}", scope="agent")
    done = set((case or {}).get("extracted", [])) | {p["docType"]}
    case["extracted"] = sorted(done)
    await ctx.memory.set(f"case:{p['cif']}", case, scope="agent")
    if done >= set(case["required"]):
        await ctx.jobs.schedule("compose_report", {"cif": p["cif"], "caseId": p["caseId"]})
        return {"docType": p["docType"], "complete": True}
    return {"docType": p["docType"], "extractedSoFar": sorted(done)}


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

    filtered = filter_to_schema(case["extractions"], case.get("reportSchema"))

    res = await ctx.llm.respond(
        route="compose",
        system="Write the final credit renewal report following the reference report's "
               "sections. Cite every figure inline as [source: <docType>.<field>] - "
               "step 7 of the pipeline. Use ONLY the provided data; if a required field "
               "is missing, write 'not available' rather than estimating.",
        input={"customer": loaded.get("archive"), "renewal": loaded.get("renewal"),
               "reportSchema": case.get("reportSchema"), "data": filtered},
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
                          "summary": {"documents": sorted(case["extractions"]),
                                      "filteredFields": {k: sorted(v) if isinstance(v, dict) else []
                                                         for k, v in filtered.items()}}}},
    )
    return {"caseId": p["caseId"], "status": "awaiting_approval"}
