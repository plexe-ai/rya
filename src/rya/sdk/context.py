"""The runtime context (``ctx``) handed to every agent handler.

Durable-execution model
------------------------
Each side-effecting / observable ctx operation is a *step* routed through
``_step``. Steps are journaled on the run by an integer sequence number, in call
order. When a run pauses for approval and later resumes, the engine simply
re-invokes the handler: ``_step`` returns the memoized result for every step it
already completed, so prior tool calls, model calls, memory writes, and logs are
NOT re-executed — only the code after the approval point runs for real.

This is what makes ``ctx.approvals.request`` able to pause a coroutine and have
the run continue correctly in a *different* CLI invocation. The constraint is
the standard one for durable execution: handlers must issue ctx operations in a
deterministic order.

In the local slice all external effects (tools, models, llm, channels) are
deterministic mocks.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import dotenv_values

from ..approvals import PausedForApproval, ApprovalRejected
from ..errors import RyaError
from ..manifest.schema import Manifest, Permission
from ..models.registry import ModelRegistry
from ..observability.logs import emit_log
from ..store import Store, now_iso, _new_id
from ..tools.registry import ToolRegistry


@dataclass
class Event:
    id: str
    type: str
    source: str
    agentId: str
    payload: dict
    createdAt: str

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            id=d["id"],
            type=d["type"],
            source=d.get("source", "manual"),
            agentId=d.get("agentId", ""),
            payload=d.get("payload", {}),
            createdAt=d.get("createdAt", now_iso()),
        )

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "agentId": self.agentId,
            "payload": self.payload,
            "createdAt": self.createdAt,
        }


@dataclass
class LLMResponse:
    text: str
    model: str
    json: Optional[dict] = None      # parsed + validated object when a schema was given
    provider: Optional[str] = None


def load_env(root: Path) -> Dict[str, str]:
    """Merge ``.env`` (project root) with the process environment."""
    values = dict(dotenv_values(root / ".env"))
    values.update({k: v for k, v in os.environ.items()})
    return values


def _http_tool(url: str, input: dict, auth_secret: Optional[str] = None) -> dict:
    """Execute an HTTP tool: POST the input as JSON, return the JSON response.

    If ``auth_secret`` is provided (a scoped connection credential resolved by the
    runtime), it is injected as a bearer token — the credential travels to the
    upstream tool but is never placed in the input the handler/model can see."""
    import json as _json
    import urllib.error
    import urllib.request

    from ..guard import check_egress
    check_egress(url, "POST")  # Action Guard — blocked requests never leave the process

    headers = {"content-type": "application/json"}
    if auth_secret:
        headers["Authorization"] = f"Bearer {auth_secret}"
    req = urllib.request.Request(url, data=_json.dumps(input, default=str).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        # A 401 on a request we authenticated means the injected connection
        # credential is expired/invalid: surface a typed reconnect signal (the
        # runtime maps it to a `needs_reconnect` outcome) so the caller is asked
        # to reconnect. No auto-refresh. A 401 with no
        # credential is just a plain upstream error.
        if e.code == 401 and auth_secret:
            raise RyaError("E_CONNECTION_EXPIRED", f"upstream rejected the credential (HTTP 401): {body}",
                           hint="The connection has expired — reconnect (log in again) and retry.",
                           http_status=401)
        # Carry the status so the retry primitive can classify a 5xx as transient.
        raise RyaError("E_TOOL_UPSTREAM", f"tool HTTP {e.code}: {body}",
                       hint="Check the tool URL / payload.", http_status=e.code)
    except urllib.error.URLError as e:
        # A socket timeout surfaces here as URLError(reason=timeout); tag it as a
        # timeout class so a retry policy that lists `timeout` re-tries it.
        import socket
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise RyaError("E_TIMEOUT", f"tool request timed out: {e.reason}",
                           hint="Upstream did not respond in time.")
        raise RyaError("E_RUNTIME", f"tool request failed: {e.reason}", hint="Check network egress.")


class RuntimeContext:
    def __init__(
        self,
        *,
        store: Store,
        manifest: Manifest,
        run: dict,
        tools: ToolRegistry,
        models: ModelRegistry,
        project_root: Path,
        identity=None,
        agent=None,
        on_trace=None,
        on_token=None,
        on_ui=None,
    ) -> None:
        self.store = store
        # Optional live trace subscriber — fired on every trace event as it
        # happens (the WebSocket surface streams a run to the client in real time).
        self._on_trace = on_trace
        # Optional token subscriber — fired with each streamed LLM text chunk.
        # Tokens are NOT journaled (only the final response is), so a replay
        # after an approval pause never re-streams.
        self._on_token = on_token
        # Optional UI subscriber — fired when the handler emits a custom UI frame
        # (a card, form, chart) via ctx.emit_ui. Journaled through _step, so a
        # replay after an approval pause never re-emits it.
        self._on_ui = on_ui
        self.manifest = manifest
        self.run = run
        self._tools = tools
        self._models = models
        self._agent = agent
        self.project_root = project_root
        self.identity = identity  # verified user Identity, or None
        self._seq = 0
        self._env = load_env(project_root)

        # Secret-redaction vault (pattern from openclaw's SecretVault): collect
        # secret values so they can be scrubbed from every trace/log before it is
        # persisted or printed. Seeded with known API keys; grows as the handler
        # reads more via ctx.secrets.get.
        self._secrets_seen: set = set()
        for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            self._seed_secret(self._env.get(_k))

        # Public interfaces (the spec's ctx.* surface).
        self.llm = _LLM(self)
        self.models = _Models(self)
        self.memory = _Memory(self)
        self.knowledge = _Knowledge(self)
        self.tools = _Tools(self)
        self.channels = _Channels(self)
        self.jobs = _Jobs(self)
        self.cron = _Cron(self)
        self.approvals = _Approvals(self)
        self.sessions = _Sessions(self)
        self.files = _Files(self)
        self.connections = _Connections(self)
        self.logs = _Logs(self)
        self.traces = _Traces(self)
        self.secrets = _Secrets(self)
        self.events = _Events(self)
        self.guard = _Guard(self)

    # ---- core journaling ----------------------------------------------
    def _step(self, kind: str, label: str, fn: Callable[[], Any], data: Optional[dict] = None) -> Any:
        seq = self._seq
        self._seq += 1
        key = str(seq)
        entry = self.run["journal"].get(key)
        if entry is not None and entry.get("status") == "done":
            return entry.get("result")  # memoized replay — no re-execution, no new trace
        result = fn()
        self.run["journal"][key] = {
            "seq": seq,
            "kind": kind,
            "label": label,
            "status": "done",
            "result": result,
        }
        self._trace(kind, label, {**(data or {}), "result": result})
        self.store.save_run(self.run)
        return result

    async def _astep(self, kind: str, label: str, afn, data: Optional[dict] = None):
        """Async sibling of _step for awaiting real (async) tool handlers. The
        handler must be a leaf (no nested journaled ctx calls) — same determinism
        rule as the rest of the runtime."""
        seq = self._seq
        self._seq += 1
        key = str(seq)
        entry = self.run["journal"].get(key)
        if entry is not None and entry.get("status") == "done":
            return entry.get("result")
        result = await afn()
        self.run["journal"][key] = {"seq": seq, "kind": kind, "label": label,
                                    "status": "done", "result": result}
        self._trace(kind, label, {**(data or {}), "result": result})
        self.store.save_run(self.run)
        return result

    def _trace(self, kind: str, label: str, data: dict) -> None:
        entry = {
            "seq": len(self.run["trace"]),
            "ts": now_iso(),
            "kind": kind,
            "label": self._redact(label),
            "data": self._redact(data),
        }
        self.run["trace"].append(entry)
        if self._on_trace is not None:
            try:
                self._on_trace(entry)
            except Exception:  # never let a subscriber break the run
                pass

    def emit_ui(self, component: str, data: Optional[dict] = None) -> dict:
        """Emit a first-class UI frame to the turn stream — a card, form, chart,
        or any component the frontend renders. Lands as a ``ui`` frame (SSE
        ``event: ui`` / WebSocket ``{"type":"ui"}``) with ``{component, data}``,
        so the client never has to scrape tool-call traces to build custom UI.

        Journaled: a replay after an approval pause returns the recorded frame
        and does NOT re-emit, exactly like tool/model steps."""
        payload = {"component": component, "data": self._redact(data or {})}

        def run():
            if self._on_ui is not None:
                try:
                    self._on_ui(payload)
                except Exception:  # never let a subscriber break the run
                    pass
            return payload

        return self._step("ui.emit", component, run, {"component": component})

    # ---- secret redaction ---------------------------------------------
    def _seed_secret(self, value) -> None:
        # Only redact non-trivial values, to avoid scrubbing short common strings.
        if isinstance(value, str) and len(value) >= 6:
            self._secrets_seen.add(value)

    def _redact(self, obj):
        if not self._secrets_seen:
            return obj
        if isinstance(obj, str):
            out = obj
            for s in self._secrets_seen:
                out = out.replace(s, "«redacted»")
            return out
        if isinstance(obj, dict):
            return {k: self._redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact(v) for v in obj]
        return obj

    # ---- approvals (special: can suspend the run) ---------------------
    def _request_approval(self, title: str, body: str, action: dict) -> dict:
        seq = self._seq
        self._seq += 1
        key = str(seq)
        entry = self.run["journal"].get(key)
        if entry is not None:
            status = entry.get("status")
            if status == "approved":
                return entry["result"]
            if status == "rejected":
                raise ApprovalRejected(entry["result"]["approvalId"])
            # still pending -> re-suspend (run was resumed before approval resolved)
            raise PausedForApproval(entry["result"]["approvalId"])

        approval = self.store.create_approval(self.run["id"], title, body, action)
        self.run["journal"][key] = {
            "seq": seq,
            "kind": "approval",
            "label": title,
            "status": "pending",
            "result": {"approvalId": approval["id"]},
        }
        self._trace("approval.requested", title, {"approvalId": approval["id"], "action": action})
        self.run["status"] = "waiting_approval"
        self.run["pendingApproval"] = approval["id"]
        self.store.save_run(self.run)
        raise PausedForApproval(approval["id"])

    # ---- scoped connected credentials (the intersection rule) ---------
    def _authorize_connection(self, decl) -> Optional[str]:
        """Resolve and authorize the scoped credential a tool needs.

        Implements the agent-authority intersection rule: a tool's effective
        authority is ``connection scopes ∩ requesting-user scopes``. The tool
        may run only if its required ``scopes`` are within that intersection.
        Returns the secret to inject (seeded into the redaction vault first, so
        it can never leak into a trace), or None if the tool declares no
        provider. Raises ``E_NO_CONNECTION`` / ``E_SCOPE_DENIED`` otherwise.
        """
        provider = getattr(decl, "provider", None) if decl is not None else None
        if not provider:
            return None
        owner = self.identity.sub if self.identity is not None else None
        # Fail closed on missing identity: a `require_user` tool must resolve a
        # per-user connection. Without a verified `sub`, get_connection would fall
        # through to a workspace-shared credential — a silent attribution leak (one
        # user acting under another's API token). Refuse instead.
        if getattr(decl, "require_user", False) and not owner:
            raise RyaError(
                "E_NO_IDENTITY",
                f"Tool needs a verified user to use its '{provider}' connection, "
                "but no user identity was presented.",
                hint="Forward the signed X-Rya-User-Token so the per-user credential resolves.",
            )
        conn = self.store.get_connection(provider, owner) if hasattr(self.store, "get_connection") else None
        if conn is None or conn.get("status") != "active":
            raise RyaError(
                "E_NO_CONNECTION",
                f"No active '{provider}' connection for this agent/user.",
                hint=f"Create one: `rya connect {provider} --scopes <...> --token <secret>`.",
            )
        required = set(getattr(decl, "scopes", []) or [])
        conn_scopes = set(conn.get("scopes", []))
        user_scopes = self.identity.scopes if self.identity is not None else None
        # user_scopes None = no scope claim on the token → user is unrestricted.
        effective = conn_scopes if user_scopes is None else (conn_scopes & set(user_scopes))
        missing = required - effective
        if missing:
            raise RyaError(
                "E_SCOPE_DENIED",
                f"Tool requires {sorted(required)} on '{provider}', but the effective "
                f"grant (connection ∩ user) is {sorted(effective)}; missing {sorted(missing)}.",
                hint="Grant the missing scopes on the connection, or authorize the user for them.",
            )
        secret = conn.get("secret")
        self._seed_secret(secret)  # vault it — never leaks into traces/logs
        return secret

    # ---- permission resolution ----------------------------------------
    def _effective_tool_permission(self, tool_id: str) -> Optional[Permission]:
        """Manifest permission, unless a runtime kill switch overrides it.

        Overrides live in the `_runtime_config` memory scope (versioned,
        append-only history) so an operator can disable a misbehaving tool
        NOW, without a redeploy. See PUT /tools/{id}/permission."""
        try:
            rc = self.store.load_memory("_runtime_config")
            ov = (rc.get("kv") or {}).get(f"tool:{tool_id}")
            if ov and ov.get("permission"):
                return Permission(ov["permission"])
        except Exception:
            # An unreadable runtime config fails CLOSED: safer to refuse tools
            # than to run one an operator may have just killed.
            return Permission.disabled
        return self.manifest.tool_permission(tool_id)

    def _resolve_tool_permission(self, tool_id: str) -> Permission:
        perm = self._effective_tool_permission(tool_id)
        if perm is None:
            raise RyaError(
                "E_TOOL_NOT_FOUND",
                f"Tool '{tool_id}' is not declared in the manifest.",
                hint="Add it under `tools:` in rya.agent.yaml with an explicit permission.",
            )
        return perm

    # ---- server-side arg pinning ----------------------------------------
    def _resolve_pin(self, source: str):
        """Resolve one ToolDecl.pin source to its trusted value.

        Supported: "event.<path>" (dotted path into the triggering event),
        "memory.<scope>.<key>", "identity.sub", else a literal."""
        if source.startswith("event."):
            node = self.run.get("event") or {}
            for part in source.split(".")[1:]:
                node = node.get(part) if isinstance(node, dict) else None
            return node
        if source.startswith("memory."):
            parts = source.split(".", 2)
            if len(parts) == 3:
                _, scope, key = parts
                return (self.store.load_memory(scope).get("kv") or {}).get(key)
            return None
        if source == "identity.sub":
            return self.identity.sub if self.identity is not None else None
        return source


# --------------------------------------------------------------------------
# Sub-interfaces. Each is a thin adapter over RuntimeContext._step.
# --------------------------------------------------------------------------
class _LLM:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def _params(self, route: Optional[str]):
        """Resolve (model, provider, temperature, max_tokens, label) for a call.
        ``route`` picks a named per-purpose model from model.routes (compose vs
        extract vs classify); unset route fields inherit from the model block."""
        mb = self._ctx.manifest.model
        if route is None:
            return mb.default, mb.provider, mb.temperature, mb.max_tokens, mb.default
        r = (mb.routes or {}).get(route)
        if r is None:
            raise RyaError(
                "E_MODEL_ROUTE_NOT_FOUND",
                f"Model route '{route}' is not declared (have: {sorted(mb.routes or {})}).",
                hint="Add it under `model.routes:` in rya.agent.yaml.",
            )
        return (r.model, r.provider or mb.provider,
                r.temperature if r.temperature is not None else mb.temperature,
                r.max_tokens or mb.max_tokens, f"{route}:{r.model}")

    async def respond(self, *, system: str, input: dict, schema: Optional[dict] = None,
                      route: Optional[str] = None,
                      documents: Optional[list] = None) -> LLMResponse:
        """Call the model. Pass ``schema`` (a JSON Schema) for first-class
        **structured output** — the result's ``.json`` is the parsed, validated
        object. Pass ``route`` to use a named per-purpose model from
        ``model.routes``. When the run has a token subscriber (WebSocket turns),
        the response is streamed chunk by chunk as it is generated.

        ``documents`` grounds the call in files (extraction over PDFs): a list of
        ``{name, format, path|bytes|b64}``; relative paths resolve against the
        project root. Supported on the bedrock provider (Converse document
        blocks); the mock provider ignores them."""
        from ..providers import respond as provider_respond

        mb = self._ctx.manifest.model
        model, provider, temperature, max_tokens, label = self._params(route)
        on_token = self._ctx._on_token
        docs = None
        if documents:
            docs = []
            for d in documents:
                d = dict(d)
                if d.get("path") and not os.path.isabs(d["path"]):
                    d["path"] = str(self._ctx.project_root / d["path"])
                docs.append(d)

        def run():
            # Provider chosen by manifest model.provider (auto/mock/anthropic/openai/bedrock).
            try:
                return provider_respond(
                    system=system, input=input, model_default=model, provider=provider,
                    temperature=temperature, max_tokens=max_tokens, schema=schema,
                    on_token=on_token, documents=docs,
                )
            except RyaError:
                # Fall back to the manifest's fallback model on provider failure
                # (default route only — named routes are explicit choices).
                if route is None and mb.fallback:
                    out = provider_respond(
                        system=system, input=input, model_default=mb.fallback, provider=provider,
                        temperature=temperature, max_tokens=max_tokens, schema=schema,
                        on_token=on_token, documents=docs,
                    )
                    out["fellBackFrom"] = mb.default
                    return out
                raise

        # Journal document names only - never raw bytes (the journal is replayed
        # and shipped to observability backends).
        step_data = {"system": system}
        if documents:
            step_data["documents"] = [d.get("name") or d.get("path") for d in documents]
        res = self._ctx._step("llm.respond", label, run, step_data)
        return LLMResponse(text=res["text"], model=res["model"], json=res.get("json"),
                           provider=res.get("provider"))

    async def run(self, *, input, system: str = "", tools: Optional[List[str]] = None,
                  max_steps: int = 6, route: Optional[str] = None) -> dict:
        """Governed **agent loop**: the model reasons and calls tools until it has
        an answer. Every tool call goes through ``ctx.tools.call`` — so the same
        permissions, scoped credentials, Action Guard, and audit apply to what the
        model decides to do. Approval-gated tools are NOT exposed to the loop;
        side-effectful actions still require an explicit ``ctx.approvals.request``.

        Returns ``{text, steps, toolCalls}`` where ``toolCalls`` lists what ran.
        """
        from ..providers import chat as provider_chat

        model, provider, temperature, max_tokens, _label = self._params(route)
        # Only non-gated tools are autonomous; the model never sees gated actions.
        # Effective permission (manifest + runtime kill switches), so a tool an
        # operator just disabled disappears from the loop immediately.
        allowed = {t.id for t in self._ctx.manifest.tools
                   if self._ctx._effective_tool_permission(t.id) in (Permission.allowed, Permission.read_only)}
        want = set(tools) if tools is not None else allowed
        usable = sorted(allowed & want)
        def _tool_def(tid):
            spec = self._ctx._tools.get(tid)
            decl = next((d for d in self._ctx.manifest.tools if d.id == tid), None)
            desc = (decl.description if decl else None) or (spec.description if spec else None) or tid
            # Schema precedence: manifest decl > @agent.tool(input_schema=...) >
            # registry spec > empty object. First match with real properties lets
            # the model use correct argument names instead of guessing.
            agent_schema = self._ctx._agent.tool_schema(tid) if self._ctx._agent else None
            schema = ((decl.input_schema if decl else None)
                      or agent_schema
                      or (spec.input_schema if spec else None)
                      or {"type": "object"})
            return {"name": tid, "description": desc, "input_schema": schema}
        tool_defs = [_tool_def(tid) for tid in usable]

        messages: List[dict] = [{"role": "user", "content": input}]
        ran: List[dict] = []
        for step in range(max_steps):
            def turn(_msgs=list(messages)):
                return provider_chat(messages=_msgs, tools=tool_defs or None, system=system,
                                     model_default=model, provider=provider,
                                     temperature=temperature, max_tokens=max_tokens)

            res = self._ctx._step("llm.chat", f"step {step}", turn, {"system": system})
            calls = res.get("toolCalls") or []
            if not calls:
                return {"text": res.get("text", ""), "steps": step + 1, "toolCalls": ran}
            messages.append({"role": "assistant", "content": res.get("text", ""), "toolCalls": calls})
            for c in calls:
                name = c.get("name")
                # Governed execution: permission + scoped creds + Action Guard all apply.
                result = await self._ctx.tools.call(name, c.get("input") or {})
                ran.append({"tool": name, "input": c.get("input"), "result": result})
                messages.append({"role": "tool", "name": name, "toolUseId": c.get("id"), "content": result})
        return {"text": "[max_steps reached]", "steps": max_steps, "toolCalls": ran}


class _Models:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def call(self, model_id: str, input: dict) -> dict:
        spec = self._ctx._models.get(model_id)
        if spec is None:
            raise RyaError(
                "E_MODEL_NOT_FOUND",
                f"Model '{model_id}' is not registered.",
                hint="Register it with `rya models register` or declare it under `models:` in the manifest.",
            )
        perm = self._ctx.manifest.model_permission(model_id)
        if perm == Permission.disabled:
            raise RyaError(
                "E_TOOL_PERMISSION_DENIED",
                f"Model '{model_id}' is disabled by the manifest.",
                hint="Change its permission under `models:` in rya.agent.yaml.",
            )

        def run():
            out = spec.fn(input)
            return {"output": out, "latencyMs": spec.mock_latency_ms, "version": spec.version}

        res = self._ctx._step("model.call", model_id, run, {"input": input})
        return res["output"]


class _Memory:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def _scope(self, scope: Optional[str]) -> str:
        # "user" scope binds memory to the verified caller identity.
        if scope == "user" and self._ctx.identity is not None:
            return f"user:{self._ctx.identity.sub}"
        return scope or "agent"

    async def get(self, key: str, scope: Optional[str] = None) -> Any:
        s = self._scope(scope)

        def run():
            return self._ctx.store.load_memory(s)["kv"].get(key)

        return self._ctx._step("memory.get", f"{s}:{key}", run)

    async def set(self, key: str, value: Any, scope: Optional[str] = None) -> Any:
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            mem["kv"][key] = value
            self._ctx.store.save_memory(s, mem)
            return value

        return self._ctx._step("memory.set", f"{s}:{key}", run)

    @staticmethod
    def _text_of(item: dict) -> str:
        import json as _json
        if isinstance(item, dict):
            return item.get("text") or item.get("content") or _json.dumps(item, default=str)
        return str(item)

    async def append(self, collection: str, item: dict, scope: Optional[str] = None) -> dict:
        from ..providers.embeddings import embed
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            col = mem["collections"].setdefault(collection, [])
            # Embed the item's text so it can be semantically retrieved later.
            vec = embed(self._text_of(item), self._ctx._env)
            record = {"_ts": now_iso(), "_embedding": vec, **item}
            col.append(record)
            self._ctx.store.save_memory(s, mem)
            return {k: v for k, v in record.items() if k != "_embedding"}

        return self._ctx._step("memory.append", f"{s}:{collection}", run)

    async def search(self, collection: str, query: str, scope: Optional[str] = None,
                     limit: int = 10) -> List[dict]:
        from ..providers.embeddings import cosine, embed
        s = self._scope(scope)

        def run():
            import json as _json

            mem = self._ctx.store.load_memory(s)
            col = mem["collections"].get(collection, [])
            qvec = embed(query, self._ctx._env)
            q = query.lower()
            scored = []
            for it in col:
                emb = it.get("_embedding")
                score = cosine(qvec, emb) if emb else 0.0
                if score == 0.0 and q in _json.dumps(it, default=str).lower():
                    score = 0.1  # lexical fallback when vectors are absent/mismatched
                if score > 0:
                    clean = {k: v for k, v in it.items() if k != "_embedding"}
                    scored.append({**clean, "_score": round(score, 4)})
            scored.sort(key=lambda r: r["_score"], reverse=True)
            return scored[:limit]

        return self._ctx._step("memory.search", f"{s}:{collection}", run, {"query": query})

    # ---- core memory blocks (Letta-style: always-in-context, self-editable) ----
    async def block_set(self, name: str, value: str, scope: Optional[str] = None,
                        limit: int = 2000) -> dict:
        """Set a named core-memory block — a small, always-in-context, agent-editable
        slot (persona, the user's profile, current task state). Truncated to `limit`
        chars so it stays cheap to keep in every prompt."""
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            blocks = mem.setdefault("blocks", {})
            trunc = len(value) > limit
            blocks[name] = {"value": value[:limit] if trunc else value, "limit": limit,
                            "updatedAt": now_iso(), "truncated": trunc}
            self._ctx.store.save_memory(s, mem)
            return blocks[name]

        return self._ctx._step("memory.block_set", f"{s}:{name}", run)

    async def block_append(self, name: str, text: str, scope: Optional[str] = None,
                          limit: int = 2000) -> dict:
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            blocks = mem.setdefault("blocks", {})
            cur = blocks.get(name, {}).get("value", "")
            nv = (cur + ("\n" if cur else "") + text)
            trunc = len(nv) > limit
            blocks[name] = {"value": nv[-limit:] if trunc else nv, "limit": limit,
                            "updatedAt": now_iso(), "truncated": trunc}
            self._ctx.store.save_memory(s, mem)
            return blocks[name]

        return self._ctx._step("memory.block_append", f"{s}:{name}", run)

    async def block_get(self, name: str, scope: Optional[str] = None) -> Optional[dict]:
        s = self._scope(scope)

        def run():
            return self._ctx.store.load_memory(s).get("blocks", {}).get(name)

        return self._ctx._step("memory.block_get", f"{s}:{name}", run)

    async def blocks(self, scope: Optional[str] = None) -> List[dict]:
        s = self._scope(scope)

        def run():
            return [{"name": n, **b} for n, b in self._ctx.store.load_memory(s).get("blocks", {}).items()]

        return self._ctx._step("memory.blocks", s, run)

    # ---- long-term facts (Mem0-style: extract → consolidate/dedup → recall) ----
    async def remember(self, text: str, scope: Optional[str] = None,
                       dedupe_threshold: float = 0.92) -> List[dict]:
        """Extract atomic facts from `text`, embed each, and CONSOLIDATE against
        existing memory: a near-duplicate (cosine ≥ threshold) updates in place
        instead of piling up. This is what keeps long-term memory token-efficient."""
        from ..providers.embeddings import cosine, embed
        import re
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            facts = mem.setdefault("collections", {}).setdefault("facts", [])
            cands = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", text) if len(c.strip()) >= 3] \
                or [text.strip()]
            out = []
            for c in cands:
                vec = embed(c, self._ctx._env)
                dup = next((f for f in facts
                            if f.get("_embedding") and cosine(vec, f["_embedding"]) >= dedupe_threshold), None)
                if dup is not None:
                    dup["text"] = c
                    dup["_ts"] = now_iso()
                    dup["_embedding"] = vec
                    out.append({"action": "consolidated", "text": c, "id": dup.get("_id")})
                else:
                    rec = {"_id": _new_id("fact"), "text": c, "_embedding": vec, "_ts": now_iso()}
                    facts.append(rec)
                    out.append({"action": "added", "text": c, "id": rec["_id"]})
            self._ctx.store.save_memory(s, mem)
            return out

        return self._ctx._step("memory.remember", s, run, {"text": text})

    async def recall(self, query: str, scope: Optional[str] = None, limit: int = 5,
                    min_score: float = 0.0) -> List[dict]:
        """Semantic recall over consolidated long-term facts (vector, with a lexical
        fallback). Returns clean {text, _score, _id} — no embeddings."""
        from ..providers.embeddings import cosine, embed
        s = self._scope(scope)

        def run():
            facts = self._ctx.store.load_memory(s).get("collections", {}).get("facts", [])
            qv = embed(query, self._ctx._env)
            ql = (query or "").lower()
            scored = []
            for f in facts:
                sc = cosine(qv, f.get("_embedding")) if f.get("_embedding") else 0.0
                if sc == 0.0 and ql and ql in f.get("text", "").lower():
                    sc = 0.1
                if sc > 0 and sc >= min_score:
                    scored.append({"text": f["text"], "_score": round(sc, 4), "_id": f.get("_id")})
            scored.sort(key=lambda r: r["_score"], reverse=True)
            return scored[:limit]

        return self._ctx._step("memory.recall", s, run, {"query": query})

    async def assemble(self, query: str, scope: Optional[str] = None,
                      token_budget: int = 1000) -> dict:
        """Assemble a budget-bounded working context (Letta-style paging): the core
        blocks are ALWAYS included; recalled facts are paged in by relevance until
        the approximate token budget (≈4 chars/token) is spent."""
        from ..providers.embeddings import cosine, embed
        s = self._scope(scope)

        def run():
            mem = self._ctx.store.load_memory(s)
            blocks = mem.get("blocks", {})
            facts = mem.get("collections", {}).get("facts", [])
            qv = embed(query, self._ctx._env)
            ranked = sorted(
                ({"text": f["text"], "_score": cosine(qv, f.get("_embedding")) if f.get("_embedding") else 0.0,
                  "_id": f.get("_id")} for f in facts),
                key=lambda r: r["_score"], reverse=True)
            approx = lambda t: max(1, len(t) // 4)
            used = sum(approx(b.get("value", "")) for b in blocks.values())
            chosen = []
            for r in ranked:
                if r["_score"] <= 0:
                    continue
                cost = approx(r["text"])
                if used + cost > token_budget:
                    break
                used += cost
                chosen.append({"text": r["text"], "_score": round(r["_score"], 4), "_id": r["_id"]})
            return {"blocks": [{"name": n, **b} for n, b in blocks.items()],
                    "facts": chosen, "approxTokens": used, "tokenBudget": token_budget}

        return self._ctx._step("memory.assemble", s, run, {"query": query})


def _chunk_text(text: str, size: int = 800, overlap: int = 100) -> List[str]:
    """Split text into overlapping chunks, preferring paragraph/sentence breaks."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):  # back up to a natural boundary if one is close
            window = text[i:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size * 0.5:
                end = i + cut + 1
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return [c for c in chunks if c]


class _Knowledge:
    """RAG: ingest documents → chunk → embed → retrieve. The knowledge base an
    agent answers over. Built on the same embeddings seam as memory (real OpenAI
    embeddings when configured, deterministic hashing vectorizer otherwise) and
    stored on the substrate, so it's durable and per-workspace under RLS."""

    SCOPE = "knowledge"

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def add(self, text: str, source: Optional[str] = None, metadata: Optional[dict] = None,
                  chunk_size: int = 800, overlap: int = 100) -> dict:
        """Ingest a document: chunk it, embed each chunk, store it for retrieval."""
        from ..providers.embeddings import embed

        def run():
            mem = self._ctx.store.load_memory(self.SCOPE)
            docs = mem.setdefault("documents", [])
            chunks = mem.setdefault("collections", {}).setdefault("chunks", [])
            doc_id = _new_id("doc")
            pieces = _chunk_text(text, chunk_size, overlap)
            for i, c in enumerate(pieces):
                chunks.append({"_id": _new_id("chk"), "docId": doc_id, "i": i, "text": c,
                               "source": source, "_embedding": embed(c, self._ctx._env),
                               "_ts": now_iso(), **(metadata or {})})
            doc = {"id": doc_id, "source": source, "chunks": len(pieces), "chars": len(text or ""),
                   "createdAt": now_iso(), **(metadata or {})}
            docs.append(doc)
            self._ctx.store.save_memory(self.SCOPE, mem)
            return {"documentId": doc_id, "chunks": len(pieces)}

        return self._ctx._step("knowledge.add", source or "document", run)

    async def search(self, query: str, limit: int = 5, min_score: float = 0.0) -> List[dict]:
        """Semantic retrieval over ingested chunks (vector + lexical fallback).
        Returns ``{text, source, docId, _score}`` — the context to feed the model."""
        from ..providers.embeddings import cosine, embed

        def run():
            import re
            chunks = self._ctx.store.load_memory(self.SCOPE).get("collections", {}).get("chunks", [])
            qv = embed(query, self._ctx._env)
            qtokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
            scored = []
            for c in chunks:
                vec = cosine(qv, c.get("_embedding")) if c.get("_embedding") else 0.0
                # Blend vector similarity with lexical token overlap — robust recall
                # regardless of the embedding backend.
                ctokens = set(re.findall(r"[a-z0-9]+", c.get("text", "").lower()))
                lex = (len(qtokens & ctokens) / len(qtokens)) if qtokens else 0.0
                score = vec + 0.5 * lex
                if score > 0 and score >= min_score:
                    scored.append({"text": c["text"], "source": c.get("source"),
                                   "docId": c.get("docId"), "_score": round(score, 4)})
            scored.sort(key=lambda r: r["_score"], reverse=True)
            return scored[:limit]

        return self._ctx._step("knowledge.search", query, run, {"query": query})

    async def documents(self) -> List[dict]:
        def run():
            return self._ctx.store.load_memory(self.SCOPE).get("documents", [])

        return self._ctx._step("knowledge.documents", "all", run)


class _Tools:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def call(self, tool_id: str, input: dict) -> dict:
        perm = self._ctx._resolve_tool_permission(tool_id)
        if perm == Permission.disabled:
            raise RyaError(
                "E_TOOL_PERMISSION_DENIED",
                f"Tool '{tool_id}' is disabled.",
                hint="Enable it under `tools:` in rya.agent.yaml.",
            )
        if perm == Permission.approval_required:
            raise RyaError(
                "E_TOOL_PERMISSION_DENIED",
                f"Tool '{tool_id}' requires approval and cannot be called directly.",
                hint="Use `ctx.approvals.request(action={'tool': '%s', 'input': {...}})` instead." % tool_id,
            )
        # Resolve the manifest decl and backend once. Scoped connected credentials
        # are enforced BEFORE the implementation runs — the tool's required scopes
        # must be within (connection scopes ∩ requesting-user scopes) — but ONLY for
        # tools that actually egress with the credential (url/mock backends). A local
        # @agent.tool handler never receives the secret, so a `provider:` on it is
        # governance metadata: resolving a credential there is pointless and would
        # wrongly require a live connection for an offline leaf.
        decl = next((t for t in self._ctx.manifest.tools if t.id == tool_id), None)
        provider = getattr(decl, "provider", None) if decl is not None else None
        handler = self._ctx._agent.tool_handler(tool_id) if self._ctx._agent else None
        url = getattr(decl, "url", None)
        secret = self._ctx._authorize_connection(decl) if (handler is None and provider) else None
        meta = {"provider": provider, "scopes": getattr(decl, "scopes", []) or []} if provider else {}

        # Server-side arg pinning: pinned fields come from trusted state (event,
        # memory, identity, literals) and OVERWRITE whatever the caller - human
        # handler or model - supplied. Never trust the model for scoped ids.
        pins = getattr(decl, "pin", None) or {}
        if pins:
            input = {**(input or {}), **{k: self._ctx._resolve_pin(v) for k, v in pins.items()}}
            meta["pinnedArgs"] = sorted(pins)

        # The id-secrecy scrub runs on the result INSIDE the journaled fn below,
        # AFTER the implementation produces it: so the scrubbed value is what the
        # loop sees, what the journal memoizes on replay, and what the trace
        # records (a secret id never lands in observability). Running it after the
        # body — not before — lets a handler still act on the raw id it received
        # before the redacted form propagates.
        scrub = self._ctx.guard.scrub

        # Resolve the backend (agent handler / HTTP / mock) into one async callable
        # over a (possibly repaired) input, so the retry+repair loop is backend-
        # agnostic. All three flow through a single journaled step, so a replay
        # after an approval pause returns the memoized final result — retries and
        # repairs never re-run.
        if handler is not None:
            impl = "agent"

            async def backend(cur):
                return await handler(cur)
        elif url:
            impl = "http"

            async def backend(cur):
                return _http_tool(url, cur, auth_secret=secret)
        else:
            spec = self._ctx._tools.get(tool_id)
            if spec is None:
                raise RyaError(
                    "E_TOOL_NOT_FOUND",
                    f"Tool '{tool_id}' is declared but has no implementation.",
                    hint="Define it with @agent.tool, add a `url:` in the manifest, or register a mock.",
                )
            impl = "mock"

            async def backend(cur):
                return spec.fn(cur)

        retry = getattr(decl, "retry", None)
        repair = self._ctx._agent.repair_handler(tool_id) if self._ctx._agent else None

        async def run_tool():
            result = scrub(await self._invoke_with_recovery(tool_id, backend, input, retry, repair))
            self._apply_adoption(decl, result)
            return result

        return await self._ctx._astep("tool.call", tool_id, run_tool,
                                      {"input": input, "permission": perm.value, "impl": impl, **meta})

    @staticmethod
    def _error_class(exc) -> Optional[str]:
        """Classify a raised error into a retry class token, or None if it is not
        a transiently-retryable failure."""
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "timeout"
        if isinstance(exc, RyaError):
            if exc.code == "E_TIMEOUT":
                return "timeout"
            status = getattr(exc, "http_status", None)
            if isinstance(status, int) and 500 <= status < 600:
                return "5xx"
        return None

    @staticmethod
    async def _backoff_sleep(backoff: str, attempt: int) -> None:
        # Kept small for the local slice — real, but sub-second so tests stay fast.
        if backoff == "none":
            return
        base = 0.02
        delay = base if backoff == "fixed" else base * (2 ** (attempt - 1))
        await asyncio.sleep(min(delay, 0.2))

    async def _invoke_with_recovery(self, tool_id, backend, input, retry, repair):
        """Run ``backend(input)`` with A1 semantics: self-heal a recoverable error
        once via the registered repair callback, and retry a transient failure per
        the declared ``retry`` policy. Repair (domain) and retry (transient) are
        orthogonal — a repair does not consume a transient-retry budget."""
        from ..errors import RyaRecoverableToolError

        max_attempts = retry.max_attempts if retry else 1
        on = set(retry.on) if retry else set()
        backoff = retry.backoff if retry else "none"

        cur = input
        transient_used = 0
        repaired = False
        while True:
            try:
                return await backend(cur)
            except RyaRecoverableToolError as e:
                # Self-heal exactly once: hand the input + error to the repair
                # callback, retry with its patched input. A tool.repair step in the
                # trace makes the self-heal visible.
                if repair is None or repaired:
                    raise
                repaired = True
                patched = repair(cur, e)
                if inspect.isawaitable(patched):
                    patched = await patched
                self._ctx._trace("tool.repair", tool_id,
                                 {"reason": e.reason, "detail": e.detail,
                                  "patched": patched if patched is not None else cur})
                cur = patched if patched is not None else cur
            except Exception as e:  # noqa: BLE001 - re-raised unless retryable
                cls = self._error_class(e)
                if cls in on and transient_used < (max_attempts - 1):
                    transient_used += 1
                    self._ctx._trace("tool.retry", tool_id,
                                     {"attempt": transient_used, "class": cls,
                                      "maxAttempts": max_attempts})
                    await self._backoff_sleep(backoff, transient_used)
                    continue
                raise

    def _apply_adoption(self, decl, result) -> None:
        """A5 adoption: after a successful call, copy declared result fields into
        scoped memory so a later pinned tool in the same turn adopts them. Written
        to the store synchronously (inside the journaled tool step), so a pin that
        reads ``memory.<scope>.<key>`` on a subsequent call sees the adopted value;
        on replay the tool step is memoized and the store already holds it."""
        adopt = getattr(decl, "adopt", None) or {}
        if not adopt or not isinstance(result, dict):
            return
        # A failed handler result (ok is explicitly False) adopts nothing.
        if result.get("ok") is False:
            return
        for field, target in adopt.items():
            val = result.get(field)
            if val in (None, "", []):
                continue
            if "." not in target:
                continue
            scope, key = target.split(".", 1)
            mem = self._ctx.store.load_memory(scope)
            mem.setdefault("kv", {})[key] = val
            self._ctx.store.save_memory(scope, mem)
            self._ctx._trace("tool.adopt", target, {"field": field, "value": val})


class _Guard:
    """Serving-path guardrails a handler (or the runtime) can invoke."""

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def _tool_outputs(self) -> list:
        return [e.get("result") for e in self._ctx.run.get("journal", {}).values()
                if e.get("kind") == "tool.call"]

    def check_grounding(self, text: str) -> dict:
        """Every money figure in ``text`` must be traceable to a tool output of
        THIS run - the grounding gate, as a primitive.
        Returns {ok, figures, violations}."""
        from ..guard import grounding_check
        return grounding_check(text, self._tool_outputs())

    def _grounding_policy(self) -> dict:
        from ..guard import load_policy, GUARD_FILE
        policy = load_policy(str(self._ctx.project_root / GUARD_FILE)) or {}
        return policy.get("grounding") or {}

    def _policy(self) -> dict:
        from ..guard import load_policy, GUARD_FILE
        return load_policy(str(self._ctx.project_root / GUARD_FILE)) or {}

    def scrub(self, obj):
        """Id-secrecy scrub: rewrite every configured secret pattern in every
        string leaf of ``obj`` to its safe token (no-op unless ``secrecy.enabled``
        in rya.guard.yaml). Applied at the tool boundary and on outbound so a
        secret id never reaches the model, the trace, or a channel send."""
        from ..guard import _compile_secrecy, secrecy_scrub
        return secrecy_scrub(obj, _compile_secrecy(self._policy()))

    def check_secrecy(self, text: str) -> dict:
        """Report whether any configured secret pattern appears in ``text``.
        Returns {ok, hits, scrubbed} — handler-side assertion complement to the
        automatic boundary scrub."""
        from ..guard import secrecy_check
        return secrecy_check(text, self._policy())


class _Channels:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def send(self, channel: str, message: dict) -> dict:
        from ..providers.channels import send as channel_send

        # Id-secrecy scrub FIRST (opt-in via rya.guard.yaml `secrecy.enabled`):
        # rewrite any secret id in the outbound message to its safe token before
        # anything else sees it — the grounding check, the wire, and the trace all
        # operate on the scrubbed message, so a secret id can never leave.
        message = self._ctx.guard.scrub(message)

        # Grounding gate (opt-in via rya.guard.yaml `grounding.enabled`): an
        # outbound message may not contain money figures that no tool output of
        # this run produced. Fail closed - a blocked send raises, and the trace
        # records why.
        gp = self._ctx.guard._grounding_policy()
        if gp.get("enabled"):
            text = " ".join(str(v) for v in (message or {}).values() if isinstance(v, (str, int, float)))
            check = self._ctx.guard.check_grounding(text)
            if not check["ok"]:
                self._ctx._trace("guard.grounding_blocked", channel,
                                 {"violations": check["violations"]})
                raise RyaError(
                    "E_GROUNDING_BLOCKED",
                    f"Outbound message contains ungrounded figures {check['violations']} "
                    "not present in any tool output of this run.",
                    hint="Only quote amounts returned by tools, or disable grounding in rya.guard.yaml.")

        def run():
            # Real Slack/email/webhook when configured via env, else mock.
            return channel_send(channel, message, self._ctx._env)

        return self._ctx._step("channel.send", channel, run, {"message": message})


class _Sessions:
    """Durable conversation state — sessions + messages on the same substrate.

    A *session* is one ongoing conversation (a Slack thread, an email thread, a
    web chat). Inbound events map to a session by ``(channel, external_id)`` so a
    reply lands in the right thread across restarts. Messages are appended in
    order and ``history()`` returns the recent window the handler feeds the model.
    This is conversation *storage and retrieval* — not a chat UI.
    """

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def _owner(self) -> Optional[str]:
        return self._ctx.identity.sub if self._ctx.identity is not None else None

    async def get_or_create(self, channel: str, external_id: str,
                            title: Optional[str] = None) -> dict:
        agent = self._ctx.manifest.name

        def run():
            existing = self._ctx.store.find_session(agent, channel, external_id)
            if existing is not None:
                return existing
            return self._ctx.store.create_session(
                agent=agent, channel=channel, external_id=external_id,
                owner=self._owner(), title=title,
            )

        return self._ctx._step("session.get_or_create", f"{channel}:{external_id}", run)

    async def append(self, session_id: str, role: str, content: str, **extra) -> dict:
        # Tie each message back to the run that produced it, for forensics.
        extra.setdefault("runId", self._ctx.run.get("id"))

        def run():
            return self._ctx.store.append_message(session_id, role, content, **extra)

        return self._ctx._step("session.append", f"{session_id}:{role}", run)

    async def history(self, session_id: str, limit: int = 20) -> List[dict]:
        def run():
            msgs = self._ctx.store.list_messages(session_id)
            return msgs[-limit:]

        return self._ctx._step("session.history", session_id, run, {"limit": limit})

    async def get(self, session_id: str) -> Optional[dict]:
        def run():
            return self._ctx.store.get_session(session_id)

        return self._ctx._step("session.get", session_id, run)

    async def search(self, session_id: str, query: str, limit: int = 10) -> List[dict]:
        """Lexical retrieval over a session's messages (the embeddings seam can
        layer on top exactly as ctx.memory.search does)."""
        q = (query or "").lower()

        def run():
            hits = [m for m in self._ctx.store.list_messages(session_id)
                    if q in (m.get("content") or "").lower()]
            return hits[:limit]

        return self._ctx._step("session.search", session_id, run, {"query": query})


class _Files:
    """Uploaded files (``POST /files`` / ``rya files upload``). Files are
    immutable once stored, so replays can re-read bytes and get identical
    content - only metadata is ever journaled, bytes never enter the journal."""

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def get(self, file_id: str) -> Optional[dict]:
        def run():
            return self._ctx.store.get_file(file_id)

        return self._ctx._step("file.get", file_id, run)

    async def list(self, tags: Optional[dict] = None) -> List[dict]:
        def run():
            return self._ctx.store.list_files(tags)

        label = ",".join(f"{k}={v}" for k, v in sorted((tags or {}).items())) or "all"
        return self._ctx._step("file.list", label, run, {"tags": tags})

    async def read(self, file_id: str) -> bytes:
        def run():
            meta = self._ctx.store.get_file(file_id)
            if meta is None:
                raise RyaError("E_NOT_FOUND", f"file '{file_id}' not found.")
            return meta  # journal carries metadata (name/sha/size), never bytes

        self._ctx._step("file.read", file_id, run)
        data = self._ctx.store.read_file(file_id)
        if data is None:
            raise RyaError("E_NOT_FOUND", f"file '{file_id}' has no stored content.")
        return data

    async def as_document(self, file_id: str) -> dict:
        """Shape a stored file for ``ctx.llm.respond(documents=[...])``."""
        meta = await self.get(file_id)
        if meta is None:
            raise RyaError("E_NOT_FOUND", f"file '{file_id}' not found.")
        name = meta.get("name") or file_id
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        fmt = ext if ext in ("pdf", "csv", "txt", "md", "html", "doc", "docx", "xls", "xlsx") else "pdf"
        return {"name": name, "format": fmt, "bytes": await self.read(file_id)}


class _Connections:
    """Scoped, vaulted connected credentials — the agent's authority to act on a
    provider on a user's behalf. The handler sees only metadata (provider, scopes,
    status); the secret stays in the runtime and is injected into tool calls, never
    exposed to the model. Enforcement (the intersection rule) happens in
    ``ctx.tools.call`` via ``RuntimeContext._authorize_connection``."""

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def get(self, provider: str) -> Optional[dict]:
        """Public metadata for the connection that would be used for `provider`
        by the current caller — WITHOUT the secret."""
        owner = self._ctx.identity.sub if self._ctx.identity is not None else None

        def run():
            conn = self._ctx.store.get_connection(provider, owner) if hasattr(self._ctx.store, "get_connection") else None
            if conn is None:
                return None
            return {k: v for k, v in conn.items() if k != "secret"} | {"secretSet": bool(conn.get("secret"))}

        return self._ctx._step("connection.get", provider, run)

    async def list(self) -> List[dict]:
        def run():
            return self._ctx.store.list_connections() if hasattr(self._ctx.store, "list_connections") else []

        return self._ctx._step("connection.list", "all", run)

    async def upsert(self, provider: str, *, secret: str, scopes: Optional[List[str]] = None,
                     label: Optional[str] = None) -> dict:
        """Mint or refresh the current caller's connection for ``provider`` — the
        write path a login handler uses after exchanging credentials for a bearer.
        Owner is bound to ``identity.sub`` (per-user); overwrite-in-place keyed on
        (provider, owner) so a re-login never leaves a stale duplicate the runtime
        could later inject. The secret is sealed at rest and seeded into the
        redaction vault; only public metadata is returned (never the secret)."""
        owner = self._ctx.identity.sub if self._ctx.identity is not None else None
        self._ctx._seed_secret(secret)

        def run():
            if not hasattr(self._ctx.store, "upsert_connection"):
                raise RyaError("E_RUNTIME", "store does not support connection upsert.")
            return self._ctx.store.upsert_connection(provider, list(scopes or []),
                                                     secret=secret, owner=owner, label=label)

        return self._ctx._step("connection.upsert", provider, run,
                               {"scopes": list(scopes or []), "owner": owner})


class _Jobs:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def schedule(self, handler: str, payload: dict, delay_seconds: int = 0,
                       max_attempts: int = 3) -> dict:
        def run():
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            job = self._ctx.store.create_job(self._ctx.run["id"], handler, payload, run_at, max_attempts)
            self._ctx.run.setdefault("scheduledJobs", []).append(job["id"])
            return {"jobId": job["id"], "handler": handler, "runAt": run_at}

        return self._ctx._step("job.schedule", handler, run, {"delaySeconds": delay_seconds})

    async def schedule_group(self, jobs: list, on_complete: tuple,
                             max_attempts: int = 3) -> dict:
        """Fan-out with race-free fan-in: schedule every ``(handler, payload)``
        in ``jobs`` and, when ALL succeed, fire ``on_complete`` exactly once -
        under any number of parallel workers. A terminal member failure marks
        the group failed and on_complete never fires."""
        def run():
            oc_handler, oc_payload = on_complete
            group = self._ctx.store.create_job_group(
                {"handler": oc_handler, "payload": oc_payload,
                 "parentRunId": self._ctx.run["id"]}, len(jobs))
            run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ids = []
            for handler, payload in jobs:
                j = self._ctx.store.create_job(self._ctx.run["id"], handler, payload,
                                               run_at, max_attempts, group_id=group["id"])
                ids.append(j["id"])
            self._ctx.run.setdefault("scheduledJobs", []).extend(ids)
            return {"groupId": group["id"], "jobIds": ids, "onComplete": oc_handler}

        return self._ctx._step("job.schedule_group",
                               f"{len(jobs)} jobs -> {on_complete[0]}", run)


class _Cron:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def schedules(self) -> List[dict]:
        return [t.model_dump() for t in self._ctx.manifest.triggers if t.type == "cron"]


class _Approvals:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def request(self, *, title: str, body: str, action: dict) -> dict:
        """Request human approval. Suspends the run until resolved.

        On approval the embedded ``action`` (a tool call) is executed by the
        engine and its result is returned here when the run resumes.
        """
        return self._ctx._request_approval(title, body, action)


class _Logs:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def _log(self, level: str, message: str, **fields):
        # Redact secrets before anything reaches stderr or the trace.
        message = self._ctx._redact(message)
        fields = self._ctx._redact(fields)

        def run():
            return emit_log(level, message, run_id=self._ctx.run["id"], **fields)

        return self._ctx._step("log", message, run, {"level": level, **fields})

    def debug(self, message: str, **f):
        return self._log("debug", message, **f)

    def info(self, message: str, **f):
        return self._log("info", message, **f)

    def warning(self, message: str, **f):
        return self._log("warning", message, **f)

    def error(self, message: str, **f):
        return self._log("error", message, **f)


class _Traces:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def event(self, name: str, data: Optional[dict] = None):
        def run():
            return {"name": name, "data": data or {}}

        return self._ctx._step("trace.event", name, run, data or {})


class _Secrets:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    def get(self, name: str) -> Optional[str]:
        # NOT journaled or traced — secret values must never be persisted. The
        # value is also added to the redaction vault so if a handler accidentally
        # logs or returns it, it gets scrubbed from traces/logs.
        value = self._ctx._env.get(name)
        self._ctx._seed_secret(value)
        return value


class _Events:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def emit(self, type: str, payload: dict) -> dict:
        def run():
            return {"emitted": True, "type": type, "payload": payload}

        return self._ctx._step("event.emit", type, run, {"payload": payload})
