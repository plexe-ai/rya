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
    """Resolve ``auto`` to a concrete provider based on which API key is present.

    Keyless mode (``RYA_KEYLESS=1``) forces the Governance Adapter: it refuses
    ``anthropic``/``openai`` even if a key is present, so a leaked credential is
    never used. ``mock`` stays available for offline dev/tests (it holds no key),
    and an explicit ``adapter`` always routes to the keyless path."""
    if os.environ.get("RYA_KEYLESS") == "1":
        from . import adapter as _adapter
        _adapter.assert_keyless()
        if provider in ("auto", "anthropic", "openai"):
            return "adapter"
    if provider and provider != "auto":
        return provider
    if os.environ.get("RYA_BEDROCK") == "1":
        return "bedrock"
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


# ---- token streaming --------------------------------------------------------
def _sse_events(resp):
    """Yield parsed JSON payloads from an SSE byte stream (``data: {...}`` lines)."""
    for raw in resp:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _http_stream(url: str, headers: dict, payload: dict, timeout: int = 120):
    from ..guard import check_egress
    check_egress(url, "POST")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RyaError("E_RUNTIME", f"LLM provider HTTP {e.code}: {body}",
                       hint="Check the API key, model name, and quota.")
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"LLM request failed: {e.reason}",
                       hint="Check network egress to the provider.")


def _anthropic_stream(name, system, content, temperature, max_tokens, on_token) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "model.provider is 'anthropic' but ANTHROPIC_API_KEY is not set.",
                       hint="Set ANTHROPIC_API_KEY, or use model.provider: mock for offline dev.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_LLM_MODEL", _DEFAULT_ANTHROPIC)
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system,
               "messages": [{"role": "user", "content": content}], "stream": True}
    if temperature is not None:
        payload["temperature"] = temperature
    resp = _http_stream("https://api.anthropic.com/v1/messages",
                        {"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json", "accept": "text/event-stream"}, payload)
    parts, usage_in, usage_out = [], None, None
    with resp:
        for ev in _sse_events(resp):
            t = ev.get("type")
            if t == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                chunk = ev["delta"].get("text", "")
                if chunk:
                    parts.append(chunk)
                    on_token(chunk)
            elif t == "message_start":
                usage_in = (ev.get("message", {}).get("usage") or {}).get("input_tokens")
            elif t == "message_delta":
                usage_out = (ev.get("usage") or {}).get("output_tokens")
    return {"text": "".join(parts), "model": model, "provider": "anthropic",
            "usage": {"input": usage_in, "output": usage_out} if usage_in or usage_out else None}


