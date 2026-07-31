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
    # A single root span carries the whole run; every other step nests under it
    # (parentObservationId set) so the UI renders one collapsible chain, not N
    # flat siblings of the trace.
    root = next(i for i in batch
                if i["type"] == "span-create" and not i["body"].get("parentObservationId"))
    assert root["body"]["name"] == "followup run"
    tool = next(i for i in batch
                if i["type"] == "span-create" and i["body"]["name"] == "crm.lookup")
    assert tool["body"]["metadata"]["permission"] == "auto"
    assert gen["body"]["parentObservationId"] == root["body"]["id"]
    assert tool["body"]["parentObservationId"] == root["body"]["id"]
    steps = [i for i in batch if i["type"] in ("generation-create", "span-create", "event-create")]
    assert all(s["body"].get("parentObservationId")
               for s in steps if s["body"]["id"] != root["body"]["id"])


def test_langfuse_agent_loop_steps_are_generations_with_message_input():
    """The governed agent loop emits `llm.chat` steps (not `llm.respond`). Each
    must export as a GENERATION carrying the real prompt (system + messages) and
    usage, and a tool called during a step must nest under that step - the
    classic `llm step -> its tool -> next llm step` chain."""
    loop_run = {
        "id": "run_loop1", "agent": "counsellor", "agentVersion": "1",
        "status": "completed", "trigger": {"type": "message.received", "payload": {}},
        "createdAt": "2026-06-21T10:00:00Z", "result": {"text": "done"},
        "trace": [
            {"seq": 0, "ts": "2026-06-21T10:00:00Z", "kind": "llm.chat", "label": "step 0",
             "data": {"system": "you are helpful",
                      "messages": [{"role": "user", "content": "look up x"}],
                      "result": {"text": "", "model": "claude-sonnet-4-6",
                                 "toolCalls": [{"id": "c1", "name": "crm.lookup", "input": {}}],
                                 "usage": {"input": 500, "output": 12}}}},
            {"seq": 1, "ts": "2026-06-21T10:00:00Z", "kind": "tool.call", "label": "crm.lookup",
             "data": {"input": {}, "permission": "auto", "result": {"plan": "pro"}}},
            {"seq": 2, "ts": "2026-06-21T10:00:00Z", "kind": "llm.chat", "label": "step 1",
             "data": {"system": "you are helpful",
                      "messages": [{"role": "user", "content": "look up x"},
                                   {"role": "assistant", "content": "", "toolCalls": [{"id": "c1"}]},
                                   {"role": "tool", "content": {"plan": "pro"}}],
                      "result": {"text": "done", "model": "claude-sonnet-4-6",
                                 "usage": {"input": 620, "output": 40}}}},
        ],
    }
    with _capture() as (url, captured):
        export_run(loop_run, {"LANGFUSE_HOST": url, "LANGFUSE_PUBLIC_KEY": "pk",
                              "LANGFUSE_SECRET_KEY": "sk"})
    (_path, body), = captured
    batch = body["batch"]
    gens = [i for i in batch if i["type"] == "generation-create"]
    assert len(gens) == 2 and all(g["body"]["model"] == "claude-sonnet-4-6" for g in gens)
    step0 = next(g for g in gens if g["body"]["name"] == "step 0")
    # input is the real prompt: system prepended to the sent messages.
    assert step0["body"]["input"][0] == {"role": "system", "content": "you are helpful"}
    assert step0["body"]["input"][1] == {"role": "user", "content": "look up x"}
    assert step0["body"]["output"]["toolCalls"][0]["name"] == "crm.lookup"
    # the tool span nests under the step that called it, not the root.
    tool = next(i for i in batch if i["type"] == "span-create" and i["body"]["name"] == "crm.lookup")
    assert tool["body"]["parentObservationId"] == step0["body"]["id"]
    # trace-level usage now includes the whole loop (500+620 in, 12+40 out).
    assert batch[0]["body"]["metadata"]["usage"] == {
        "inputTokens": 1120, "outputTokens": 52, "costUsd": None}


def _lf_batch(run):
    with _capture() as (url, cap):
        export_run(run, {"LANGFUSE_HOST": url, "LANGFUSE_PUBLIC_KEY": "pk",
                         "LANGFUSE_SECRET_KEY": "sk"})
    (_path, body), = cap
    return body["batch"]


