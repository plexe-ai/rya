"""External run ingest: an agent loop running OUTSIDE Rya ships its traces in,
so Runs & traces is the single pane during a sidecar migration."""

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold

CSA_SHAPED_TRACE = [
    {"kind": "run.started", "label": "chat turn", "data": {}},
    {"kind": "tool.call", "label": "cams_lookup_student",
     "data": {"input": {"camsId": "SAMPLE-001"}, "result": {"name": "Sample Student"}}},
    {"kind": "llm.respond", "label": "compose:claude-sonnet-4-6",
     "data": {"result": {"model": "claude-sonnet-4-6",
                         "usage": {"input": 1200, "output": 340}}}},
    {"kind": "run.completed", "label": "ok", "data": {}},
]


def _client(tmp_path):
    scaffold.write_project(tmp_path, "ingest-agent", template="demo")
    return TestClient(build_app(tmp_path))


def test_ingest_run_shows_in_runs_and_counts_tokens(tmp_path):
    client = _client(tmp_path)
    r = client.post("/runs/ingest", json={
        "trigger": "csa-chat", "status": "completed", "source": "chatstudyabroad",
        "event": {"type": "message.received", "payload": {"counsellor": "sample@csa"}},
        "trace": CSA_SHAPED_TRACE,
    })
    assert r.status_code == 200
    rid = r.json()["runId"]
    assert rid.startswith("run_") and r.json()["events"] == 4

    run = client.get(f"/runs/{rid}").json()
    assert run["ingested"] is True and run["sourceSystem"] == "chatstudyabroad"
    assert run["trigger"] == "csa-chat"
    assert [e["kind"] for e in run["trace"]] == [e["kind"] for e in CSA_SHAPED_TRACE]
    assert [e["seq"] for e in run["trace"]] == [0, 1, 2, 3]  # normalized

    # tokens flow into the console aggregate exactly like a native run
    snap = client.get("/console").json()
    assert snap["stats"]["inputTokens"] >= 1200
    assert snap["stats"]["outputTokens"] >= 340
    assert any(x["id"] == rid for x in snap["runs"])

    trace = client.get(f"/runs/{rid}/trace").json()
    assert trace["trace"][1]["label"] == "cams_lookup_student"


def test_ingest_validation(tmp_path):
    client = _client(tmp_path)
    assert client.post("/runs/ingest", json={"status": "nope", "trace": CSA_SHAPED_TRACE}).status_code == 400
    assert client.post("/runs/ingest", json={"status": "completed", "trace": []}).status_code == 400
    assert client.post("/runs/ingest", json={"status": "completed",
                                             "trace": [{"label": "no kind"}]}).status_code == 400
    long = [{"kind": "log"}] * 1001
    assert client.post("/runs/ingest", json={"status": "completed", "trace": long}).status_code == 400


def test_ingested_run_id_is_server_generated(tmp_path):
    client = _client(tmp_path)
    r = client.post("/runs/ingest", json={"status": "completed", "trace": [{"kind": "log"}],
                                          "id": "run_attacker_chosen"})
    assert r.json()["runId"] != "run_attacker_chosen"
