"""Token usage + cost accounting, computed from a run's trace.

Computed from the trace (not a live accumulator) so it is correct across
approval pause/resume replays. Cost is only reported when a price is configured
via env — we never hard-code provider prices that might be wrong:

    RYA_PRICE_<MODEL>_IN   = dollars per 1M input tokens
    RYA_PRICE_<MODEL>_OUT  = dollars per 1M output tokens
"""

from __future__ import annotations

import os
from typing import Optional


def _price(model: str, direction: str, env: dict):
    key = f"RYA_PRICE_{model.upper().replace('-', '_').replace('.', '_')}_{direction}"
    raw = env.get(key)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def run_usage(run: dict, env: Optional[dict] = None) -> dict:
    env = env or os.environ
    inp = out = 0
    cost = 0.0
    priced = False
    for ev in run.get("trace", []):
        if ev.get("kind") != "llm.respond":
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
