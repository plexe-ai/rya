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


def _mock_structured(schema: dict) -> dict:
    """A deterministic object that satisfies a JSON schema's required fields —
    keeps structured-output tests reproducible offline."""
    props = (schema or {}).get("properties", {})
    required = (schema or {}).get("required", list(props.keys()))
    out = {}
    for key in required:
        spec = props.get(key, {})
        t = spec.get("type", "string")
        if "enum" in spec and spec["enum"]:
            out[key] = spec["enum"][0]
        elif t in ("number", "integer"):
            out[key] = 0
        elif t == "boolean":
            out[key] = False
        elif t == "array":
            out[key] = []
        elif t == "object":
            out[key] = {}
        else:
            out[key] = f"mock-{key}"
    return out


def _validate(obj, schema: dict) -> None:
    """Lightweight structural check (required keys + top-level types). Avoids a
    hard jsonschema dependency; raises RyaError on mismatch."""
    if not isinstance(obj, dict):
        raise RyaError("E_LLM_SCHEMA", "structured output is not a JSON object.")
    for key in (schema or {}).get("required", []):
        if key not in obj:
            raise RyaError("E_LLM_SCHEMA", f"structured output missing required field '{key}'.")


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response (handles ```json fences)."""
    s = text.strip()
    if "```" in s:
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    return json.loads(s)


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
            temperature: Optional[float] = None, max_tokens: Optional[int] = None,
            schema: Optional[dict] = None) -> dict:
    """Return ``{text, model, provider, usage[, json]}``. Mock path never raises.

    When ``schema`` (a JSON Schema) is given, the model is asked for JSON and the
    result is parsed + validated into ``json`` — first-class structured output."""
    effective = resolve_provider(provider)
    sys = system
    if schema is not None and effective != "mock":
        sys = (system + "\n\nRespond with ONLY a JSON object matching this JSON Schema "
               "(no prose, no code fences):\n" + json.dumps(schema))
    content = _stringify(input)
    if effective == "anthropic":
        out = _anthropic(model_default, sys, content, temperature, max_tokens or (1024 if schema else None))
    elif effective == "openai":
        out = _openai(model_default, sys, content, temperature, max_tokens)
    else:
        out = {"text": _mock_text(system, input), "model": model_default, "provider": "mock", "usage": None}
        if schema is not None:
            out["json"] = _mock_structured(schema)
        return out
    if schema is not None:
        obj = _extract_json(out["text"])
        _validate(obj, schema)
        out["json"] = obj
    return out


# ---- tool-calling chat (the governed agent loop's model step) --------------
def _mock_chat(messages: list, tools: Optional[list]) -> dict:
    """Deterministic agent-loop driver for offline tests: call the first tool
    once (if any and not already called this turn), otherwise answer."""
    already_called = any(m.get("role") == "tool" for m in messages)
    if tools and not already_called:
        t = tools[0]
        # echo the last user content as the tool input, so the call is meaningful
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), {})
        arg = user if isinstance(user, dict) else {"query": str(user)}
        return {"text": "", "toolCalls": [{"id": "call_1", "name": t["name"], "input": arg}],
                "model": "mock-llm", "provider": "mock", "usage": None}
    tool_outs = [m.get("content") for m in messages if m.get("role") == "tool"]
    return {"text": f"[mock-llm] done. observed {len(tool_outs)} tool result(s).",
            "toolCalls": [], "model": "mock-llm", "provider": "mock", "usage": None}


def chat(*, messages: list, tools: Optional[list] = None, system: str = "",
         model_default: str = "mock-llm", provider: str = "auto",
         temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> dict:
    """One model turn that may request tool calls. Returns
    ``{text, toolCalls:[{id,name,input}], model, provider, usage}``.

    ``messages`` is a list of ``{role: user|assistant|tool, content, [name]}``.
    The mock provider drives a deterministic call-one-tool-then-answer loop;
    real providers use the native tool-use / function-calling format."""
    effective = resolve_provider(provider)
    if effective == "anthropic":
        return _anthropic_chat(model_default, system, messages, tools, temperature, max_tokens)
    if effective == "openai":
        return _openai_chat(model_default, system, messages, tools, temperature, max_tokens)
    return _mock_chat(messages, tools)


def _anthropic_chat(name, system, messages, tools, temperature, max_tokens) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "anthropic provider but ANTHROPIC_API_KEY is not set.",
                       hint="Set ANTHROPIC_API_KEY or use the mock provider.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_LLM_MODEL", _DEFAULT_ANTHROPIC)
    amsgs = []
    for m in messages:
        if m["role"] == "tool":
            amsgs.append({"role": "user", "content": [{"type": "tool_result",
                          "tool_use_id": m.get("toolUseId", "call_1"),
                          "content": _stringify(m["content"])}]})
        else:
            amsgs.append({"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                          else _stringify(m["content"])})
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system, "messages": amsgs}
    if tools:
        payload["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                             "input_schema": t.get("input_schema", {"type": "object"})} for t in tools]
    if temperature is not None:
        payload["temperature"] = temperature
    j = _http_json("https://api.anthropic.com/v1/messages",
                   {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, payload)
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    calls = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input", {})}
             for b in j.get("content", []) if b.get("type") == "tool_use"]
    u = j.get("usage") or {}
    return {"text": text, "toolCalls": calls, "model": model, "provider": "anthropic",
            "usage": {"input": u.get("input_tokens"), "output": u.get("output_tokens")} if u else None}


def _openai_chat(name, system, messages, tools, temperature, max_tokens) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "openai provider but OPENAI_API_KEY is not set.",
                       hint="Set OPENAI_API_KEY or use the mock provider.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_OPENAI_MODEL", _DEFAULT_OPENAI)
    omsgs = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            omsgs.append({"role": "tool", "tool_call_id": m.get("toolUseId", "call_1"),
                          "content": _stringify(m["content"])})
        else:
            omsgs.append({"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                          else _stringify(m["content"])})
    payload = {"model": model, "messages": omsgs}
    if tools:
        payload["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"})}} for t in tools]
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = max_tokens
    j = _http_json("https://api.openai.com/v1/chat/completions",
                   {"Authorization": f"Bearer {key}", "content-type": "application/json"}, payload)
    msg = j["choices"][0]["message"]
    calls = [{"id": c["id"], "name": c["function"]["name"],
              "input": json.loads(c["function"].get("arguments") or "{}")}
             for c in (msg.get("tool_calls") or [])]
    u = j.get("usage") or {}
    return {"text": msg.get("content") or "", "toolCalls": calls, "model": model, "provider": "openai",
            "usage": {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")} if u else None}
