"""Memory management: core blocks, fact consolidation (remember/recall), and
budget-bounded context assembly."""

import asyncio

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.sdk.context import RuntimeContext
from rya.store import Store
from rya.models.registry import default_registry as models_registry
from rya.tools.registry import default_registry as tools_registry


def _ctx(tmp_path):
    scaffold.write_project(tmp_path, "mem")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    run = {"id": "run_mem", "journal": {}, "trace": []}
    return RuntimeContext(store=store, manifest=manifest, run=run,
                          tools=tools_registry(), models=models_registry(), project_root=tmp_path), store


def test_core_memory_blocks(tmp_path):
    ctx, store = _ctx(tmp_path)

    async def go():
        await ctx.memory.block_set("persona", "You are Ada, a calm support agent.")
        await ctx.memory.block_append("persona", "Always confirm the order id.")
        b = await ctx.memory.block_get("persona")
        assert "calm support agent" in b["value"] and "order id" in b["value"]
        # limit truncates and flags
        await ctx.memory.block_set("tiny", "x" * 50, limit=10)
        t = await ctx.memory.block_get("tiny")
        assert len(t["value"]) == 10 and t["truncated"] is True
        names = {bl["name"] for bl in await ctx.memory.blocks()}
        assert names == {"persona", "tiny"}

    asyncio.run(go())
    # durable
    assert "persona" in store.load_memory("agent")["blocks"]


def test_remember_consolidates_duplicates(tmp_path):
    ctx, store = _ctx(tmp_path)

    async def go():
        await ctx.memory.remember("The customer's name is Ada Lovelace.")
        # near-identical fact should consolidate, not pile up
        again = await ctx.memory.remember("The customer's name is Ada Lovelace.")
        assert again[0]["action"] == "consolidated"
        await ctx.memory.remember("Ada prefers email over phone.")
        facts = store.load_memory("agent")["collections"]["facts"]
        return len(facts)

    n = asyncio.run(go())
    assert n == 2  # two distinct facts, the duplicate folded in


def test_recall_ranks_relevant_fact(tmp_path):
    ctx, _ = _ctx(tmp_path)

    async def go():
        await ctx.memory.remember("Ada prefers email over phone.\n"
                                  "The account is on the enterprise plan.\n"
                                  "The renewal date is in March.")
        hits = await ctx.memory.recall("how should we contact the customer", limit=2)
        assert hits and all("_score" in h and "_embedding" not in h for h in hits)
        # the contact-preference fact should surface (vector or lexical)
        joined = " ".join(h["text"].lower() for h in hits)
        return joined

    joined = asyncio.run(go())
    assert "email" in joined or "phone" in joined


def test_assemble_respects_token_budget(tmp_path):
    ctx, _ = _ctx(tmp_path)

    async def go():
        await ctx.memory.block_set("persona", "Support agent for Acme.")
        for i in range(20):
            await ctx.memory.remember(f"Fact number {i}: the widget code is W{i} and it ships in {i} days.")
        out = await ctx.memory.assemble("widget shipping", token_budget=60)
        return out

    out = asyncio.run(go())
    # core block always present; facts paged in within budget
    assert any(b["name"] == "persona" for b in out["blocks"])
    assert out["approxTokens"] <= out["tokenBudget"]
    assert len(out["facts"]) >= 1
