"""Trace export — ship completed run traces to an external observability backend.

Rya is the runtime; it does not try to *be* an observability product. Instead it
emits each completed run to best-in-class backends, env-gated and best-effort (an
export failure never fails the run):

- ``RYA_TRACE_WEBHOOK`` → POST the run summary to any URL (generic / self-hosted).
- ``LANGFUSE_HOST`` + ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` → push the
  run as a Langfuse trace with one nested *observation* per step (model/LLM steps
  become GENERATIONs carrying token usage; tool steps become SPANs).
- ``RYA_OTLP_ENDPOINT`` → POST OpenTelemetry spans (OTLP/HTTP JSON) with GenAI
  semantic-convention attributes, so the same run lights up Arize Phoenix,
  Grafana Tempo, Datadog, or any OTLP collector.

The engine calls ``export_run`` once a run reaches a terminal status.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .usage import run_usage

# Trace kinds that represent a model/LLM call (→ Langfuse GENERATION / OTLP gen_ai span).
# `llm.chat` is the per-step kind emitted by the governed agent loop (ctx.llm.run);
# `llm.respond` is a single-shot ctx.llm.respond; `model.call` is a custom model.
# All three must map to GENERATIONs — otherwise the agent loop's model turns (the
# interesting ones) land as contentless EVENTs and token metering skips them.
_LLM_KINDS = ("llm.respond", "model.call", "llm.chat")
_FAILED_KINDS = ("run.failed", "run.rejected")

# Kind prefixes that are runtime *bookkeeping* rather than agent work. They are
# still exported (they matter for forensics) but at DEBUG level and grouped under
# one collapsed span, so the top level of a trace shows only the model steps and
# the tools they called — the thing a reader actually came for.
_DEBUG_PREFIXES = ("memory.", "session.", "file.", "connection.", "knowledge.")
# Instantaneous markers: no meaningful duration, so they stay Langfuse EVENTs
# (only SPAN and above accept an endTime).
_EVENT_KINDS = ("run.started", "run.completed", "run.failed", "run.rejected",
                "run.needs_reconnect", "log", "trace.event", "approval.requested",
                "tool.retry", "tool.repair", "tool.adopt")
_WARNING_KINDS = ("tool.retry", "tool.repair", "run.needs_reconnect")


def _level_of(kind: str) -> str:
    if kind in _FAILED_KINDS:
        return "ERROR"
    if kind in _WARNING_KINDS or kind.startswith("guard."):
        return "WARNING"
    if kind.startswith(_DEBUG_PREFIXES):
        return "DEBUG"
    return "DEFAULT"


def _io_of(data: dict):
    """Split a journaled step's data into Langfuse (input, output).

    Every step records ``result`` (its return value) alongside whatever inputs the
    caller passed. Surfacing them as real input/output — for *all* kinds, not just
    LLM/tool steps — is what stops memory/session observations rendering as
    contentless rows in the UI.
    """
    out = data.get("result")
    inp = {k: v for k, v in data.items() if k != "result"} or None
    return inp, out


def _post(url: str, headers: dict, payload: dict, timeout: int = 10) -> int:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _lf_configured(env: dict) -> bool:
    return bool(env.get("LANGFUSE_HOST") and env.get("LANGFUSE_PUBLIC_KEY")
                and env.get("LANGFUSE_SECRET_KEY"))


def _lf_auth(env: dict) -> str:
    return base64.b64encode(
        f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()


def run_summary(run: dict, env: Optional[dict] = None) -> dict:
    return {
        "runId": run["id"],
        "agent": run.get("agent"),
        "agentVersion": run.get("agentVersion"),
        "status": run.get("status"),
        "trigger": run.get("trigger"),
        "createdAt": run.get("createdAt"),
        "usage": run_usage(run, env),
        "error": run.get("error"),
        "trace": run.get("trace", []),
    }


# ---- shared helpers --------------------------------------------------------
def _result_of(ev: dict):
    return (ev.get("data") or {}).get("result")


def _model_of(ev: dict) -> str:
    res = _result_of(ev)
    if isinstance(res, dict) and res.get("model"):
        return res["model"]
    return ev.get("label") or ev.get("kind", "")


def _usage_of(ev: dict) -> dict:
    res = _result_of(ev)
    u = (res.get("usage") if isinstance(res, dict) else None) or {}
    return {"input": u.get("input") or 0, "output": u.get("output") or 0}


def _provider_of(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude") or "anthropic" in m:
        return "anthropic"
    if m.startswith("gpt") or m.startswith("o1") or "openai" in m:
        return "openai"
    return "rya"


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _llm_input(data: dict):
    """The exact prompt sent to the model: the real chat `messages` (with the
    system block prepended) when the agent loop journaled them, else whatever
    single-shot input/system was recorded."""
    explicit = data.get("input")
    if explicit is not None:
        return explicit
    msgs = data.get("messages")
    system = data.get("system")
    if msgs:
        return ([{"role": "system", "content": system}] if system else []) + list(msgs)
    return system


# ---- Langfuse --------------------------------------------------------------
def _langfuse(run: dict, env: dict) -> None:
    auth = _lf_auth(env)
    created = run.get("createdAt")
    rid = run["id"]
    # Engine runs: trigger is a string and the event dict sits alongside.
    # Ingested runs: trigger may itself be the dict. Prefer the richer one.
    trigger = run.get("trigger") or {}
    event = run.get("event")
    trace_input = event if isinstance(event, dict) else (
        trigger if isinstance(trigger, dict) else {"trigger": trigger})
    trace = run.get("trace", [])
    n = len(trace)
    # Prefer the real wall-clock span recorded per step (`startedAt`/`endedAt`,
    # millisecond precision). Runs recorded before that existed only carry a
    # second-resolution `ts`, which collapses a whole turn onto one instant — for
    # those, fall back to a synthetic seq-derived offset purely so the steps stay
    # ORDERED (their durations are not real and are left as a 1ms placeholder).
    base_dt = (_parse_iso(created)
               or _parse_iso(trace[0].get("ts") if trace else None)
               or datetime(1970, 1, 1, tzinfo=timezone.utc))

    def _seq_time(seq) -> str:
        return _iso_ms(base_dt + timedelta(milliseconds=int(seq if seq is not None else 0)))

    def _times(i, ev):
        started, ended = ev.get("startedAt"), ev.get("endedAt")
        if started:
            return started, (ended or started)
        seq = ev.get("seq")
        nxt = trace[i + 1].get("seq") if i + 1 < n else (seq or 0) + 1
        return _seq_time(seq), _seq_time(nxt if nxt is not None else (seq or 0) + 1)

    # Root span covers the whole run: first step's start → last step's end.
    starts = [t for t in (ev.get("startedAt") for ev in trace) if t]
    ends = [t for t in (ev.get("endedAt") for ev in trace) if t]
    root_start = min(starts) if starts else _seq_time(0)
    created_dt = _parse_iso(created)
    if created_dt is not None:
        created_ms = _iso_ms(created_dt)
        if not starts or created_ms < root_start:
            root_start = created_ms
    root_end = max(ends) if ends else _seq_time(n + 1)
    if root_end < root_start:
        root_end = root_start
    root_oid = f"{rid}-run"

    # Langfuse groups traces that share a sessionId into one conversation view —
    # invaluable for a multi-turn agent. The runtime doesn't hand the session id
    # to the exporter, so recover it from whichever session step ran this turn.
    session_id = None
    for ev in trace:
        if str(ev.get("kind", "")).startswith("session."):
            res = _result_of(ev)
            if isinstance(res, dict) and res.get("sessionId"):
                session_id = res["sessionId"]
                break
            if isinstance(res, dict) and res.get("id") and ev.get("kind") == "session.get_or_create":
                session_id = res["id"]
                break

    status = run.get("status")
    trace_body = {"id": rid, "name": run.get("agent"),
                  "input": trace_input,
                  # The engine stores the handler's return value on `output`;
                  # `result` was never populated, which is why traces showed a
                  # null output in the UI.
                  "output": run.get("output", run.get("result")),
                  "tags": [t for t in (run.get("agent"), status) if t],
                  "metadata": {"status": status, "usage": run_usage(run, env),
                               "agentVersion": run.get("agentVersion")}}
    if session_id:
        trace_body["sessionId"] = session_id
    if run.get("userId"):
        trace_body["userId"] = run["userId"]

    batch = [
        {"id": f"{rid}-trace", "type": "trace-create",
         "timestamp": created or root_start, "body": trace_body},
        {"id": root_oid, "type": "span-create", "timestamp": root_start,
         "body": {"id": root_oid, "traceId": rid, "type": "SPAN",
                  "name": f"{run.get('agent') or 'run'} run",
                  "startTime": root_start, "endTime": root_end,
                  "input": trace_input, "output": run.get("output", run.get("result")),
                  "level": "ERROR" if status in ("failed", "rejected") else "DEFAULT",
                  "metadata": {"status": status}}},
    ]

    def _add(oid, otype, body, ts):
        batch.append({"id": oid, "type": otype, "timestamp": ts, "body": body})

    # --- synthetic grouping spans ------------------------------------------
    # `loopId` marks every step of one ctx.llm.run agent loop (and the tools it
    # called). Wrapping them in a span gives the LangGraph-style
    # `run -> agent loop -> step -> tool` shape without the runtime having to
    # journal an extra step (which would be replay-visible).
    loops: dict = {}
    for i, ev in enumerate(trace):
        lid = (ev.get("data") or {}).get("loopId")
        if lid:
            loops.setdefault(lid, []).append(i)
    loop_oid = {}
    for k, (lid, idxs) in enumerate(loops.items()):
        oid = f"{rid}-loop-{k}"
        loop_oid[lid] = oid
        st, _ = _times(idxs[0], trace[idxs[0]])
        _, et = _times(idxs[-1], trace[idxs[-1]])
        # Summarise the loop on its own span: what was asked, what came back,
        # and which tools it went through — readable without expanding it.
        first_msgs = (trace[idxs[0]].get("data") or {}).get("messages") or []
        last_res = _result_of(trace[idxs[-1]])
        tools_used = [trace[i].get("label") for i in idxs
                      if trace[i].get("kind") == "tool.call"]
        _add(oid, "span-create",
             {"id": oid, "traceId": rid, "parentObservationId": root_oid, "type": "SPAN",
              "name": "agent loop", "startTime": st, "endTime": et,
              "input": first_msgs[-1] if first_msgs else None,
              "output": {"text": last_res.get("text") if isinstance(last_res, dict) else last_res,
                         "toolsCalled": tools_used},
              "metadata": {"steps": len(idxs), "toolsCalled": tools_used}}, st)

    # Bookkeeping (memory/session/file/…) goes under one collapsed DEBUG span so
    # it stays available for forensics without competing with the agent's work.
    dbg_idxs = [i for i, ev in enumerate(trace)
                if _level_of(ev.get("kind", "")) == "DEBUG"]
    ctx_oid = None
    if dbg_idxs:
        ctx_oid = f"{rid}-context"
        st, _ = _times(dbg_idxs[0], trace[dbg_idxs[0]])
        _, et = _times(dbg_idxs[-1], trace[dbg_idxs[-1]])
        ops = [f"{trace[i].get('kind')}:{trace[i].get('label')}" for i in dbg_idxs]
        _add(ctx_oid, "span-create",
             {"id": ctx_oid, "traceId": rid, "parentObservationId": root_oid, "type": "SPAN",
              "name": f"context ({len(dbg_idxs)} ops)", "startTime": st, "endTime": et,
              "level": "DEBUG", "output": {"ops": ops},
              "metadata": {"ops": len(dbg_idxs)}}, st)

    # --- one observation per step ------------------------------------------
    # A tool call nests under the model step that requested it; `last_llm_oid`
    # tracks that step (and is the fallback for runs recorded before `loopId`).
    last_llm_oid = root_oid
    for i, ev in enumerate(trace):
        seq, kind, label = ev.get("seq"), ev.get("kind", ""), ev.get("label")
        oid = f"{rid}-{seq}"
        st, et = _times(i, ev)
        data = ev.get("data") or {}
        lid = data.get("loopId")
        level = _level_of(kind)
        inp, out = _io_of(data)

        if kind in _LLM_KINDS:
            u = _usage_of(ev)
            body = {"id": oid, "traceId": rid,
                    "parentObservationId": loop_oid.get(lid, root_oid),
                    "type": "GENERATION", "name": label,
                    "startTime": st, "endTime": et, "model": _model_of(ev),
                    "input": _llm_input(data), "output": out, "level": level,
                    "usage": {"input": u["input"], "output": u["output"],
                              "total": u["input"] + u["output"], "unit": "TOKENS"}}
            if data.get("modelParameters"):
                body["modelParameters"] = {k: v for k, v in data["modelParameters"].items()
                                           if v is not None}
            _add(oid, "generation-create", body, st)
            last_llm_oid = oid
            continue

        if kind == "tool.call":
            # Under the model step that requested it (falls back to the run root
            # for a direct ctx.tools.call outside any loop).
            _add(oid, "span-create",
                 {"id": oid, "traceId": rid, "parentObservationId": last_llm_oid, "type": "SPAN",
                  "name": label, "startTime": st, "endTime": et,
                  "input": data.get("input"), "output": out, "level": level,
                  "metadata": {"permission": data.get("permission")}}, st)
            continue

        parent = ctx_oid if level == "DEBUG" and ctx_oid else root_oid
        name = f"{kind}:{label}" if label else kind
        if kind in _EVENT_KINDS:
            body = {"id": oid, "traceId": rid, "parentObservationId": parent,
                    "name": name, "startTime": st,
                    "input": inp, "output": out, "level": level, "metadata": data}
            if level == "ERROR":
                body["statusMessage"] = str(data.get("message") or label or "")[:500]
            _add(oid, "event-create", body, st)
        else:
            # Everything else did real work (memory/session/ui/channel/job): a SPAN
            # so it can carry an endTime and show its true duration.
            _add(oid, "span-create",
                 {"id": oid, "traceId": rid, "parentObservationId": parent, "type": "SPAN",
                  "name": name, "startTime": st, "endTime": et,
                  "input": inp, "output": out, "level": level}, st)

    host = env["LANGFUSE_HOST"].rstrip("/")
    _post(f"{host}/api/public/ingestion", {"Authorization": f"Basic {auth}"}, {"batch": batch})


# ---- OpenTelemetry (OTLP/HTTP JSON) ---------------------------------------
def _ns(iso: Optional[str], fallback: int) -> int:
    if not iso:
        return fallback
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return fallback


def _hex(s: str, nbytes: int) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[: nbytes * 2]


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}  # int64 → string per OTLP JSON
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": "" if value is None else str(value)}}


def _otlp(run: dict, env: dict) -> None:
    rid = run["id"]
    trace_id = _hex(rid, 16)
    root_span = _hex(f"{rid}:root", 8)
    created = run.get("createdAt")
    trace = run.get("trace", [])
    base = _ns(created, 0) or _ns(trace[0].get("ts") if trace else None, 1_000_000_000)
    last = _ns(trace[-1].get("ts") if trace else None, base)

    spans = [{
        "traceId": trace_id, "spanId": root_span, "name": f"rya.run {run.get('agent','')}".strip(),
        "kind": 1, "startTimeUnixNano": str(base), "endTimeUnixNano": str(max(last, base)),
        "attributes": [_attr("rya.run_id", rid), _attr("rya.agent", run.get("agent")),
                       _attr("rya.status", run.get("status"))],
        "status": {"code": 2 if run.get("status") in ("failed", "rejected") else 1},
    }]
    # Same hierarchy as the Langfuse export, so Phoenix/Tempo/Datadog show the
    # identical `run -> agent loop -> model step -> tool` shape.
    loop_span = {}
    for lid in {(ev.get("data") or {}).get("loopId") for ev in trace} - {None}:
        loop_span[lid] = _hex(f"{rid}:loop:{lid}", 8)
    for lid, sid in loop_span.items():
        idxs = [i for i, ev in enumerate(trace) if (ev.get("data") or {}).get("loopId") == lid]
        s = _ns(trace[idxs[0]].get("startedAt") or trace[idxs[0]].get("ts"), base)
        e = _ns(trace[idxs[-1]].get("endedAt") or trace[idxs[-1]].get("ts"), s)
        spans.append({
            "traceId": trace_id, "spanId": sid, "parentSpanId": root_span,
            "name": "agent loop", "kind": 1,
            "startTimeUnixNano": str(s), "endTimeUnixNano": str(max(e, s)),
            "attributes": [_attr("rya.loop_id", lid)], "status": {"code": 1},
        })
    last_llm_span = root_span
    for i, ev in enumerate(trace):
        kind, label = ev.get("kind", ""), ev.get("label")
        # Prefer the real recorded span; fall back to `ts` for pre-timing runs.
        start = _ns(ev.get("startedAt") or ev.get("ts"), base)
        if ev.get("endedAt"):
            end = _ns(ev.get("endedAt"), start)
        else:
            end = _ns(trace[i + 1].get("ts"), start) if i + 1 < len(trace) else start
        attrs = [_attr("rya.kind", kind), _attr("rya.label", label)]
        lid = (ev.get("data") or {}).get("loopId")
        parent = loop_span.get(lid, root_span)
        span_id = _hex(f"{rid}:{ev.get('seq', i)}", 8)
        if kind in _LLM_KINDS:
            model = _model_of(ev)
            u = _usage_of(ev)
            attrs += [_attr("gen_ai.operation.name", "chat"), _attr("gen_ai.system", _provider_of(model)),
                      _attr("gen_ai.request.model", model), _attr("gen_ai.response.model", model),
                      _attr("gen_ai.usage.input_tokens", u["input"]),
                      _attr("gen_ai.usage.output_tokens", u["output"])]
            last_llm_span = span_id
        elif kind == "tool.call":
            attrs.append(_attr("rya.permission", (ev.get("data") or {}).get("permission")))
            parent = last_llm_span
        spans.append({
            "traceId": trace_id, "spanId": span_id,
            "parentSpanId": parent, "name": label or kind, "kind": 1,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(end, start)),
            "attributes": attrs, "status": {"code": 2 if kind in _FAILED_KINDS else 1},
        })

    payload = {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", env.get("OTEL_SERVICE_NAME", "rya")),
                                    _attr("rya.agent", run.get("agent"))]},
        "scopeSpans": [{"scope": {"name": "rya"}, "spans": spans}],
    }]}
    endpoint = env["RYA_OTLP_ENDPOINT"].rstrip("/")
    if not endpoint.endswith("/v1/traces"):
        endpoint = f"{endpoint}/v1/traces"
    headers = {}
    if env.get("RYA_OTLP_HEADERS"):  # "key=val,key2=val2"
        for pair in env["RYA_OTLP_HEADERS"].split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                headers[k.strip()] = v.strip()
    _post(endpoint, headers, payload)


def export_scores(trace_id: str, scores: list, env: Optional[dict] = None) -> Optional[str]:
    """Attach scores to an already-exported trace in Langfuse (``score-create``).

    Evals call this so every eval check lands on the run's trace in the Langfuse
    UI: pass/fail checks as BOOLEAN scores, metric scores (e.g. DeepEval
    faithfulness) as NUMERIC. Each score dict: ``{name, value, comment?,
    dataType?}``. Best-effort like every exporter here - returns "sent", an
    "error: ..." string, or None when Langfuse isn't configured.
    """
    env = env or os.environ
    if not _lf_configured(env):
        return None
    auth = _lf_auth(env)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    batch = []
    for i, s in enumerate(scores):
        sid = f"{trace_id}-score-{i}"
        body = {"id": sid, "traceId": trace_id, "name": s["name"],
                "value": float(s["value"]), "dataType": s.get("dataType", "NUMERIC")}
        if s.get("comment"):
            body["comment"] = str(s["comment"])[:500]
        batch.append({"id": sid, "type": "score-create", "timestamp": now, "body": body})
    if not batch:
        return None
    try:
        host = env["LANGFUSE_HOST"].rstrip("/")
        _post(f"{host}/api/public/ingestion", {"Authorization": f"Basic {auth}"}, {"batch": batch})
        return "sent"
    except Exception as e:
        return f"error: {e}"


# ---- Langfuse datasets (pull items + link runs) ----------------------------
def fetch_dataset_items(dataset_name: str, env: Optional[dict] = None) -> list:
    """Fetch every item of a Langfuse dataset (following pagination).

    Reads the dataset the same way the UI does: ``GET
    /api/public/dataset-items?datasetName=<name>``. Each item carries ``id``,
    ``input``, ``expectedOutput``, ``metadata``, ``datasetName`` and
    ``sourceTraceId``. Returns [] when Langfuse isn't configured; raises only on
    an outright HTTP error (the caller reports it) — an *empty* dataset is a
    legitimate empty list, not an error.
    """
    import urllib.parse
    import urllib.request

    env = env or os.environ
    if not _lf_configured(env):
        return []
    host = env["LANGFUSE_HOST"].rstrip("/")
    auth = _lf_auth(env)
    items, page = [], 1
    while True:
        q = urllib.parse.urlencode({"datasetName": dataset_name, "page": page, "limit": 100})
        req = urllib.request.Request(
            f"{host}/api/public/dataset-items?{q}", method="GET",
            headers={"Authorization": f"Basic {auth}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            doc = json.loads(resp.read().decode())
        batch = doc.get("data") or []
        items.extend(batch)
        meta = doc.get("meta") or {}
        total_pages = meta.get("totalPages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    return items


def export_dataset_run_item(run_name: str, dataset_item_id: str, trace_id: str,
                            env: Optional[dict] = None, metadata: Optional[dict] = None,
                            run_description: Optional[str] = None) -> Optional[str]:
    """Link a run's trace to a dataset item under a named dataset run.

    ``POST /api/public/dataset-run-items`` with ``{runName, datasetItemId,
    traceId}`` (Langfuse recommends referencing the trace directly, so no
    ``observationId``). This is what surfaces the run under *Datasets → runs* in
    the Langfuse UI. Best-effort like the other exporters — returns "sent", an
    "error: ..." string, or None when Langfuse isn't configured.
    """
    env = env or os.environ
    if not _lf_configured(env):
        return None
    body = {"runName": run_name, "datasetItemId": dataset_item_id, "traceId": trace_id}
    if metadata is not None:
        body["metadata"] = metadata
    if run_description is not None:
        body["runDescription"] = run_description
    try:
        host = env["LANGFUSE_HOST"].rstrip("/")
        _post(f"{host}/api/public/dataset-run-items",
              {"Authorization": f"Basic {_lf_auth(env)}"}, body)
        return "sent"
    except Exception as e:
        return f"error: {e}"


def export_run(run: dict, env: Optional[dict] = None) -> dict:
    env = env or os.environ
    results = {}
    url = env.get("RYA_TRACE_WEBHOOK")
    if url:
        try:
            _post(url, {}, run_summary(run, env))
            results["webhook"] = "sent"
        except Exception as e:  # never fail a run because export failed
            results["webhook"] = f"error: {e}"
    if _lf_configured(env):
        try:
            _langfuse(run, env)
            results["langfuse"] = "sent"
        except Exception as e:
            results["langfuse"] = f"error: {e}"
    if env.get("RYA_OTLP_ENDPOINT"):
        try:
            _otlp(run, env)
            results["otlp"] = "sent"
        except Exception as e:
            results["otlp"] = f"error: {e}"
    return results
