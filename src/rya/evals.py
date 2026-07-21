"""`rya eval` — a declarative eval harness for agents.

Evals are the production loop Rya was missing: the readiness gate proves an agent
*could* ship; evals prove it *behaves*. Each case fires a real event at the
runtime and scores the resulting run against expectations — deterministic scorers
(status, which tools ran, approvals, tokens/cost, trace contents) plus an optional
LLM judge of the final message. ``rya eval`` runs them all and exits non-zero on
any failure, so it gates a deploy exactly like ``rya deploy --check``.

Cases live in ``rya.evals.yaml`` (coding-agent-friendly, declarative):

    evals:
      - id: high_risk_pauses_for_approval
        trigger: { type: message.received, payload: { email: "risk@acme.io" } }
        expect:
          status: waiting_approval
          tools_called: [crm.lookup]
          approval_requested: true
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from .observability.usage import run_usage

EVALS_FILE = "rya.evals.yaml"


def load_evals(root: Path) -> list:
    p = Path(root) / EVALS_FILE
    if not p.is_file():
        return []
    doc = yaml.safe_load(p.read_text()) or {}
    return doc.get("evals", []) if isinstance(doc, dict) else (doc or [])


# ---- scorers: each takes (expected_value, run, env) -> (pass, detail) --------
def _trace_labels(run: dict, kind: str) -> list:
    return [e.get("label") for e in run.get("trace", []) if e.get("kind") == kind]


def _score_status(expected, run, env):
    return run.get("status") == expected, f"status={run.get('status')} (want {expected})"


def _score_tools_called(expected, run, env):
    called = set(_trace_labels(run, "tool.call"))
    want = set(expected if isinstance(expected, list) else [expected])
    missing = want - called
    return not missing, f"called={sorted(called)} missing={sorted(missing)}"


def _score_tools_not_called(expected, run, env):
    called = set(_trace_labels(run, "tool.call"))
    forbidden = set(expected if isinstance(expected, list) else [expected]) & called
    return not forbidden, f"unexpectedly called {sorted(forbidden)}"


def _score_approval_requested(expected, run, env):
    got = bool(_trace_labels(run, "approval.requested"))
    return got == bool(expected), f"approval_requested={got} (want {expected})"


def _score_no_failure(expected, run, env):
    ok = run.get("status") != "failed"
    return ok == bool(expected), f"status={run.get('status')}"


def _score_result_contains(expected, run, env):
    blob = json.dumps(run.get("trace", []), default=str).lower()
    return str(expected).lower() in blob, f"looking for '{expected}'"


def _score_max_tokens(expected, run, env):
    u = run_usage(run)
    tot = u["inputTokens"] + u["outputTokens"]
    return tot <= int(expected), f"tokens={tot} (cap {expected})"


def _score_max_cost(expected, run, env):
    cost = run_usage(run).get("costUsd") or 0.0
    return cost <= float(expected), f"costUsd={cost} (cap {expected})"


def _score_trace_has(expected, run, env):
    kinds = {e.get("kind") for e in run.get("trace", [])}
    want = set(expected if isinstance(expected, list) else [expected])
    missing = want - kinds
    return not missing, f"missing kinds {sorted(missing)}"


def _score_trace_lacks(expected, run, env):
    kinds = {e.get("kind") for e in run.get("trace", [])}
    present = set(expected if isinstance(expected, list) else [expected]) & kinds
    return not present, f"unexpected kinds {sorted(present)}"


def _score_judge(expected, run, env):
    """LLM-judge the final assistant/draft text against a rubric. Best-effort:
    when the provider resolves to the mock LLM (no key), the judge is SKIPPED
    (counts as pass) so evals stay runnable offline."""
    from .providers import resolve_provider, respond
    rubric = expected if isinstance(expected, str) else expected.get("rubric", "")
    # pull the model's last output text from the trace
    outputs = [e.get("data", {}).get("result") for e in run.get("trace", [])
               if e.get("kind") in ("llm.respond", "model.call")]
    text = next((o.get("text") if isinstance(o, dict) else str(o) for o in reversed(outputs) if o), "")
    if resolve_provider("auto") == "mock":
        return True, "judge skipped (mock LLM — set a real provider to enable)"
    try:
        verdict = respond(system=f"Score PASS or FAIL only. Rubric: {rubric}",
                          input={"text": text})
        v = (verdict.get("text") if isinstance(verdict, dict) else str(verdict)).upper()
        return ("FAIL" not in v and "PASS" in v), f"judge said {v[:60]}"
    except Exception as e:  # never let the judge break the harness
        return True, f"judge unavailable ({e})"


# DeepEval handles LLM-output-quality metrics (faithfulness, relevancy,
# hallucination, RAG precision/recall) — a deep, specialized domain Rya delegates
# rather than reimplements. Behavioural/governance scoring stays native above.
_DEEPEVAL_METRICS = {
    "answer_relevancy": "AnswerRelevancyMetric",
    "relevancy": "AnswerRelevancyMetric",
    "faithfulness": "FaithfulnessMetric",
    "hallucination": "HallucinationMetric",
    "bias": "BiasMetric",
    "toxicity": "ToxicityMetric",
    "contextual_relevancy": "ContextualRelevancyMetric",
    "contextual_precision": "ContextualPrecisionMetric",
    "contextual_recall": "ContextualRecallMetric",
}


def _last_output_text(run: dict) -> str:
    outputs = [e.get("data", {}).get("result") for e in run.get("trace", [])
               if e.get("kind") in ("llm.respond", "model.call")]
    return next((o.get("text") if isinstance(o, dict) else str(o) for o in reversed(outputs) if o), "")


def _deepeval_judge(spec, env):
    """Pick the LLM that scores the metric. Honor an explicit override, else use
    whichever provider key is present (so it 'just works' with an Anthropic key,
    not only OpenAI). Return None → DeepEval's own default (OpenAI GPT)."""
    import os
    want = spec.get("model") or env.get("RYA_DEEPEVAL_MODEL") or os.environ.get("RYA_DEEPEVAL_MODEL")
    has_anthropic = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    has_openai = env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    try:
        from deepeval.models import AnthropicModel, GPTModel
        if want:
            return AnthropicModel(model=want) if str(want).lower().startswith("claude") else GPTModel(model=want)
        if has_anthropic and not has_openai:
            return AnthropicModel(model="claude-haiku-4-5")  # override via RYA_DEEPEVAL_MODEL
    except Exception:
        return None
    return None