def _openai_stream(name, system, content, temperature, max_tokens, on_token) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RyaError("E_VALIDATION", "model.provider is 'openai' but OPENAI_API_KEY is not set.",
                       hint="Set OPENAI_API_KEY, or use model.provider: mock for offline dev.")
    model = name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_OPENAI_MODEL", _DEFAULT_OPENAI)
    payload = {"model": model, "stream": True, "stream_options": {"include_usage": True},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": content}]}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = max_tokens
    resp = _http_stream("https://api.openai.com/v1/chat/completions",
                        {"Authorization": f"Bearer {key}", "content-type": "application/json",
                         "accept": "text/event-stream"}, payload)
    parts, usage = [], None
    with resp:
        for ev in _sse_events(resp):
            choices = ev.get("choices") or []
            if choices:
                chunk = (choices[0].get("delta") or {}).get("content") or ""
                if chunk:
                    parts.append(chunk)
                    on_token(chunk)
            if ev.get("usage"):
                usage = {"input": ev["usage"].get("prompt_tokens"),
                         "output": ev["usage"].get("completion_tokens")}
    return {"text": "".join(parts), "model": model, "provider": "openai", "usage": usage}


def _mock_stream(system: str, input: dict, on_token) -> str:
    text = _mock_text(system, input)
    for i, word in enumerate(text.split(" ")):
        on_token(word if i == 0 else " " + word)
    return text


def respond(*, system: str, input: dict, model_default: str = "mock-llm", provider: str = "auto",
            temperature: Optional[float] = None, max_tokens: Optional[int] = None,
            schema: Optional[dict] = None, on_token=None, documents: Optional[list] = None) -> dict:
    """Return ``{text, model, provider, usage[, json]}``. Mock path never raises.

    When ``schema`` (a JSON Schema) is given, the model is asked for JSON and the
    result is parsed + validated into ``json`` — first-class structured output.
    When ``on_token`` is given, the response is generated via the provider's
    streaming API and each text chunk is delivered to the callback as it
    arrives; the returned dict is identical to the non-streaming shape."""
    effective = resolve_provider(provider)
    sys = system
    if schema is not None and effective != "mock":
        sys = (system + "\n\nRespond with ONLY a JSON object matching this JSON Schema "
               "(no prose, no code fences):\n" + json.dumps(schema))
    content = _stringify(input)
    if documents and effective not in ("bedrock", "mock"):
        raise RyaError("E_VALIDATION", f"documents are not supported on the '{effective}' provider yet.",
                       hint="Use model.provider: bedrock for document-grounded calls.")
    if effective == "bedrock":
        out = (_bedrock_stream(model_default, sys, content, temperature,
                               max_tokens or (1024 if schema else None), on_token, documents)
               if on_token else
               _bedrock(model_default, sys, content, temperature,
                        max_tokens or (1024 if schema else None), documents))
    elif effective == "anthropic":
        out = (_anthropic_stream(model_default, sys, content, temperature,
                                 max_tokens or (1024 if schema else None), on_token)
               if on_token else
               _anthropic(model_default, sys, content, temperature, max_tokens or (1024 if schema else None)))
    elif effective == "openai":
        out = (_openai_stream(model_default, sys, content, temperature, max_tokens, on_token)
               if on_token else
               _openai(model_default, sys, content, temperature, max_tokens))
    elif effective == "adapter":
        # Keyless path: no provider key, only the Platform Token; fails closed.
        from . import adapter as _adapter
        out = _adapter.adapter_respond(model_default, sys, content, temperature,
                                       max_tokens or (1024 if schema else None), on_token)
    else:
        text = _mock_stream(system, input, on_token) if on_token else _mock_text(system, input)
        out = {"text": text, "model": model_default, "provider": "mock", "usage": None}
        if schema is not None:
            out["json"] = _mock_structured(schema)
        return out
    if schema is not None:
        obj = _extract_json(out["text"])
        _validate(obj, schema)
        out["json"] = obj
    return out


# ---- AWS Bedrock (Converse API, IAM-signed via boto3) -----------------------
# No API key: auth is the ambient AWS identity (IAM role / profile), which is
# what banks and other keyless-by-policy environments require. The model name
# is a Bedrock inference profile id (e.g. ``us.anthropic.claude-haiku-4-5``).
_DEFAULT_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _bedrock_client():
    try:
        import boto3  # optional dep: pip install 'rya[bedrock]'
    except ImportError:
        raise RyaError("E_VALIDATION", "model.provider is 'bedrock' but boto3 is not installed.",
                       hint="pip install 'rya[bedrock]' (or add boto3 to the project).")
    region = (os.environ.get("RYA_BEDROCK_REGION") or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def _bedrock_model(name):
    return name if name not in _PLACEHOLDER_NAMES else os.environ.get("RYA_BEDROCK_MODEL", _DEFAULT_BEDROCK)


def _bedrock_error(e) -> RyaError:
    return RyaError("E_RUNTIME", f"Bedrock call failed: {e}",
                    hint="Check AWS credentials, RYA_BEDROCK_REGION, and Bedrock model access.")


def _doc_blocks(documents) -> list:
    """[{name, format, bytes|b64|path}] -> Converse document blocks. Bedrock
    rejects names with characters outside [A-Za-z0-9 ()\\[\\]-], so sanitize."""
    import base64
    import re
    blocks = []
    for d in documents or []:
        raw = d.get("bytes")
        if raw is None and d.get("b64"):
            raw = base64.b64decode(d["b64"])
        if raw is None and d.get("path"):
            with open(d["path"], "rb") as f:
                raw = f.read()
        if raw is None:
            raise RyaError("E_VALIDATION", "document needs one of: bytes, b64, path.")
        name = re.sub(r"[^A-Za-z0-9 \-\(\)\[\]]", "-", str(d.get("name") or "document"))[:60] or "document"
        blocks.append({"document": {"format": d.get("format", "pdf"), "name": name,
                                    "source": {"bytes": raw}}})
    return blocks


def _bedrock(name, system, content, temperature, max_tokens, documents=None) -> dict:
    client = _bedrock_client()
    model = _bedrock_model(name)
    inference = {"maxTokens": max_tokens or 1024}
    if temperature is not None:
        inference["temperature"] = temperature
    try:
        j = client.converse(
            modelId=model,
            system=[{"text": system}] if system else [],
            messages=[{"role": "user", "content": _doc_blocks(documents) + [{"text": content}]}],
            inferenceConfig=inference,
        )
    except RyaError:
        raise
    except Exception as e:
        raise _bedrock_error(e)
    text = "".join(b.get("text", "") for b in j["output"]["message"]["content"] if "text" in b)
    u = j.get("usage") or {}
    return {"text": text, "model": model, "provider": "bedrock",
            "usage": {"input": u.get("inputTokens"), "output": u.get("outputTokens")} if u else None}


def _bedrock_stream(name, system, content, temperature, max_tokens, on_token, documents=None) -> dict:
    client = _bedrock_client()
    model = _bedrock_model(name)
    inference = {"maxTokens": max_tokens or 1024}
    if temperature is not None:
        inference["temperature"] = temperature
    try:
        resp = client.converse_stream(
            modelId=model,
            system=[{"text": system}] if system else [],
            messages=[{"role": "user", "content": _doc_blocks(documents) + [{"text": content}]}],
            inferenceConfig=inference,
        )
        parts, usage = [], None
        for ev in resp["stream"]:
            if "contentBlockDelta" in ev:
                chunk = ev["contentBlockDelta"].get("delta", {}).get("text", "")
                if chunk:
                    parts.append(chunk)
                    on_token(chunk)
            elif "metadata" in ev:
                u = ev["metadata"].get("usage") or {}
                usage = {"input": u.get("inputTokens"), "output": u.get("outputTokens")}
    except RyaError:
        raise
    except Exception as e:
        raise _bedrock_error(e)
    return {"text": "".join(parts), "model": model, "provider": "bedrock", "usage": usage}


def _merge_adjacent_roles(msgs: list) -> list:
    """Converse requires strict role alternation; a multi-tool turn produces
    consecutive user messages (one toolResult each). Merge their content lists."""
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append(m)
    return merged


def _bedrock_chat(name, system, messages, tools, temperature, max_tokens) -> dict:
    import re

    client = _bedrock_client()
    model = _bedrock_model(name)
    # Bedrock tool names must match [a-zA-Z0-9_-]+ but Rya tool ids are dotted
    # (crm.lookup). Sanitize outbound, translate back on the returned calls.
    name_map = {}

    def _safe(n):
        s = re.sub(r"[^A-Za-z0-9_-]", "_", str(n))
        name_map[s] = n
        return s

    bmsgs = []
    for m in messages:
        if m["role"] == "tool":
            bmsgs.append({"role": "user", "content": [{"toolResult": {
                "toolUseId": m.get("toolUseId", "call_1"),
                "content": [{"text": _stringify(m["content"])}]}}]})
        elif m["role"] == "assistant" and m.get("toolCalls"):
            content = []
            if m.get("content"):
                content.append({"text": m["content"] if isinstance(m["content"], str)
                                else _stringify(m["content"])})
            for c in m["toolCalls"]:
                content.append({"toolUse": {"toolUseId": c.get("id", "call_1"),
                                            "name": _safe(c.get("name")),
                                            "input": c.get("input") or {}}})
            bmsgs.append({"role": "assistant", "content": content})
        else:
            bmsgs.append({"role": m["role"], "content": [{"text": m["content"] if isinstance(m["content"], str)
                                                          else _stringify(m["content"])}]})
    kwargs = {
        "modelId": model,
        "system": [{"text": system}] if system else [],
        "messages": _merge_adjacent_roles(bmsgs),
        "inferenceConfig": {"maxTokens": max_tokens or 1024,
                            **({"temperature": temperature} if temperature is not None else {})},
    }
    if tools:
        kwargs["toolConfig"] = {"tools": [{"toolSpec": {
            "name": _safe(t["name"]), "description": t.get("description") or t["name"],
            "inputSchema": {"json": t.get("input_schema") or {"type": "object"}}}} for t in tools]}
    try:
        j = client.converse(**kwargs)
    except RyaError:
        raise
    except Exception as e:
        raise _bedrock_error(e)
    blocks = j["output"]["message"]["content"]
    text = "".join(b.get("text", "") for b in blocks if "text" in b)
    calls = [{"id": b["toolUse"].get("toolUseId"),
              "name": name_map.get(b["toolUse"].get("name"), b["toolUse"].get("name")),
              "input": b["toolUse"].get("input", {})} for b in blocks if "toolUse" in b]
    u = j.get("usage") or {}
    return {"text": text, "toolCalls": calls, "model": model, "provider": "bedrock",
            "usage": {"input": u.get("inputTokens"), "output": u.get("outputTokens")} if u else None}


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
    if effective == "bedrock":
        return _bedrock_chat(model_default, system, messages, tools, temperature, max_tokens)
    if effective == "anthropic":
        return _anthropic_chat(model_default, system, messages, tools, temperature, max_tokens)
    if effective == "openai":
        return _openai_chat(model_default, system, messages, tools, temperature, max_tokens)
    if effective == "adapter":
        # Keyless path: no provider key, only the Platform Token; fails closed.
        from . import adapter as _adapter
        return _adapter.adapter_chat(model_default, system, messages, tools, temperature, max_tokens)
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
        elif m["role"] == "assistant" and m.get("toolCalls"):
            # Reconstruct the tool_use content blocks so the following
            # tool_result messages have their matching tool_use (required by
            # Anthropic; without this a multi-tool ctx.llm.run turn 400s).
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"] if isinstance(m["content"], str)
                                else _stringify(m["content"])})
            for c in m["toolCalls"]:
                content.append({"type": "tool_use", "id": c.get("id", "call_1"),
                                "name": c.get("name"), "input": c.get("input") or {}})
            amsgs.append({"role": "assistant", "content": content})
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
        elif m["role"] == "assistant" and m.get("toolCalls"):
            # Attach the assistant's tool_calls so the following tool messages
            # have their matching call (required by OpenAI).
            omsgs.append({"role": "assistant", "content": m.get("content") or None,
                          "tool_calls": [{"id": c.get("id", "call_1"), "type": "function",
                                          "function": {"name": c.get("name"),
                                                       "arguments": json.dumps(c.get("input") or {})}}
                                         for c in m["toolCalls"]]})
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
