"""Embeddings provider seam for semantic memory search.

Same env-gated factory shape as the LLM/channel seams:
- ``openai``  → ``OPENAI_API_KEY`` (text-embedding-3-small over stdlib HTTP)
- otherwise   → mock: a deterministic hashing vectorizer (bag-of-tokens) so
  cosine similarity reflects lexical overlap — real vectors, fully offline,
  reproducible in tests.

Vectors are stored alongside memory items; ``ctx.memory.search`` ranks by cosine.
Cosine only compares same-length vectors, so a collection embedded with one
provider and queried with another safely falls back to a lexical score.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import List, Optional

_MOCK_DIM = 64


def active_embedding_provider(env: Optional[dict] = None) -> str:
    env = env or os.environ
    if env.get("RYA_EMBEDDINGS") == "mock":
        return "mock"
    if env.get("OPENAI_API_KEY"):
        return "openai"
    return "mock"


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _mock_embed(text: str, dim: int = _MOCK_DIM) -> List[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    return _normalize(vec)


def _openai_embed(text: str, env: dict) -> List[float]:
    import json
    import urllib.error
    import urllib.request

    from ..errors import RyaError

    from ..guard import check_egress
    check_egress("https://api.openai.com/v1/embeddings", "POST")  # Action Guard egress check
    payload = {"model": env.get("RYA_EMBEDDING_MODEL", "text-embedding-3-small"), "input": text}
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {env['OPENAI_API_KEY']}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())["data"][0]["embedding"]
    except urllib.error.HTTPError as e:
        raise RyaError("E_RUNTIME", f"embeddings HTTP {e.code}: {e.read().decode(errors='replace')[:200]}",
                       hint="Check OPENAI_API_KEY / embedding model.")
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"embeddings request failed: {e.reason}", hint="Check network egress.")


def embed(text: str, env: Optional[dict] = None) -> List[float]:
    env = env or os.environ
    if active_embedding_provider(env) == "openai":
        return _openai_embed(text, env)
    return _mock_embed(text)


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
