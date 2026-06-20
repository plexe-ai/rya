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
        raise RyaError("E_RUNTIME", f"tool HTTP {e.code}: {e.read().decode(errors='replace')[:200]}",
                       hint="Check the tool URL / payload.")
    except urllib.error.URLError as e:
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
    ) -> None:
        self.store = store
        # Optional live trace subscriber — fired on every trace event as it
        # happens (the WebSocket surface streams a run to the client in real time).
        self._on_trace = on_trace
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
        self.tools = _Tools(self)
        self.channels = _Channels(self)
        self.jobs = _Jobs(self)
        self.cron = _Cron(self)
        self.approvals = _Approvals(self)
        self.sessions = _Sessions(self)
        self.connections = _Connections(self)
        self.logs = _Logs(self)
        self.traces = _Traces(self)
        self.secrets = _Secrets(self)
        self.events = _Events(self)

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
    def _resolve_tool_permission(self, tool_id: str) -> Permission:
        perm = self.manifest.tool_permission(tool_id)
        if perm is None:
            raise RyaError(
                "E_TOOL_NOT_FOUND",
                f"Tool '{tool_id}' is not declared in the manifest.",
                hint="Add it under `tools:` in rya.agent.yaml with an explicit permission.",
            )
        return perm


# --------------------------------------------------------------------------
# Sub-interfaces. Each is a thin adapter over RuntimeContext._step.
# --------------------------------------------------------------------------
class _LLM:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def respond(self, *, system: str, input: dict) -> LLMResponse:
        from ..providers import respond as provider_respond

        mb = self._ctx.manifest.model

        def run():
            # Provider chosen by manifest model.provider (auto/mock/anthropic/openai).
            try:
                return provider_respond(
                    system=system, input=input, model_default=mb.default,
                    provider=mb.provider, temperature=mb.temperature, max_tokens=mb.max_tokens,
                )
            except RyaError:
                # Fall back to the manifest's fallback model on provider failure.
                if mb.fallback:
                    out = provider_respond(
                        system=system, input=input, model_default=mb.fallback,
                        provider=mb.provider, temperature=mb.temperature, max_tokens=mb.max_tokens,
                    )
                    out["fellBackFrom"] = mb.default
                    return out
                raise

        res = self._ctx._step("llm.respond", mb.default, run, {"system": system})
        return LLMResponse(text=res["text"], model=res["model"])


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
        # Resolve the manifest decl once, then enforce scoped connected credentials
        # BEFORE any implementation runs: the tool's required scopes must be within
        # (connection scopes ∩ requesting-user scopes). Returns the secret to inject.
        decl = next((t for t in self._ctx.manifest.tools if t.id == tool_id), None)
        secret = self._ctx._authorize_connection(decl)
        provider = getattr(decl, "provider", None) if decl is not None else None
        meta = {"provider": provider, "scopes": getattr(decl, "scopes", []) or []} if provider else {}

        # 1) Real tool defined in the agent via @agent.tool — async leaf handler.
        handler = self._ctx._agent.tool_handler(tool_id) if self._ctx._agent else None
        if handler is not None:
            async def run_handler():
                return await handler(input)
            return await self._ctx._astep("tool.call", tool_id, run_handler,
                                          {"input": input, "permission": perm.value, "impl": "agent", **meta})

        # 2) HTTP tool — manifest declares a url to POST the input to.
        url = getattr(decl, "url", None)
        if url:
            async def run_http():
                return _http_tool(url, input, auth_secret=secret)
            return await self._ctx._astep("tool.call", tool_id, run_http,
                                          {"input": input, "permission": perm.value, "impl": "http", **meta})

        # 3) Mock registry fallback.
        spec = self._ctx._tools.get(tool_id)
        if spec is None:
            raise RyaError(
                "E_TOOL_NOT_FOUND",
                f"Tool '{tool_id}' is declared but has no implementation.",
                hint="Define it with @agent.tool, add a `url:` in the manifest, or register a mock.",
            )

        def run():
            return spec.fn(input)

        return self._ctx._step("tool.call", tool_id, run, {"input": input, "permission": perm.value})


class _Channels:
    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    async def send(self, channel: str, message: dict) -> dict:
        from ..providers.channels import send as channel_send

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
