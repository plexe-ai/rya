"""The eval harness — declarative behavioural checks scored against real runs."""

from rya.cli import scaffold
from rya.evals import load_evals, run_evals
from rya.manifest import load_manifest
from rya.runtime import load_agent
from rya.store import Store


def _project(tmp_path):
    scaffold.write_project(tmp_path, "ev")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()
    return manifest, agent, store


def test_scaffold_ships_evals(tmp_path):
    scaffold.write_project(tmp_path, "ev")
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
