"""Files primitive - durable uploads that wake the agent.

Covers the store roundtrip, the journal rule (metadata only, never bytes), the
as_document bridge into ctx.llm.respond(documents=), and the upload API firing
a ``file.uploaded`` event that triggers a run."""

import asyncio

from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


def _engine(tmp_path) -> Engine:
    scaffold.write_project(tmp_path, "files-agent", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def _ctx(engine):
    from rya.sdk.context import RuntimeContext
    event = engine.make_event("message.received", {"email": "a@x.co"})
    run = engine._new_run("event", event)
    return RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                          tools=engine.tools, models=engine.models,
                          project_root=engine.project_root, agent=engine.agent)


def test_store_roundtrip_and_tag_filter(tmp_path):
    engine = _engine(tmp_path)
    m1 = engine.store.save_file("aecb.pdf", b"%PDF-1", content_type="application/pdf",
                                tags={"cif": "884411", "docType": "aecb"})
    engine.store.save_file("spread.xlsx", b"XLSX", tags={"cif": "884411", "docType": "spread"})
    engine.store.save_file("other.pdf", b"%PDF-2", tags={"cif": "999999"})

    assert engine.store.read_file(m1["id"]) == b"%PDF-1"
    meta = engine.store.get_file(m1["id"])
    assert meta["size"] == 6 and meta["sha256"] == m1["sha256"]
    cif_files = engine.store.list_files(tags={"cif": "884411"})
    assert {f["name"] for f in cif_files} == {"aecb.pdf", "spread.xlsx"}
    assert engine.store.list_files(tags={"cif": "884411", "docType": "aecb"})[0]["name"] == "aecb.pdf"


def test_ctx_files_journal_holds_metadata_never_bytes(tmp_path):
    engine = _engine(tmp_path)
    meta = engine.store.save_file("doc.pdf", b"SECRET-BYTES", tags={})
    ctx = _ctx(engine)

    data = asyncio.run(ctx.files.read(meta["id"]))
    assert data == b"SECRET-BYTES"
    journal_blob = str(ctx.run["journal"])
    assert "SECRET-BYTES" not in journal_blob
    assert meta["sha256"] in journal_blob  # metadata IS journaled

    doc = asyncio.run(ctx.files.as_document(meta["id"]))
    assert doc == {"name": "doc.pdf", "format": "pdf", "bytes": b"SECRET-BYTES"}


def test_upload_api_stores_and_fires_event(tmp_path):
    scaffold.write_project(tmp_path, "files-agent", template="demo")
    client = TestClient(build_app(tmp_path))

    r = client.post("/files?name=aecb.pdf&tag.cif=884411&tag.docType=aecb",
                    content=b"%PDF-aecb", headers={"content-type": "application/pdf"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["tags"] == {"cif": "884411", "docType": "aecb"}
    assert body["runId"]  # a file.uploaded run was triggered
    run = client.get(f"/runs/{body['runId']}").json()
    assert run["event"]["type"] == "file.uploaded"
    assert run["event"]["payload"]["fileId"] == body["file"]["id"]

    # event=false stores silently
    r2 = client.post("/files?name=quiet.pdf&event=false", content=b"x")
    assert r2.status_code == 200 and "runId" not in r2.json()

    # listing + meta endpoints
    assert {f["name"] for f in client.get("/files").json()["files"]} == {"aecb.pdf", "quiet.pdf"}
    assert client.get(f"/files/{body['file']['id']}").json()["name"] == "aecb.pdf"
    assert client.post("/files?name=", content=b"x").status_code == 400
    assert client.post("/files?name=e.pdf", content=b"").status_code == 400


import os

import pytest


@pytest.mark.skipif(not os.environ.get("RYA_TEST_DATABASE_URL"),
                    reason="set RYA_TEST_DATABASE_URL to run Postgres files tests")
def test_postgres_files_roundtrip():
    from rya.store_postgres import PostgresStore
    s = PostgresStore(os.environ["RYA_TEST_DATABASE_URL"])
    s.ensure()
    m = s.save_file("a.pdf", b"PGBYTES\x00\x01", content_type="application/pdf",
                    tags={"cif": "884411"})
    assert s.read_file(m["id"]) == b"PGBYTES\x00\x01"  # binary-safe roundtrip
    assert s.get_file(m["id"])["tags"] == {"cif": "884411"}
    assert any(f["id"] == m["id"] for f in s.list_files(tags={"cif": "884411"}))
    assert all(f["id"] != m["id"] for f in s.list_files(tags={"cif": "nope"}))
