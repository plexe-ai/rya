"""LLM provider seam for ``ctx.llm`` — real multi-provider, mock fallback.

Pattern borrowed from openclaw's `createLlm` factory: the manifest declares
``model.provider`` (auto | mock | anthropic | openai) and a model name; the
factory returns the matching provider. Real providers are called over plain HTTP
(stdlib ``urllib``, no SDK dependency), so the real path works on a base install.

- ``auto`` (default): real if an API key is present, else mock — zero-config dev.
- ``mock``: deterministic offline stub (keeps tests + CI reproducible).
- ``anthropic`` / ``openai``: require the matching API key; error clearly if missing.

**D8 — run inputs are declared, not ambient.** Which world a call talks to, which
model it names, and which credential it carries all arrive as a resolved
``config.ModelRoute``; this module reads no environment of its own. Callers that
have not been re-pointed yet get a route resolved at the entry point from
``config.legacy_env()`` (the single transitional shim, see ``_route_for``), so
behaviour is unchanged while ``sdk/context.py`` migrates to passing ``route=``.
That is what makes the mock/real decision testable: with an explicit route (or an
explicit ``env=``), an ambient ``ANTHROPIC_API_KEY`` can no longer swap a
deterministic mock for a billed model call.

The call is journaled by the runtime, so replays after an approval pause never
re-bill the model.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from ..config import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_OPENAI_MODEL,
    PLACEHOLDER_MODEL_NAMES,
    ModelRoute,
    legacy_env,
    resolve_model_route,
    with_call_params,
)
from ..errors import RyaError

# Back-compat aliases: the names stayed, the resolution they feed moved to
# rya.config (D8) — placeholder names and per-provider default models are part of
# resolving a route, not of making the HTTP call.
_PLACEHOLDER_NAMES = PLACEHOLDER_MODEL_NAMES
_DEFAULT_ANTHROPIC = DEFAULT_ANTHROPIC_MODEL
_DEFAULT_OPENAI = DEFAULT_OPENAI_MODEL


def _route_for(*, provider: str = "auto", model: str = "mock-llm",
               temperature: float | None = None, max_tokens: int | None = None,
               route: ModelRoute | None = None,
               env: Mapping[str, str] | None = None) -> ModelRoute:
    """The one resolution boundary in this module (D8).

    A caller that already resolved its config passes ``route=`` (the explicit
    path) or ``env=`` (an explicit mapping). Only when it passes neither do we
    fall back to ``legacy_env()`` — the single, deliberately named ambient read
    that keeps un-migrated callers working. Nothing below this function ever asks
    the environment anything.
    """
    if route is not None:
        return with_call_params(route, temperature=temperature, max_tokens=max_tokens)
    return resolve_model_route(provider=provider, model=model, temperature=temperature,
                               max_tokens=max_tokens,
                               env=env if env is not None else legacy_env())


def resolve_provider(provider: str = "auto", *, env: Mapping[str, str] | None = None) -> str:
    """Resolve ``auto`` to a concrete provider based on which API key is present.

    Keyless mode (``RYA_KEYLESS=1``) forces the Governance Adapter: it refuses
    ``anthropic``/``openai`` even if a key is present, so a leaked credential is
    never used. ``mock`` stays available for offline dev/tests (it holds no key),
    and an explicit ``adapter`` always routes to the keyless path.

    The precedence itself now lives in ``config._concrete_provider``, reading a
    supplied ``env`` mapping; pass ``env=`` to resolve against declared config
    instead of the process environment."""
    return _route_for(provider=provider, env=env).provider


# Back-compat: earlier code called active_provider() with no manifest context.
def active_provider(*, env: Mapping[str, str] | None = None) -> str:
    return resolve_provider("auto", env=env)


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
                       hint="Check the API key, model name, and quota.") from e
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"LLM request failed: {e.reason}",
                       hint="Check network egress to the provider.") from e


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
        if spec.get("enum"):
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


def _as_route(route, provider: str) -> ModelRoute:
    """Back-compat for the per-provider helpers: they used to take a bare model
    name as their first argument and look the credential up themselves. Callers
    that still pass a string get a route resolved through the same single shim, so
    the explicit ``ModelRoute`` remains the only new API surface."""
    if isinstance(route, str):
        return resolve_model_route(provider=provider, model=route, env=legacy_env())
    return route


def _require_key(route: ModelRoute, env_name: str, subject: str) -> str:
    """The credential for this route, or the same clear error as before. The key
    comes from the resolved route (D8), so the message names the env var only to
    tell a developer what to declare."""
    if not route.api_key:
        raise RyaError("E_VALIDATION", f"{subject} but {env_name} is not set.",
                       hint=f"Set {env_name}, or use model.provider: mock for offline dev.")
    return route.api_key


def _anthropic(route, system, content, temperature, max_tokens) -> dict:
    route = _as_route(route, "anthropic")
    key = _require_key(route, "ANTHROPIC_API_KEY", "model.provider is 'anthropic'")
    model = route.model
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


def _openai(route, system, content, temperature, max_tokens) -> dict:
    route = _as_route(route, "openai")
    key = _require_key(route, "OPENAI_API_KEY", "model.provider is 'openai'")
    model = route.model
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
                       hint="Check the API key, model name, and quota.") from e
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"LLM request failed: {e.reason}",
                       hint="Check network egress to the provider.") from e


def _anthropic_stream(route, system, content, temperature, max_tokens, on_token) -> dict:
    route = _as_route(route, "anthropic")
    key = _require_key(route, "ANTHROPIC_API_KEY", "model.provider is 'anthropic'")
    model = route.model
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


def _openai_stream(route, system, content, temperature, max_tokens, on_token) -> dict:
    route = _as_route(route, "openai")
    key = _require_key(route, "OPENAI_API_KEY", "model.provider is 'openai'")
    model = route.model
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
            temperature: float | None = None, max_tokens: int | None = None,
            schema: dict | None = None, on_token=None, documents: list | None = None,
            route: ModelRoute | None = None, env: Mapping[str, str] | None = None) -> dict:
    """Return ``{text, model, provider, usage[, json]}``. Mock path never raises.

    When ``schema`` (a JSON Schema) is given, the model is asked for JSON and the
    result is parsed + validated into ``json`` — first-class structured output.
    When ``on_token`` is given, the response is generated via the provider's
    streaming API and each text chunk is delivered to the callback as it
    arrives; the returned dict is identical to the non-streaming shape.

    Pass ``route`` (a resolved ``config.ModelRoute``) for the D8 path: the call
    then depends on nothing ambient. ``model_default``/``provider`` remain for
    callers that still declare the choice inline."""
    r = _route_for(provider=provider, model=model_default, temperature=temperature,
                   max_tokens=max_tokens, route=route, env=env)
    effective, temperature, max_tokens = r.provider, r.temperature, r.max_tokens
    sys = system
    if schema is not None and effective != "mock":
        sys = (system + "\n\nRespond with ONLY a JSON object matching this JSON Schema "
               "(no prose, no code fences):\n" + json.dumps(schema))
    content = _stringify(input)
    if documents and effective not in ("bedrock", "mock"):
        raise RyaError("E_VALIDATION", f"documents are not supported on the '{effective}' provider yet.",
                       hint="Use model.provider: bedrock for document-grounded calls.")
    if effective == "bedrock":
        out = (_bedrock_stream(r, sys, content, temperature,
                               max_tokens or (1024 if schema else None), on_token, documents)
               if on_token else
               _bedrock(r, sys, content, temperature,
                        max_tokens or (1024 if schema else None), documents))
    elif effective == "anthropic":
        out = (_anthropic_stream(r, sys, content, temperature,
                                 max_tokens or (1024 if schema else None), on_token)
               if on_token else
               _anthropic(r, sys, content, temperature, max_tokens or (1024 if schema else None)))
    elif effective == "openai":
        out = (_openai_stream(r, sys, content, temperature, max_tokens, on_token)
               if on_token else
               _openai(r, sys, content, temperature, max_tokens))
    elif effective == "adapter":
        # Keyless path: no provider key, only the Platform Token; fails closed.
        from . import adapter as _adapter
        out = _adapter.adapter_respond(r, sys, content, temperature,
                                       max_tokens or (1024 if schema else None), on_token)
    else:
        text = _mock_stream(system, input, on_token) if on_token else _mock_text(system, input)
        out = {"text": text, "model": r.model, "provider": "mock", "usage": None}
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
_DEFAULT_BEDROCK = DEFAULT_BEDROCK_MODEL


def _bedrock_client(region: str = ""):
    try:
        import boto3  # optional dep: pip install 'rya[bedrock]'
    except ImportError:
        raise RyaError("E_VALIDATION", "model.provider is 'bedrock' but boto3 is not installed.",
                       hint="pip install 'rya[bedrock]' (or add boto3 to the project).") from None
    if not region:
        # No Rya-declared region on the route: fall back to the AWS chain the
        # ambient IAM identity already uses (the D8 shim — see config.legacy_env).
        env = legacy_env()
        region = (env.get("RYA_BEDROCK_REGION") or env.get("AWS_REGION")
                  or env.get("AWS_DEFAULT_REGION") or "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def _bedrock_runtime(route: ModelRoute):
    """Bedrock client for ``route``'s region. ``_bedrock_client`` keeps its
    zero-argument call shape (tests patch that symbol with a zero-arg fake), so a
    region is passed only when the route actually declares one."""
    return _bedrock_client(route.region) if route.region else _bedrock_client()


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


def _bedrock(route, system, content, temperature, max_tokens, documents=None) -> dict:
    route = _as_route(route, "bedrock")
    client = _bedrock_runtime(route)
    model = route.model
    inference = {"maxTokens": max_tokens or 1024}
    if temperature is not None:
        inference["temperature"] = temperature
    try:
        j = client.converse(
            modelId=model,
            system=[{"text": system}] if system else [],
            messages=[{"role": "user", "content": [*_doc_blocks(documents), {"text": content}]}],
            inferenceConfig=inference,
        )
    except RyaError:
        raise
    except Exception as e:
        raise _bedrock_error(e) from e
    text = "".join(b.get("text", "") for b in j["output"]["message"]["content"] if "text" in b)
    u = j.get("usage") or {}
    return {"text": text, "model": model, "provider": "bedrock",
            "usage": {"input": u.get("inputTokens"), "output": u.get("outputTokens")} if u else None}


def _bedrock_stream(route, system, content, temperature, max_tokens, on_token, documents=None) -> dict:
    route = _as_route(route, "bedrock")
    client = _bedrock_runtime(route)
    model = route.model
    inference = {"maxTokens": max_tokens or 1024}
    if temperature is not None:
        inference["temperature"] = temperature
    try:
        resp = client.converse_stream(
            modelId=model,
            system=[{"text": system}] if system else [],
            messages=[{"role": "user", "content": [*_doc_blocks(documents), {"text": content}]}],
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
        raise _bedrock_error(e) from e
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


def _bedrock_chat(route, system, messages, tools, temperature, max_tokens) -> dict:
    import re

    route = _as_route(route, "bedrock")
    client = _bedrock_runtime(route)
    model = route.model
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
        raise _bedrock_error(e) from e
    blocks = j["output"]["message"]["content"]
    text = "".join(b.get("text", "") for b in blocks if "text" in b)
    calls = [{"id": b["toolUse"].get("toolUseId"),
              "name": name_map.get(b["toolUse"].get("name"), b["toolUse"].get("name")),
              "input": b["toolUse"].get("input", {})} for b in blocks if "toolUse" in b]
    u = j.get("usage") or {}
    return {"text": text, "toolCalls": calls, "model": model, "provider": "bedrock",
            "usage": {"input": u.get("inputTokens"), "output": u.get("outputTokens")} if u else None}


# ---- tool-calling chat (the governed agent loop's model step) --------------
def _mock_chat(messages: list, tools: list | None) -> dict:
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


def chat(*, messages: list, tools: list | None = None, system: str = "",
         model_default: str = "mock-llm", provider: str = "auto",
         temperature: float | None = None, max_tokens: int | None = None,
         on_token=None, route: ModelRoute | None = None,
         env: Mapping[str, str] | None = None) -> dict:
    """One model turn that may request tool calls. Returns
    ``{text, toolCalls:[{id,name,input}], model, provider, usage}``.

    ``messages`` is a list of ``{role: user|assistant|tool, content, [name]}``.
    The mock provider drives a deterministic call-one-tool-then-answer loop;
    real providers use the native tool-use / function-calling format.

    When ``on_token`` is given, the assistant's text is streamed chunk by chunk
    to the callback as it arrives (currently the anthropic provider); the
    returned dict is identical to the non-streaming shape. Providers without a
    streaming chat path ignore ``on_token`` and return the full result at once,
    so passing it is always safe.

    ``route`` is the D8 path (a resolved ``config.ModelRoute``); ``provider`` /
    ``model_default`` stay for callers that declare the choice inline."""
    r = _route_for(provider=provider, model=model_default, temperature=temperature,
                   max_tokens=max_tokens, route=route, env=env)
    effective, temperature, max_tokens = r.provider, r.temperature, r.max_tokens
    if effective == "bedrock":
        return _bedrock_chat(r, system, messages, tools, temperature, max_tokens)
    if effective == "anthropic":
        if on_token:
            return _anthropic_chat_stream(r, system, messages, tools,
                                          temperature, max_tokens, on_token)
        return _anthropic_chat(r, system, messages, tools, temperature, max_tokens)
    if effective == "openai":
        return _openai_chat(r, system, messages, tools, temperature, max_tokens)
    if effective == "adapter":
        # Keyless path: no provider key, only the Platform Token; fails closed.
        from . import adapter as _adapter
        return _adapter.adapter_chat(r, system, messages, tools, temperature, max_tokens)
    return _mock_chat(messages, tools)


def _anthropic_messages(messages) -> list:
    """Shape Rya's ``{role, content, [toolCalls|toolUseId]}`` messages into the
    Anthropic content-block format. Shared by the non-streaming and streaming
    chat paths so they can never diverge."""
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
    return amsgs


def _anthropic_tools(tools) -> list:
    return [{"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("input_schema", {"type": "object"})} for t in tools]


def _anthropic_chat(route, system, messages, tools, temperature, max_tokens) -> dict:
    route = _as_route(route, "anthropic")
    key = _require_key(route, "ANTHROPIC_API_KEY", "anthropic provider")
    model = route.model
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system,
               "messages": _anthropic_messages(messages)}
    if tools:
        payload["tools"] = _anthropic_tools(tools)
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


def _anthropic_chat_stream(route, system, messages, tools, temperature, max_tokens, on_token) -> dict:
    """Streaming twin of ``_anthropic_chat``: emits assistant text deltas to
    ``on_token`` as they arrive, while assembling the SAME return shape
    ``{text, toolCalls, model, provider, usage}``. Tool-use blocks are rebuilt
    from ``input_json_delta`` fragments exactly as the non-streaming path parses
    them, so a tool-calling step behaves identically - only the final text is
    additionally streamed."""
    route = _as_route(route, "anthropic")
    key = _require_key(route, "ANTHROPIC_API_KEY", "anthropic provider")
    model = route.model
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system,
               "messages": _anthropic_messages(messages), "stream": True}
    if tools:
        payload["tools"] = _anthropic_tools(tools)
    if temperature is not None:
        payload["temperature"] = temperature
    resp = _http_stream("https://api.anthropic.com/v1/messages",
                        {"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json", "accept": "text/event-stream"}, payload)
    blocks: dict = {}  # index -> {"type", "text"} | {"type":"tool_use","id","name","json"}
    usage_in = usage_out = None
    with resp:
        for ev in _sse_events(resp):
            t = ev.get("type")
            if t == "message_start":
                usage_in = (ev.get("message", {}).get("usage") or {}).get("input_tokens")
            elif t == "content_block_start":
                idx = ev.get("index")
                cb = ev.get("content_block", {}) or {}
                if cb.get("type") == "tool_use":
                    blocks[idx] = {"type": "tool_use", "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                else:
                    blocks[idx] = {"type": "text", "text": ""}
            elif t == "content_block_delta":
                b = blocks.get(ev.get("index"))
                if b is None:
                    continue
                d = ev.get("delta", {}) or {}
                if d.get("type") == "text_delta":
                    chunk = d.get("text", "")
                    if chunk:
                        b["text"] = b.get("text", "") + chunk
                        if on_token:
                            on_token(chunk)
                elif d.get("type") == "input_json_delta":
                    b["json"] = b.get("json", "") + d.get("partial_json", "")
            elif t == "message_delta":
                usage_out = (ev.get("usage") or {}).get("output_tokens")
    ordered = [blocks[i] for i in sorted(blocks)]
    text = "".join(b.get("text", "") for b in ordered if b["type"] == "text")
    calls = []
    for b in ordered:
        if b["type"] == "tool_use":
            try:
                inp = json.loads(b["json"]) if b.get("json") else {}
            except json.JSONDecodeError:
                inp = {}
            calls.append({"id": b.get("id"), "name": b.get("name"), "input": inp})
    return {"text": text, "toolCalls": calls, "model": model, "provider": "anthropic",
            "usage": {"input": usage_in, "output": usage_out} if (usage_in or usage_out) else None}


def _openai_chat(route, system, messages, tools, temperature, max_tokens) -> dict:
    route = _as_route(route, "openai")
    key = _require_key(route, "OPENAI_API_KEY", "openai provider")
    model = route.model
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