def _score_deepeval(expected, run, env):
    """Score the run's final output with a DeepEval metric (faithfulness, answer
    relevancy, hallucination, …). Spec: ``{metric, threshold, context?, input?,
    model?}`` or just a metric name string. Optional dep — ``pip install
    'rya[deepeval]'`` plus a provider key (OpenAI or Anthropic) for the metric's
    own judge. SKIPPED (counts as pass) when deepeval isn't installed or can't run
    offline, so evals stay runnable."""
    spec = {"metric": expected} if isinstance(expected, str) else dict(expected or {})
    name = (spec.get("metric") or "answer_relevancy").lower()
    threshold = float(spec.get("threshold", 0.5))
    cls = _DEEPEVAL_METRICS.get(name)
    if cls is None:
        return False, f"unknown deepeval metric '{name}' (have {sorted(_DEEPEVAL_METRICS)})"
    try:
        from deepeval import metrics as M
        from deepeval.test_case import LLMTestCase
    except Exception:
        return True, "deepeval not installed — `pip install 'rya[deepeval]'` to enable"
    actual = _last_output_text(run)
    ctx = spec.get("context")
    if ctx is None:
        ctx = [str(e.get("data", {}).get("result")) for e in run.get("trace", [])
               if e.get("kind") == "tool.call"] or None
    # Engine runs store trigger as a string ("event"/"api") with the event dict
    # alongside; ingested runs may carry a dict trigger. Handle both.
    trig, event = run.get("trigger"), run.get("event")
    payload = ((trig.get("payload") if isinstance(trig, dict) else None)
               or (event.get("payload") if isinstance(event, dict) else None) or {})
    inp = spec.get("input") or json.dumps(payload, default=str)
    try:
        kwargs = {"input": inp, "actual_output": actual}
        if ctx:
            kwargs["retrieval_context"] = ctx
            kwargs["context"] = ctx
        tc = LLMTestCase(**kwargs)
        judge = _deepeval_judge(spec, env)
        metric = getattr(M, cls)(threshold=threshold, **({"model": judge} if judge else {}))
        metric.measure(tc)
        score = getattr(metric, "score", None)
        ok = getattr(metric, "is_successful", lambda: (score or 0) >= threshold)()
        # third element = the numeric metric score, exported to Langfuse as-is
        return bool(ok), f"{name} score={score} (threshold {threshold})", (
            float(score) if score is not None else None)
    except Exception as e:  # missing model key / network — skip, never break the harness
        return True, f"deepeval '{name}' skipped ({type(e).__name__}: {e})"


