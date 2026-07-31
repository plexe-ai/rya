"""`rya eval --langfuse-dataset` — pull Langfuse dataset items, run the agent over
each, and link every run's trace back as a dataset run.

Fully offline: a mock HTTP server stands in for Langfuse, serving paginated
dataset items on GET and capturing every POST (trace ingestion, scores, and the
dataset-run-item links), so we assert the exact wire contract without a network.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from rya.cli import scaffold
from rya.errors import RyaError
from rya.evals import _item_to_trigger, run_langfuse_dataset
from rya.manifest import load_manifest
from rya.observability.export import export_dataset_run_item, fetch_dataset_items
from rya.runtime import load_agent
from rya.store import Store

LF = {"LANGFUSE_HOST": None, "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}


def _project(tmp_path):
    scaffold.write_project(tmp_path, "ev", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()
    return manifest, agent, store


@contextmanager
def _langfuse_mock(pages):
    """Mock Langfuse: GET /api/public/dataset-items paginates over `pages` (a list
    of item-lists); every POST is captured as (path, parsed-body)."""
    captured = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            page = int(q.get("page", ["1"])[0])
            data = pages[page - 1] if 1 <= page <= len(pages) else []
            body = {"data": data, "meta": {"page": page, "totalPages": len(pages)}}
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            try:
                payload = json.loads(raw)
            except Exception:
                payload = None
            captured.append((urlparse(self.path).path, payload))
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


# ---- input → trigger mapping ----------------------------------------------
def test_item_to_trigger_explicit_type_and_payload():
    typ, payload = _item_to_trigger({"input": {"type": "webhook.hit", "payload": {"a": 1}}},
                                    "message.received")
    assert typ == "webhook.hit" and payload == {"a": 1}


def test_item_to_trigger_bare_dict_is_payload_with_defaults():
    typ, payload = _item_to_trigger({"input": {"body": "hi"}}, "message.received",
                                    {"email": "c@csa.test"})
    assert typ == "message.received"
    assert payload == {"email": "c@csa.test", "body": "hi"}


def test_item_to_trigger_string_becomes_body():
    typ, payload = _item_to_trigger({"input": "just text"}, "message.received", {"email": "c@x"})
    assert payload == {"email": "c@x", "body": "just text"}


def test_item_to_trigger_item_wins_over_defaults():
    _, payload = _item_to_trigger({"input": {"email": "own@x"}}, "message.received",
                                  {"email": "default@x"})
    assert payload["email"] == "own@x"


# ---- dataset client (wire contract) ---------------------------------------
def test_fetch_dataset_items_follows_pagination():
    pages = [[{"id": "i1", "input": {"email": "a@x"}}], [{"id": "i2", "input": {"email": "b@x"}}]]
    with _langfuse_mock(pages) as (url, _):
        items = fetch_dataset_items("d1", {**LF, "LANGFUSE_HOST": url})
    assert [i["id"] for i in items] == ["i1", "i2"]


def test_fetch_dataset_items_noop_without_config():
    assert fetch_dataset_items("d1", {}) == []


def test_export_dataset_run_item_wire_body():
    with _langfuse_mock([[]]) as (url, captured):
        res = export_dataset_run_item("run-x", "item-1", "trace-9", {**LF, "LANGFUSE_HOST": url},
                                      metadata={"status": "completed"})
    assert res == "sent"
    (path, body), = captured
    assert path == "/api/public/dataset-run-items"
    assert body["runName"] == "run-x"
    assert body["datasetItemId"] == "item-1"
    assert body["traceId"] == "trace-9"
    assert body["metadata"] == {"status": "completed"}


def test_export_dataset_run_item_noop_without_config():
    assert export_dataset_run_item("r", "i", "t", {}) is None


# ---- full harness ----------------------------------------------------------
def test_run_langfuse_dataset_runs_and_links_every_item(tmp_path, monkeypatch):
    manifest, agent, store = _project(tmp_path)
    pages = [[
        {"id": "item-a", "input": {"email": "ada@acme.io", "body": "hello"}},
        # carries a Rya expect block → scored like a local eval case
        {"id": "item-b", "input": {"email": "risk@acme.io", "body": "please refund"},
         "metadata": {"expect": {"no_failure": True}}},
    ]]
    with _langfuse_mock(pages) as (url, captured):
        monkeypatch.setenv("LANGFUSE_HOST", url)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        rep = run_langfuse_dataset(manifest, agent, store, tmp_path, "d1",
                                   run_name="run-under-test")

    assert rep["dataset"] == "d1" and rep["runName"] == "run-under-test"
    assert rep["total"] == 2 and rep["hasItems"]
    assert {r["itemId"] for r in rep["results"]} == {"item-a", "item-b"}
    # every item got linked as a dataset-run-item pointing at its own run's trace
    links = [b for p, b in captured if p == "/api/public/dataset-run-items"]
    assert len(links) == 2
    by_item = {b["datasetItemId"]: b for b in links}
    for r in rep["results"]:
        assert by_item[r["itemId"]]["traceId"] == r["runId"]
        assert r["linked"] == "sent"
    # the item with an expect block produced scored checks
    item_b = next(r for r in rep["results"] if r["itemId"] == "item-b")
    assert item_b["checks"] and any(c["check"] == "no_failure" for c in item_b["checks"])
    # its scores were pushed to Langfuse ingestion (score-create batch)
    assert any(p == "/api/public/ingestion" for p, _ in captured)


def test_run_langfuse_dataset_requires_config(tmp_path, monkeypatch):
    manifest, agent, store = _project(tmp_path)
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RyaError) as ei:
        run_langfuse_dataset(manifest, agent, store, tmp_path, "d1")
    assert ei.value.code == "E_LANGFUSE_NOT_CONFIGURED"


def test_run_langfuse_dataset_empty_dataset(tmp_path, monkeypatch):
    manifest, agent, store = _project(tmp_path)
    with _langfuse_mock([[]]) as (url, _):
        monkeypatch.setenv("LANGFUSE_HOST", url)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        rep = run_langfuse_dataset(manifest, agent, store, tmp_path, "missing")
    assert rep["hasItems"] is False and rep["total"] == 0 and rep["ok"] is True
