"""Trace export → Langfuse (nested observations), OTLP (GenAI spans), webhook.

Each backend is exercised against a mock HTTP server that captures the exact
payload Rya emits, so we assert the wire format, not just that a call happened.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from rya.observability.export import export_run

# A representative completed run: a tool call + an LLM generation carrying usage.
RUN = {
    "id": "run_abc123",
    "agent": "followup",
    "agentVersion": "1",
    "status": "completed",
    "trigger": {"type": "message.received", "payload": {"email": "x@y.co"}},
    "createdAt": "2026-06-21T10:00:00Z",
    "result": {"ok": True},
    "trace": [
        {"seq": 0, "ts": "2026-06-21T10:00:00Z", "kind": "run.started", "label": "begin", "data": {}},
        {"seq": 1, "ts": "2026-06-21T10:00:01Z", "kind": "tool.call", "label": "crm.lookup",
         "data": {"input": {"email": "x@y.co"}, "permission": "auto", "result": {"plan": "pro"}}},
        {"seq": 2, "ts": "2026-06-21T10:00:02Z", "kind": "llm.respond", "label": "claude-haiku-4-5",
         "data": {"system": "draft a reply",
                  "result": {"text": "Hello there", "model": "claude-haiku-4-5",
                             "usage": {"input": 120, "output": 45}}}},
    ],
}


@contextmanager
def _capture():
    """A mock HTTP server that records every POST as (path, parsed-json-body)."""
    captured = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            try:
                payload = json.loads(body)
            except Exception:
                payload = None
            captured.append((self.path, payload))
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", captured
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


def test_webhook_posts_run_summary():
    with _capture() as (url, captured):
        res = export_run(RUN, {"RYA_TRACE_WEBHOOK": url})
    assert res == {"webhook": "sent"}
    (path, body), = captured
    assert body["runId"] == "run_abc123"
    assert body["usage"] == {"inputTokens": 120, "outputTokens": 45, "costUsd": None}


def test_langfuse_emits_nested_observations_with_usage():
    with _capture() as (url, captured):
        res = export_run(RUN, {"LANGFUSE_HOST": url, "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    assert res == {"langfuse": "sent"}
    (path, body), = captured
    assert path == "/api/public/ingestion"
    batch = body["batch"]
    types = [item["type"] for item in batch]
    assert types[0] == "trace-create" and batch[0]["body"]["id"] == "run_abc123"
    assert "generation-create" in types and "span-create" in types
    gen = next(i for i in batch if i["type"] == "generation-create")
    assert gen["body"]["model"] == "claude-haiku-4-5"
    assert gen["body"]["usage"] == {"input": 120, "output": 45, "total": 165, "unit": "TOKENS"}
    span = next(i for i in batch if i["type"] == "span-create")
    assert span["body"]["name"] == "crm.lookup" and span["body"]["metadata"]["permission"] == "auto"


def test_otlp_emits_genai_spans():
    with _capture() as (url, captured):
        res = export_run(RUN, {"RYA_OTLP_ENDPOINT": url})
    assert res == {"otlp": "sent"}
    (path, body), = captured
    assert path == "/v1/traces"                       # endpoint normalized to OTLP traces path
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 4                            # 1 root + 3 trace steps
    assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)
    llm = next(s for s in spans if s["name"] == "claude-haiku-4-5")
    attrs = {a["key"]: list(a["value"].values())[0] for a in llm["attributes"]}
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-haiku-4-5"
    assert attrs["gen_ai.usage.input_tokens"] == "120"   # int64 → string per OTLP JSON
    assert attrs["gen_ai.usage.output_tokens"] == "45"
    # child spans parent onto the root run span
    root = next(s for s in spans if "parentSpanId" not in s)
    assert all(s["parentSpanId"] == root["spanId"] for s in spans if "parentSpanId" in s)


def test_all_three_fire_together():
    with _capture() as (url, captured):
        res = export_run(RUN, {"RYA_TRACE_WEBHOOK": url, "LANGFUSE_HOST": url,
                               "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk",
                               "RYA_OTLP_ENDPOINT": url})
    assert res == {"webhook": "sent", "langfuse": "sent", "otlp": "sent"}
    assert len(captured) == 3


def test_nothing_exports_when_unconfigured():
    assert export_run(RUN, {}) == {}


def test_export_is_best_effort_never_raises():
    # dead port → each backend records an error string, the run is unaffected
    dead = "http://127.0.0.1:1"
    res = export_run(RUN, {"RYA_TRACE_WEBHOOK": dead, "RYA_OTLP_ENDPOINT": dead})
    assert res["webhook"].startswith("error:") and res["otlp"].startswith("error:")


def test_real_engine_run_exports_to_otlp_and_langfuse(tmp_path, monkeypatch):
    """End-to-end: a real engine run (not a synthetic dict) auto-exports on
    terminal status, and its actual trace produces an OTLP gen_ai span + a
    Langfuse generation for the LLM call."""
    from rya.manifest import load_manifest
    from rya.runtime import Engine, load_agent
    from rya.store import Store

    (tmp_path / "rya.agent.yaml").write_text("name: t\nruntime: python\nentrypoint: agent.py\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    await ctx.llm.respond(system='reply', input={'q': 'hi'})\n"
        "    return 'ok'\n"
    )
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)

    with _capture() as (url, captured):
        monkeypatch.setenv("RYA_OTLP_ENDPOINT", url)
        monkeypatch.setenv("LANGFUSE_HOST", url)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        run = Engine(manifest, agent, Store(tmp_path), tmp_path).run_event("message.received", {"q": 1})

    assert run["status"] == "completed"
    paths = [p for p, _ in captured]
    assert "/v1/traces" in paths and "/api/public/ingestion" in paths
    otlp = next(b for p, b in captured if p == "/v1/traces")
    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert any(a["key"] == "gen_ai.operation.name" for s in spans for a in s["attributes"])
    lf = next(b for p, b in captured if p == "/api/public/ingestion")
    assert any(i["type"] == "generation-create" for i in lf["batch"])
