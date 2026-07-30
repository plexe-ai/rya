"""Token usage + cost accounting.

Two sources, in order of authority:

1. **The durable meter** (``store.meter_*``) — the billable-fact ledger written
   once, at the moment of the call, by ``RuntimeContext._meter``. This is what
   money is computed from (PLATFORM_DESIGN D10: "billable facts are journaled,
   not traced").
2. **The run trace** — retained as a fallback for runs written before the meter
   existed, and for callers that hold a run dict but no store handle. The trace
   is a debugging artifact: it is redacted, it can be truncated, and it is
   rewritten with the run row. It is not a ledger.

Cost is only reported when a price is configured — we never hard-code provider
prices that might be wrong:

    RYA_PRICE_<MODEL>_IN   = dollars per 1M input tokens
    RYA_PRICE_<MODEL>_OUT  = dollars per 1M output tokens

Prices come from the resolved run config (D8), not from ambient environment;
``env`` is the mapping the caller was given, and only falls back to
``os.environ`` when a caller has none to hand.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional


def _price(model: str, direction: str, env: Mapping[str, str]):
    key = f"RYA_PRICE_{model.upper().replace('-', '_').replace('.', '_')}_{direction}"
    raw = env.get(key)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def price_for(model: str, input_tokens: int, output_tokens: int,
              env: Optional[Mapping[str, str]] = None) -> float:
    """Dollar cost of one model call, or 0.0 when no price is configured.

    The meter records a number rather than None so the ledger stays summable;
    ``run_usage`` still reports ``costUsd: None`` when nothing was priced, which
    is the distinction between "free" and "we don't know".
    """
    env = env if env is not None else os.environ
    cost = 0.0
    pi, po = _price(model or "", "IN", env), _price(model or "", "OUT", env)
    if pi is not None:
        cost += (input_tokens / 1_000_000) * pi
    if po is not None:
        cost += (output_tokens / 1_000_000) * po
    return round(cost, 6)


def _usage_from_meter(store, run_id: str, env: Mapping[str, str]) -> Optional[dict]:
    """Usage summed from the durable ledger, or None when this store has no
    meter or has no facts for the run (so the caller falls back to the trace)."""
    read = getattr(store, "meter_read", None)
    if read is None:
        return None
    try:
        records = read(run_id=run_id, limit=10 ** 6)
    except Exception:  # a meter read must never break a run summary
        return None
    if not records:
        return None
    inp = out = 0
    cost = 0.0
    priced = False
    for rec in records:
        i, o = int(rec.get("inputTokens") or 0), int(rec.get("outputTokens") or 0)
        inp += i
        out += o
        model = rec.get("model") or ""
        if _price(model, "IN", env) is not None or _price(model, "OUT", env) is not None:
            priced = True
            cost += price_for(model, i, o, env)
        elif rec.get("costUsd"):
            # Priced at write time under a config that has since changed. The
            # recorded number is the billable one — re-pricing history silently
            # would make an invoice unreproducible.
            priced = True
            cost += float(rec["costUsd"])
    return {"inputTokens": inp, "outputTokens": out,
            "costUsd": round(cost, 6) if priced else None}


def _usage_from_trace(run: dict, env: Mapping[str, str]) -> dict:
    inp = out = 0
    cost = 0.0
    priced = False
    for ev in run.get("trace", []):
        # Count every real LLM call: single-shot `llm.respond` AND each governed
        # agent-loop step `llm.chat` (both carry {model, usage}). Without llm.chat
        # the whole compose loop's tokens/cost were silently dropped from metering.
        if ev.get("kind") not in ("llm.respond", "llm.chat"):
            continue
        res = (ev.get("data") or {}).get("result") or {}
        usage = res.get("usage") or {}
        i, o = usage.get("input") or 0, usage.get("output") or 0
        inp += i
        out += o
        model = res.get("model", "")
        pi, po = _price(model, "IN", env), _price(model, "OUT", env)
        if pi is not None:
            cost += (i / 1_000_000) * pi
            priced = True
        if po is not None:
            cost += (o / 1_000_000) * po
            priced = True
    return {"inputTokens": inp, "outputTokens": out,
            "costUsd": round(cost, 6) if priced else None}


def run_usage(run: dict, env: Optional[Mapping[str, str]] = None, store=None) -> dict:
    """Usage for one run. Prefers the durable meter when a store is supplied."""
    env = env if env is not None else os.environ
    if store is not None:
        metered = _usage_from_meter(store, run.get("id", ""), env)
        if metered is not None:
            return metered
    return _usage_from_trace(run, env)


def workspace_usage(store, since: Optional[str] = None, until: Optional[str] = None,
                    group_by: Optional[str] = None) -> dict:
    """Aggregate billable facts across a workspace — the number an invoice is
    built from. Reads the ledger directly; no run dicts, no traces."""
    totals = getattr(store, "meter_totals", None)
    if totals is None:
        return {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
    return totals(since=since, until=until, group_by=group_by)