# A run in the current recording format: real ms timings, a loopId-tagged agent
# loop, bookkeeping steps, and the handler's return on `output` (not `result`).
TIMED_RUN = {
    "id": "run_t1", "agent": "counsellor", "agentVersion": "1", "status": "completed",
    "trigger": {"type": "message.received", "payload": {}}, "userId": "counsellor@x.co",
    "createdAt": "2026-06-21T10:00:00Z", "output": {"reply": "all done"},
    "trace": [
        {"seq": 0, "ts": "2026-06-21T10:00:00Z", "kind": "run.started", "label": "begin",
         "data": {}, "startedAt": "2026-06-21T10:00:00.000Z", "endedAt": "2026-06-21T10:00:00.000Z"},
        {"seq": 1, "ts": "2026-06-21T10:00:00Z", "kind": "session.get_or_create", "label": "web:a@b.co",
         "data": {"result": {"id": "ses_1"}},
         "startedAt": "2026-06-21T10:00:00.010Z", "endedAt": "2026-06-21T10:00:00.014Z"},
        {"seq": 2, "ts": "2026-06-21T10:00:00Z", "kind": "memory.get", "label": "user:tenantId",
         "data": {"result": "acme"},
         "startedAt": "2026-06-21T10:00:00.015Z", "endedAt": "2026-06-21T10:00:00.016Z"},
        {"seq": 3, "ts": "2026-06-21T10:00:01Z", "kind": "llm.chat", "label": "step 0",
         "data": {"system": "sys", "messages": [{"role": "user", "content": "hi"}],
                  "loopId": "loop_a",
                  "modelParameters": {"temperature": 0.2, "max_tokens": 8192,
                                      "provider": None, "route": "compose"},
                  "result": {"text": "", "model": "claude-sonnet-4-6",
                             "toolCalls": [{"id": "c1", "name": "crm.lookup", "input": {}}],
                             "usage": {"input": 500, "output": 12}}},
         "startedAt": "2026-06-21T10:00:01.000Z", "endedAt": "2026-06-21T10:00:03.500Z"},
        {"seq": 4, "ts": "2026-06-21T10:00:03Z", "kind": "tool.call", "label": "crm.lookup",
         "data": {"input": {"q": 1}, "permission": "auto", "loopId": "loop_a",
                  "result": {"plan": "pro"}},
         "startedAt": "2026-06-21T10:00:03.500Z", "endedAt": "2026-06-21T10:00:03.560Z"},
        {"seq": 5, "ts": "2026-06-21T10:00:04Z", "kind": "llm.chat", "label": "step 1",
         "data": {"system": "sys", "messages": [{"role": "user", "content": "hi"}],
                  "loopId": "loop_a",
                  "result": {"text": "all done", "model": "claude-sonnet-4-6",
                             "usage": {"input": 620, "output": 40}}},
         "startedAt": "2026-06-21T10:00:03.600Z", "endedAt": "2026-06-21T10:00:05.900Z"},
    ],
}


def test_langfuse_bookkeeping_has_io_and_debug_level():
    """The user-visible complaint: memory/session observations rendered as empty
    rows. They must carry real input/output, be SPANs (so they get a duration),
    and sit at DEBUG level under one collapsed `context` span."""
    batch = _lf_batch(TIMED_RUN)
    by_name = {i["body"].get("name"): i for i in batch}
    mem = by_name["memory.get:user:tenantId"]
    assert mem["type"] == "span-create"          # SPAN, not EVENT → can carry endTime
    assert mem["body"]["output"] == "acme"       # was null before
    assert mem["body"]["level"] == "DEBUG"
    assert mem["body"]["endTime"] == "2026-06-21T10:00:00.016Z"
    ses = by_name["session.get_or_create:web:a@b.co"]
    assert ses["body"]["output"] == {"id": "ses_1"}
    # both nest under the single collapsed context span
    ctx = next(i for i in batch if str(i["body"].get("name", "")).startswith("context ("))
    assert ctx["body"]["level"] == "DEBUG"
    assert mem["body"]["parentObservationId"] == ctx["body"]["id"]
    assert ses["body"]["parentObservationId"] == ctx["body"]["id"]


def test_langfuse_uses_real_durations_not_synthetic_ones():
    """Timings must come from the recorded wall-clock span. Deriving them from
    `seq` (as an earlier revision did) made every step 1ms and the whole run
    ~0.02s, which silently destroys the latency view."""
    batch = _lf_batch(TIMED_RUN)
    step0 = next(i for i in batch if i["body"].get("name") == "step 0")
    assert step0["body"]["startTime"] == "2026-06-21T10:00:01.000Z"
    assert step0["body"]["endTime"] == "2026-06-21T10:00:03.500Z"   # 2.5s, not 1ms
    root = next(i for i in batch if i["body"].get("name") == "counsellor run")
    assert root["body"]["startTime"] == "2026-06-21T10:00:00.000Z"
    assert root["body"]["endTime"] == "2026-06-21T10:00:05.900Z"    # spans the whole run