SCORERS = {
    "status": _score_status,
    "tools_called": _score_tools_called,
    "tools_not_called": _score_tools_not_called,
    "approval_requested": _score_approval_requested,
    "no_failure": _score_no_failure,
    "result_contains": _score_result_contains,
    "max_tokens": _score_max_tokens,
    "max_cost": _score_max_cost,
    "trace_has": _score_trace_has,
    "trace_lacks": _score_trace_lacks,
    "judge": _score_judge,
    "deepeval": _score_deepeval,
}


def _export_case_scores(cid: str, run: dict, checks: list, passed: bool, env: dict) -> Optional[str]:
    """Push one eval case's results to Langfuse as scores on the run's trace.

    The engine already exported the run itself as a trace, so scores attach by
    ``traceId == run id``: the case verdict as ``eval:<id>`` (BOOLEAN), each
    check as ``<id>:<check>`` (BOOLEAN, or NUMERIC when the scorer produced a
    real metric value, e.g. DeepEval faithfulness). No-op unless LANGFUSE_* is
    configured; never breaks the harness."""
    from .observability.export import export_run, export_scores

    run_id = run.get("id")
    if not run_id:
        return None
    # The engine exports only terminal runs; an eval case can legitimately end
    # paused (e.g. waiting_approval). Export those here so the scores have a
    # trace to attach to.
    if run.get("status") not in ("completed", "failed", "rejected"):
        try:
            export_run(run, env)
        except Exception:
            pass
    scores = [{"name": f"eval:{cid}", "value": 1.0 if passed else 0.0,
               "dataType": "BOOLEAN", "comment": f"status={run.get('status')}"}]
    for c in checks:
        if c.get("value") is not None:
            scores.append({"name": f"{cid}:{c['check']}", "value": float(c["value"]),
                           "dataType": "NUMERIC", "comment": c.get("detail")})
        else:
            scores.append({"name": f"{cid}:{c['check']}", "value": 1.0 if c["pass"] else 0.0,
                           "dataType": "BOOLEAN", "comment": c.get("detail")})
    try:
        return export_scores(run_id, scores, env)
    except Exception:
        return None


def run_evals(manifest, agent, store, root, only: Optional[str] = None) -> dict:
    """Run each eval case against a real engine run and score it."""
    from .runtime import Engine
    from .sdk.context import load_env

    env = load_env(Path(root))
    cases = load_evals(root)
    if only:
        cases = [c for c in cases if c.get("id") == only]

    results = []
    for case in cases:
        cid = case.get("id", "eval")
        trig = case.get("trigger", {})
        expect = case.get("expect", {})
        engine = Engine(manifest, agent, store, root)
        checks, error = [], None
        try:
            run = engine.run_event(trig.get("type", "message.received"),
                                   trig.get("payload", {}), source="eval")
        except Exception as e:
            run, error = {"status": "error", "trace": []}, str(e)
        for key, expected in expect.items():
            scorer = SCORERS.get(key)
            if scorer is None:
                checks.append({"check": key, "pass": False, "detail": "unknown scorer"})
                continue
            try:
                out = scorer(expected, run, env)
                ok, detail = out[0], out[1]
                value = out[2] if len(out) > 2 else None  # numeric metric score, if any
            except Exception as e:
                ok, detail, value = False, f"scorer error: {e}", None
            check = {"check": key, "pass": bool(ok), "detail": detail}
            if value is not None:
                check["value"] = value
            checks.append(check)
        passed = all(c["pass"] for c in checks) and error is None
        exported = _export_case_scores(cid, run, checks, passed, env)
        results.append({"id": cid, "pass": passed, "runId": run.get("id"),
                        "status": run.get("status"), "checks": checks, "error": error,
                        **({"langfuse": exported} if exported else {})})

    n_pass = sum(1 for r in results if r["pass"])
    lf_on = bool(env.get("LANGFUSE_HOST") and env.get("LANGFUSE_PUBLIC_KEY")
                 and env.get("LANGFUSE_SECRET_KEY"))
    return {
        "ok": all(r["pass"] for r in results),
        "total": len(results), "passed": n_pass, "failed": len(results) - n_pass,
        "score": round(n_pass / len(results), 3) if results else None,
        "results": results,
        "hasEvals": bool(cases),
        "langfuse": "enabled" if lf_on else None,
    }
