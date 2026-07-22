"""The eval harness — declarative behavioural checks scored against real runs."""

import os

import pytest

from rya.cli import scaffold
from rya.evals import load_evals, run_evals
from rya.manifest import load_manifest
from rya.runtime import load_agent
from rya.store import Store


def _project(tmp_path):
    scaffold.write_project(tmp_path, "ev", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()
    return manifest, agent, store


def test_scaffold_ships_evals(tmp_path):
    scaffold.write_project(tmp_path, "ev", template="demo")
    assert (tmp_path / "rya.evals.yaml").exists()
    cases = load_evals(tmp_path)
    assert {c["id"] for c in cases} >= {"high_risk_pauses_for_approval", "handles_event_without_error"}


def test_default_evals_pass(tmp_path):
    manifest, agent, store = _project(tmp_path)
    rep = run_evals(manifest, agent, store, tmp_path)
    assert rep["hasEvals"] and rep["ok"] is True
    assert rep["passed"] == rep["total"] == 2
    high = next(r for r in rep["results"] if r["id"] == "high_risk_pauses_for_approval")
    assert high["status"] == "waiting_approval"
    assert all(c["pass"] for c in high["checks"])


def test_failing_expectation_is_reported(tmp_path):
    manifest, agent, store = _project(tmp_path)
    # overwrite with an eval that asserts the wrong status on purpose
    (tmp_path / "rya.evals.yaml").write_text(
        "evals:\n  - id: wrong_status\n    trigger:\n      type: message.received\n"
        "      payload: { email: risk@acme.io }\n    expect:\n      status: completed\n")
    rep = run_evals(manifest, agent, store, tmp_path)
    assert rep["ok"] is False and rep["failed"] == 1
    bad = rep["results"][0]
    assert bad["pass"] is False
    assert any(c["check"] == "status" and not c["pass"] for c in bad["checks"])


def test_only_filter_runs_one_case(tmp_path):
    manifest, agent, store = _project(tmp_path)
    rep = run_evals(manifest, agent, store, tmp_path, only="handles_event_without_error")
    assert rep["total"] == 1 and rep["results"][0]["id"] == "handles_event_without_error"


def test_no_evals_file_is_empty(tmp_path):
    manifest, agent, store = _project(tmp_path)
    (tmp_path / "rya.evals.yaml").unlink()  # remove after scaffolding
    rep = run_evals(manifest, agent, store, tmp_path)
    assert rep["hasEvals"] is False and rep["total"] == 0 and rep["ok"] is True


def test_deepeval_scorer_registered_and_skips_when_absent():
    """The deepeval scorer is wired in; with deepeval not installed it SKIPS
    (counts as pass) so evals stay runnable, and a bad metric name is rejected."""
    import importlib.util
    from rya.evals import SCORERS, _score_deepeval

    assert "deepeval" in SCORERS
    run = {"id": "r", "trigger": {"payload": {}},
           "trace": [{"kind": "llm.respond", "label": "m",
                      "data": {"result": {"text": "the capital is Paris"}}}]}
    if importlib.util.find_spec("deepeval") is None:
        ok, detail = _score_deepeval({"metric": "faithfulness", "threshold": 0.7}, run, {})
        assert ok is True and "not installed" in detail
    # unknown metric is a hard fail regardless of install state
    ok, detail = _score_deepeval({"metric": "nope"}, run, {})
    assert ok is False and "unknown deepeval metric" in detail


@pytest.mark.skipif(not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")),
                    reason="set ANTHROPIC_API_KEY or OPENAI_API_KEY to run the live DeepEval metric")
def test_deepeval_faithfulness_discriminates_live():
    """With a real provider key, the DeepEval scorer computes a genuine metric
    and tells a faithful answer apart from a hallucinated one."""
    from rya.evals import _score_deepeval
    ctx = ["France is a country in Europe. Its capital is Paris."]

    def run_with(ans):
        return {"trigger": {"payload": {}},
                "trace": [{"kind": "llm.respond", "label": "m", "data": {"result": {"text": ans}}}]}

    spec = {"metric": "faithfulness", "threshold": 0.7, "context": ctx, "input": "Capital of France?"}
    ok_good, _ = _score_deepeval(spec, run_with("The capital of France is Paris."), {})
    ok_bad, _ = _score_deepeval(spec, run_with("The capital of France is Berlin."), {})
    assert ok_good and not ok_bad


def test_eval_scores_export_to_langfuse(tmp_path, monkeypatch):
    """With LANGFUSE_* configured, every case's run exports as a trace (from the
    engine) and its checks land as scores on that trace."""
    from tests.test_export import _capture

    manifest, agent, store = _project(tmp_path)
    with _capture() as (url, captured):
        monkeypatch.setenv("LANGFUSE_HOST", url)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        rep = run_evals(manifest, agent, store, tmp_path)

    assert rep["ok"] and rep["langfuse"] == "enabled"
    assert all(r.get("langfuse") == "sent" for r in rep["results"])

    batches = [body["batch"] for path, body in captured if path == "/api/public/ingestion"]
    flat = [item for b in batches for item in b]
    # engine exported each eval run as a trace...
    traces = [i for i in flat if i["type"] == "trace-create"]
    assert len(traces) == rep["total"]
    # ...and the harness attached scores to those same trace ids
    scores = [i for i in flat if i["type"] == "score-create"]
    assert {s["body"]["traceId"] for s in scores} <= {t["body"]["id"] for t in traces}
    names = {s["body"]["name"] for s in scores}
    assert "eval:high_risk_pauses_for_approval" in names
    assert "high_risk_pauses_for_approval:approval_requested" in names
    verdicts = [s for s in scores if s["body"]["name"].startswith("eval:")]
    assert all(s["body"]["value"] == 1.0 and s["body"]["dataType"] == "BOOLEAN" for s in verdicts)


def test_numeric_scorer_value_exports_as_numeric(tmp_path, monkeypatch):
    """A scorer returning (ok, detail, value) - like deepeval - exports NUMERIC."""
    import rya.evals as ev
    from tests.test_export import _capture

    manifest, agent, store = _project(tmp_path)
    (tmp_path / "rya.evals.yaml").write_text(
        "evals:\n  - id: metric_case\n    trigger:\n      type: message.received\n"
        "      payload: { email: a@b.co }\n    expect:\n      fake_metric: 0.5\n")
    monkeypatch.setitem(ev.SCORERS, "fake_metric",
                        lambda expected, run, env: (True, "fake score=0.83", 0.83))
    with _capture() as (url, captured):
        monkeypatch.setenv("LANGFUSE_HOST", url)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        rep = run_evals(manifest, agent, store, tmp_path)

    assert rep["ok"]
    case = rep["results"][0]
    assert case["checks"][0]["value"] == 0.83
    flat = [i for _, body in captured for i in body["batch"]]
    metric = next(i for i in flat if i["type"] == "score-create"
                  and i["body"]["name"] == "metric_case:fake_metric")
    assert metric["body"]["value"] == 0.83 and metric["body"]["dataType"] == "NUMERIC"


def test_approval_inside_a_job_resumes_the_job_handler(tmp_path):
    """Platform regression (found by the loan-renewal build): a run paused on an
    approval INSIDE a @agent.job handler must resume through that job handler,
    not the event handler."""
    from rya.runtime import Engine
    manifest, agent, store = _project(tmp_path)

    (tmp_path / "src" / "agent.py").write_text('''
from rya import define_agent
agent = define_agent()

@agent.tool("side.effect")
async def side_effect(input):
    return {"ok": True, "wrote": input.get("value")}

@agent.on_event
async def main(ctx, event):
    await ctx.jobs.schedule("gated_job", {"value": 42})
    return {"scheduled": True}

@agent.job("gated_job")
async def gated_job(ctx, job):
    await ctx.approvals.request(title="write?", body="value",
                                action={"tool": "side.effect",
                                        "input": {"value": job.payload["value"]}})
    return {"resumed": True, "value": job.payload["value"]}
''')
    from rya.runtime import load_agent as _load
    agent = _load(manifest, tmp_path)
    engine = Engine(manifest, agent, store, tmp_path)

    engine.run_event("message.received", {"email": "a@x.co"})
    ran = engine.work_once()
    assert ran and ran[0]["status"] == "waiting_approval"

    approval = store.list_approvals(status="pending")[0]
    done = engine.approve(approval["id"])
    assert done["status"] == "completed", done.get("error")
    assert done["output"] == {"resumed": True, "value": 42}
    # and the approved action ACTUALLY executed (async @agent.tool handler)
    resolved = store.get_approval(approval["id"])
    assert resolved["actionResult"] == {"ok": True, "wrote": 42}
