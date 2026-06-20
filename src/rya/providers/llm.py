"""LLM provider seam for ``ctx.llm`` — real multi-provider, mock fallback.

Pattern borrowed from openclaw's `createLlm` factory: the manifest declares
``model.provider`` (auto | mock | anthropic | openai) and a model name; the
factory returns the matching provider. Real providers are called over plain HTTP
(stdlib ``urllib``, no SDK dependency), so the real path works on a base install.

- ``auto`` (default): real if an API key is present, else mock — zero-config dev.
- ``mock``: deterministic offline stub (keeps tests + CI reproducible).
- ``anthropic`` / ``openai``: require the matching API key; error clearly if missing.

The call is journaled by the runtime, so replays after an approval pause never
re-bill the model.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from ..errors import RyaError

# Names that mean "no real model chosen yet" — fall back to an env/default model.
_PLACEHOLDER_NAMES = {"mock-llm", "mock-llm-mini", "mock", "dev"}
_DEFAULT_ANTHROPIC = "claude-haiku-4-5-20251001"
_DEFAULT_OPENAI = "gpt-4.1-mini"


def resolve_provider(provider: str = "auto") -> str:
    """Resolve ``auto`` to a concrete provider based on which API key is present."""
    if provider and provider != "auto":
        return provider
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "mock"


# Back-compat: earlier code called active_provider() with no manifest context.
def active_provider() -> str:
    return resolve_provider("auto")


def _stringify(input: dict) -> str:
    return json.dumps(input, default=str)


def _http_json(url: str, headers: dict, payload: dict, timeout: int = 60) -> dict:
    from ..guard import check_egress
    check_egress(url, "POST")  # Action Guard egress check
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RyaError("E_RUNTIME", f"LLM provider HTTP {e.code}: {body}",
                       hint="Check the API key, model name, and quota.")
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"LLM request failed: {e.reason}",
                       hint="Check network egress to the provider.")


def _mock_text(system: str, input: dict) -> str:
    cust = input.get("customer") if isinstance(input, dict) else None
    name = cust.get("name") if isinstance(cust, dict) else "customer"
    return f"[mock-llm] {system.strip()} -> draft for {name}"


def _anthropic(name, system, content, temperature, max_tokens) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "model.provider is 'anthropic' but ANTHROPIC_API_KEY is not set.",
                       hint="Set ANTHROPIC_API_KEY, or use model.provider: mock for offline dev.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_LLM_MODEL", _DEFAULT_ANTHROPIC)
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system,
               "messages": [{"role": "user", "content": content}]}
    if temperature is not None:
        payload["temperature"] = temperature
    j = _http_json("https://api.anthropic.com/v1/messages",
                   {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   payload)
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    u = j.get("usage") or {}
    return {"text": text, "model": model, "provider": "anthropic",
            "usage": {"input": u.get("input_tokens"), "output": u.get("output_tokens")} if u else None}


def _openai(name, system, content, temperature, max_tokens) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "model.provider is 'openai' but OPENAI_API_KEY is not set.",
                       hint="Set OPENAI_API_KEY, or use model.provider: mock for offline dev.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_OPENAI_MODEL", _DEFAULT_OPENAI)
    payload = {"model": model, "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": content}]}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = max_tokens
    j = _http_json("https://api.openai.com/v1/chat/completions",
                   {"Authorization": f"Bearer {key}", "content-type": "application/json"}, payload)
    text = j["choices"][0]["message"]["content"]
    u = j.get("usage") or {}
    return {"text": text, "model": model, "provider": "openai",
            "usage": {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")} if u else None}


def respond(*, system: str, input: dict, model_default: str = "mock-llm", provider: str = "auto",
            temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> dict:
    """Return ``{text, model, provider, usage}``. Mock path never raises."""
    effective = resolve_provider(provider)
    content = _stringify(input)
    if effective == "anthropic":
        return _anthropic(model_default, system, content, temperature, max_tokens)
    if effective == "openai":
        return _openai(model_default, system, content, temperature, max_tokens)
    return {"text": _mock_text(system, input), "model": model_default, "provider": "mock", "usage": None}
