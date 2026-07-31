"""Governance Adapter — the keyless inference + governance boundary (Workstream E).

Under the AutoRentals SOW the Conversational AI Application is *keyless*: it holds
no model-provider API keys, secrets, or SDKs. The only credential it carries is a
governance-minted **Platform Token** (tenant-scoped, introspectable, revocable in
seconds). Every model call, metering event, and kill-check flows through this one
module to the Customer's governance system, which holds the real provider keys.
This is the sole boundary between the Application and inference; no direct-provider
path exists anywhere else (enforced by the keyless CI scanner, a Deliverable).

**Fails closed** (Acceptance Criterion 3): if the Platform Token is absent or
revoked, or governance is unreachable, the Adapter makes NO model call and raises
``E_GOVERNANCE_UNAVAILABLE``. Callers surface the Application's designed degraded
state and route users to existing channels. There is no bypass mode, no mock
fallback in keyless mode, and no credential-injection path.

The module mirrors the ``GovernanceAdapter`` interface proven in the reference
concierge (``streamTurn / complete / meter / killCheck``) so both implementations
converge on the Customer's frozen interface at the Interface Contracts milestone
(M2). Until then the wire envelope here is the pre-M2 stand-in; a local
mock-governance shim implements the far side for development and the fail-closed
UAT demonstration. Because the Adapter is one swappable module, freezing the real
interface is a config change, not a re-architecture.

Configuration (declared per environment, never read from ambient process state
inside the call — D8; the names below are the env keys ``rya.config`` resolves
into the adapter's ``ModelRoute``):
  RYA_GOVERNANCE_URL   the governance inference endpoint (Customer-controlled; the
                       Adapter's single, allowlisted egress target) -> route.base_url
  RYA_PLATFORM_TOKEN   the tenant-scoped Platform Token (the sole credential) -> route.api_key
  RYA_ADAPTER_MODE     "available" (default) | "unavailable" — forces fail-closed,
                       for the Criterion 3 governance-unavailability demonstration
                       -> route.options["adapter_mode"]
  RYA_KEYLESS          "1" makes resolve_provider refuse anthropic/openai even if a
                       provider key is present in the environment (see llm.py)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from ..config import ModelRoute, legacy_env
from ..errors import RyaError

FAIL_CLOSED = "E_GOVERNANCE_UNAVAILABLE"


def _fail_closed(reason: str) -> RyaError:
    return RyaError(
        FAIL_CLOSED,
        f"governance adapter unavailable: {reason}",
        hint="The Application fails closed: no model call is made. Surface the "
             "designed degraded state and route the user to existing channels.",
    )


# ---- configuration accessors (declared per environment; the Customer owns them) --
# Each takes the environment explicitly (D8). ``env=None`` means "resolve from the
# one transitional shim" (config.legacy_env) so un-migrated callers keep working.
def governance_url(env: Mapping[str, str] | None = None) -> str:
    url = (env if env is not None else legacy_env()).get("RYA_GOVERNANCE_URL")
    if not url:
        raise _fail_closed("RYA_GOVERNANCE_URL is not set")
    return url


def platform_token(env: Mapping[str, str] | None = None) -> str:
    tok = (env if env is not None else legacy_env()).get("RYA_PLATFORM_TOKEN")
    if not tok:
        raise _fail_closed("no Platform Token (RYA_PLATFORM_TOKEN unset or revoked)")
    return tok


def kill_check(*, mode: str | None = None, env: Mapping[str, str] | None = None) -> dict:
    """Whole-AI kill-check delegated to governance — returns ``{allowed, reason?}``.

    The stand-in honours ``RYA_ADAPTER_MODE=unavailable`` (the Criterion 3 demo);
    the production interface reads the governance system's kill decision. ``mode``
    is the value carried on the resolved route; ``env`` is the legacy path."""
    if mode is None:
        mode = (env if env is not None else legacy_env()).get("RYA_ADAPTER_MODE")
    if mode == "unavailable":
        return {"allowed": False, "reason": "governance system unavailable"}
    return {"allowed": True}


def assert_keyless(env: Mapping[str, str] | None = None) -> None:
    """Strong 'zero provider keys' guarantee (Criterion 2): when keyless mode is
    on, refuse to run if any provider credential is present in the environment.
    Cheap; called on every provider resolution and verifiable by inspection.

    ``env`` is the environment being *declared* for the run — the check has to see
    the same mapping the model call will use, or it guards the wrong world."""
    env = env if env is not None else legacy_env()
    if env.get("RYA_KEYLESS") != "1":
        return
    leaked = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                          "AWS_SECRET_ACCESS_KEY", "GOOGLE_API_KEY")
              if env.get(k)]
    if leaked:
        raise RyaError(
            "E_KEYLESS_VIOLATION",
            f"keyless mode is on but provider credential(s) present in env: {leaked}",
            hint="The keyless Application carries no provider keys — only the "
                 "Platform Token. Remove these from the deployment environment.",
        )


# ---- the single governed egress ---------------------------------------------
def _post(payload: dict, route: ModelRoute, timeout: int = 120):
    """POST to the governance endpoint with the Platform Token. Fails closed on a
    missing token, a rejected token (401/403 = revoked), or an unreachable
    governance system. Returns the raw urlopen response for the caller to read.

    Endpoint, credential and adapter mode all come off the resolved route (D8):
    the keyless boundary must not be able to pick up a *different* governance
    system from ambient process state than the one the run was granted."""
    from ..guard import check_egress

    url = route.base_url
    if not url:
        raise _fail_closed("RYA_GOVERNANCE_URL is not set")
    tok = route.api_key
    if not tok:
        raise _fail_closed("no Platform Token (RYA_PLATFORM_TOKEN unset or revoked)")
    kill = kill_check(mode=(route.options or {}).get("adapter_mode"))
    if not kill.get("allowed"):
        raise _fail_closed(kill.get("reason") or "killed")
    check_egress(url, "POST")  # Action Guard: governance is the only allowed egress
    headers = {
        "Authorization": f"Bearer {tok}",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, default=str).encode(), headers=headers, method="POST"
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _fail_closed(f"Platform Token rejected ({e.code})") from e
        body = e.read().decode(errors="replace")[:300]
        raise _fail_closed(f"governance HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise _fail_closed(f"governance unreachable: {e.reason}") from e


def _sse(resp):
    """Yield parsed ``data: {...}`` JSON events from a governance SSE stream."""
    for raw in resp:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def meter(event: dict) -> None:
    """Best-effort metering event. Stand-in emits structured stdout; production
    posts to the governance metering endpoint through the frozen interface."""
    try:
        print(json.dumps({"meter": event}, default=str))
    except Exception:
        pass


# ---- the two shapes Rya's provider seam calls (llm.py) ----------------------
# adapter_respond mirrors ``streamTurn``/``complete``; adapter_chat mirrors a
# tool-calling ``streamTurn``. The Application stays provider-agnostic: it sends
# neutral messages/tools and governance owns provider-specific formatting.
def adapter_respond(route: ModelRoute, system, content, temperature, max_tokens, on_token=None) -> dict:
    name = route.model
    payload = {
        "purpose": "compose",
        "model": name,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens or 1024,
        "stream": bool(on_token),
    }
    if temperature is not None:
        payload["temperature"] = temperature
    resp = _post(payload, route)
    if on_token:
        parts, usage = [], None
        with resp:
            for ev in _sse(resp):
                if ev.get("type") == "text_delta":
                    chunk = ev.get("text", "")
                    if chunk:
                        parts.append(chunk)
                        on_token(chunk)
                elif ev.get("type") == "message_done":
                    usage = ev.get("usage")
        text = "".join(parts)
    else:
        with resp:
            body = json.loads(resp.read().decode())
        text, usage = body.get("text", ""), body.get("usage")
    meter({"kind": "model_call", "model": name, "usage": usage})
    return {"text": text, "model": name, "provider": "adapter", "usage": usage}


def adapter_chat(route: ModelRoute, system, messages, tools, temperature, max_tokens) -> dict:
    name = route.model
    payload = {
        "purpose": "tools",
        "model": name,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens or 1024,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    resp = _post(payload, route)
    with resp:
        body = json.loads(resp.read().decode())
    calls = [{"id": c.get("id"), "name": c.get("name"), "input": c.get("input") or {}}
             for c in (body.get("tool_calls") or [])]
    usage = body.get("usage")
    meter({"kind": "model_call", "model": name, "usage": usage})
    return {"text": body.get("text", ""), "toolCalls": calls, "model": name,
            "provider": "adapter", "usage": usage}
