"""RAG / knowledge primitive — ingest → chunk → embed → retrieve."""

import asyncio

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.sdk.context import RuntimeContext, _chunk_text
from rya.store import Store
from rya.models.registry import default_registry as models_registry
from rya.tools.registry import default_registry as tools_registry


def test_chunker_overlaps_and_respects_size():
    text = ("para one is here. " * 30) + "\n\n" + ("para two follows. " * 30)
    chunks = _chunk_text(text, size=200, overlap=40)
    assert len(chunks) >= 3
    assert all(len(c) <= 260 for c in chunks)          # ~size (+ boundary slack)
    assert all(c.strip() for c in chunks)
    short = _chunk_text("just a little text", size=800)
    assert short == ["just a little text"]


def _ctx(tmp_path):
    scaffold.write_project(tmp_path, "rag", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    run = {"id": "run_rag", "journal": {}, "trace": []}
    return RuntimeContext(store=store, manifest=manifest, run=run, tools=tools_registry(),
                          models=models_registry(), project_root=tmp_path), store


def test_ingest_and_retrieve(tmp_path):
    ctx, store = _ctx(tmp_path)

    async def go():
        d1 = await ctx.knowledge.add(
            "Refunds are processed within 5 business days to the original payment method.",
            source="policies/refunds.md")
        d2 = await ctx.knowledge.add(
            "The enterprise plan includes SSO, audit logs, and a 99.9% uptime SLA.",
            source="policies/plans.md")
        assert d1["chunks"] >= 1 and d2["chunks"] >= 1
        docs = await ctx.knowledge.documents()
        assert {d["source"] for d in docs} == {"policies/refunds.md", "policies/plans.md"}
        # retrieval surfaces the relevant chunk with its source (token-overlap +
        # vector blend ranks the refund policy first for a refund query)
        hits = await ctx.knowledge.search("refund processed payment method", limit=2)
        assert hits and all("_embedding" not in h for h in hits)
        top = hits[0]
        assert "refund" in top["text"].lower() and top["source"] == "policies/refunds.md"
        return len(docs)

    n = asyncio.run(go())
    assert n == 2
    # durable on the substrate
    assert len(store.load_memory("knowledge")["documents"]) == 2


def test_long_document_is_chunked(tmp_path):
    ctx, store = _ctx(tmp_path)

    async def go():
        big = "Section. " * 500  # ~4000 chars
        res = await ctx.knowledge.add(big, source="big.txt", chunk_size=500, overlap=50)
        return res

    res = asyncio.run(go())
    assert res["chunks"] >= 5
    chunks = store.load_memory("knowledge")["collections"]["chunks"]
    assert len(chunks) == res["chunks"]
    assert all(c.get("_embedding") for c in chunks)  # every chunk embedded
