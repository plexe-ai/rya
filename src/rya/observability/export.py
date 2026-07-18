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
from datetime import datetime, timezone
from typing import Optional

from .usage import run_usage

# Trace kinds that represent a model/LLM call (→ Langfuse GENERATION / OTLP gen_ai span).
_LLM_KINDS = ("llm.respond", "model.call")
_FAILED_KINDS = ("run.failed", "run.rejected")


def _post(url: str, headers: dict, payload: dict, timeout: int = 10) -> int:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


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


# ---- Langfuse --------------------------------------------------------------
def _langfuse(run: dict, env: dict) -> None:
    auth = base64.b64encode(f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()
    created = run.get("createdAt")
    rid = run["id"]
    trigger = run.get("trigger") or {}
    batch = [{
        "id": f"{rid}-trace",
        "type": "trace-create",
        "timestamp": created,
        "body": {"id": rid, "name": run.get("agent"),
                 "input": trigger, "output": run.get("result"),
                 "metadata": {"status": run.get("status"), "usage": run_usage(run, env),
                              "agentVersion": run.get("agentVersion")}},
    }]
    for ev in run.get("trace", []):
        seq, kind, label = ev.get("seq"), ev.get("kind", ""), ev.get("label")
        oid = f"{rid}-{seq}"
        ts = ev.get("ts", created)
        data = ev.get("data") or {}
        if kind in _LLM_KINDS:
            u = _usage_of(ev)
            body = {"id": oid, "traceId": rid, "type": "GENERATION", "name": label,
                    "startTime": ts, "endTime": ts, "model": _model_of(ev),
                    "input": data.get("input") or data.get("system"),
                    "output": _result_of(ev),
                    "usage": {"input": u["input"], "output": u["output"],
                              "total": u["input"] + u["output"], "unit": "TOKENS"}}
            otype = "generation-create"
        elif kind == "tool.call":
            body = {"id": oid, "traceId": rid, "type": "SPAN", "name": label,
                    "startTime": ts, "endTime": ts, "input": data.get("input"),
                    "output": _result_of(ev), "metadata": {"permission": data.get("permission")}}
            otype = "span-create"
        else:
            body = {"id": oid, "traceId": rid, "name": f"{kind}:{label}" if label else kind,
                    "startTime": ts, "metadata": data}
            otype = "event-create"
        batch.append({"id": oid, "type": otype, "timestamp": ts, "body": body})
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
    for i, ev in enumerate(trace):
        kind, label = ev.get("kind", ""), ev.get("label")
        start = _ns(ev.get("ts"), base)
        end = _ns(trace[i + 1].get("ts"), start) if i + 1 < len(trace) else start
        attrs = [_attr("rya.kind", kind), _attr("rya.label", label)]
        if kind in _LLM_KINDS:
            model = _model_of(ev)
            u = _usage_of(ev)
            attrs += [_attr("gen_ai.operation.name", "chat"), _attr("gen_ai.system", _provider_of(model)),
                      _attr("gen_ai.request.model", model), _attr("gen_ai.response.model", model),
                      _attr("gen_ai.usage.input_tokens", u["input"]),
                      _attr("gen_ai.usage.output_tokens", u["output"])]
        elif kind == "tool.call":
            attrs.append(_attr("rya.permission", (ev.get("data") or {}).get("permission")))
        spans.append({
            "traceId": trace_id, "spanId": _hex(f"{rid}:{ev.get('seq', i)}", 8),
            "parentSpanId": root_span, "name": label or kind, "kind": 1,
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
    if env.get("LANGFUSE_HOST") and env.get("LANGFUSE_PUBLIC_KEY") and env.get("LANGFUSE_SECRET_KEY"):
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