def test_langfuse_agent_loop_span_and_trace_fields():
    batch = _lf_batch(TIMED_RUN)
    trace = batch[0]["body"]
    # output comes from run["output"] - run["result"] was never populated by the
    # engine, which is why traces used to show a null output.
    assert trace["output"] == {"reply": "all done"}
    assert trace["sessionId"] == "ses_1"          # enables Langfuse's Sessions view
    assert trace["userId"] == "counsellor@x.co"
    assert "counsellor" in trace["tags"]
    loop = next(i for i in batch if i["body"].get("name") == "agent loop")
    steps = [i for i in batch if i["body"].get("name") in ("step 0", "step 1")]
    assert all(s["body"]["parentObservationId"] == loop["body"]["id"] for s in steps)
    assert loop["body"]["startTime"] == "2026-06-21T10:00:01.000Z"
    assert loop["body"]["endTime"] == "2026-06-21T10:00:05.900Z"
    # model parameters ride along on the generation, minus unset entries
    assert steps[0]["body"]["modelParameters"] == {"temperature": 0.2, "max_tokens": 8192,
                                                   "route": "compose"}


def test_langfuse_failed_run_is_error_level():
    failed = dict(TIMED_RUN, id="run_err", status="failed", output=None,
                  trace=TIMED_RUN["trace"] + [
                      {"seq": 6, "ts": "2026-06-21T10:00:06Z", "kind": "run.failed",
                       "label": "E_RUNTIME", "data": {"message": "boom"},
                       "startedAt": "2026-06-21T10:00:06.000Z",
                       "endedAt": "2026-06-21T10:00:06.000Z"}])
    batch = _lf_batch(failed)
    err = next(i for i in batch if i["body"].get("name") == "run.failed:E_RUNTIME")
    assert err["type"] == "event-create"          # instantaneous marker
    assert err["body"]["level"] == "ERROR"
    assert err["body"]["statusMessage"] == "boom"
    root = next(i for i in batch if i["body"].get("name") == "counsellor run")
    assert root["body"]["level"] == "ERROR"


def test_langfuse_still_exports_runs_recorded_before_timing_existed():
    """Old runs have no startedAt/endedAt/loopId - they must still export, using
    the seq-derived ordering fallback."""
    batch = _lf_batch(RUN)                        # the legacy-format fixture
    gen = next(i for i in batch if i["type"] == "generation-create")
    assert gen["body"]["startTime"] and gen["body"]["endTime"]
    assert not any(i["body"].get("name") == "agent loop" for i in batch)


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


def test_export_scores_ships_boolean_and_numeric():
    from rya.observability.export import export_scores
    scores = [
        {"name": "eval:high_risk_pauses", "value": 1.0, "dataType": "BOOLEAN", "comment": "status=waiting_approval"},
        {"name": "high_risk_pauses:deepeval", "value": 0.83, "dataType": "NUMERIC", "comment": "faithfulness score=0.83"},
    ]
    with _capture() as (url, captured):
        res = export_scores("run_abc123", scores,
                            {"LANGFUSE_HOST": url, "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    assert res == "sent"
    (path, body), = captured
    assert path == "/api/public/ingestion"
    batch = body["batch"]
    assert [i["type"] for i in batch] == ["score-create", "score-create"]
    assert all(i["body"]["traceId"] == "run_abc123" for i in batch)
    b0, b1 = batch[0]["body"], batch[1]["body"]
    assert b0["name"] == "eval:high_risk_pauses" and b0["value"] == 1.0 and b0["dataType"] == "BOOLEAN"
    assert b1["value"] == 0.83 and b1["dataType"] == "NUMERIC" and "faithfulness" in b1["comment"]


def test_export_scores_noop_without_config_and_survives_error():
    from rya.observability.export import export_scores
    assert export_scores("r1", [{"name": "x", "value": 1}], {}) is None
    # unreachable host -> "error: ...", never an exception
    res = export_scores("r1", [{"name": "x", "value": 1}],
                        {"LANGFUSE_HOST": "http://127.0.0.1:9", "LANGFUSE_PUBLIC_KEY": "pk",
                         "LANGFUSE_SECRET_KEY": "sk"})
    assert res.startswith("error:")
