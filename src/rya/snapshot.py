"""One-call machine-readable project state for coding agents.

The InsForge lesson: don't make the agent *discover* the backend through trial
and error (list tools, trigger, fail, retry — burning tokens). Hand it the whole
live state up front in one compact, structured payload — manifest, tools +
permissions, models, channels, handlers, recent runs, pending approvals/jobs,
active store/LLM backend — plus the invariants it must respect and the obvious
next actions. This is what ``rya context`` / the ``rya_context`` MCP tool return.

Kept dependency-light (registries + providers only) so both the CLI and the MCP
ops layer can import it without cycles.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .models.registry import default_registry as _models
from .providers import resolve_provider
from .tools.registry import default_registry as _tools

# Invariants an agent must respect — surfaced up front so it doesn't learn them
# by hitting an error.
RULES = [
    "An approval_required tool CANNOT be called via ctx.tools.call — gate it with "
    "ctx.approvals.request(action={'tool': id, 'input': {...}}); it runs only after approval.",
    "A run that calls ctx.approvals.request PAUSES. Resume it with "
    "`rya approvals approve <id>` (or rya_approve_action); prior steps are memoized.",
    "Issue ctx operations in a deterministic order (durable-replay requirement).",
    "Read secrets via ctx.secrets.get(NAME); never hard-code them. They are never traced.",
]


def _multitenant() -> bool:
    has_pg = bool(os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    return os.environ.get("RYA_MULTITENANT") == "1" and has_pg


def build_snapshot(manifest, store, agent=None, recent_limit: int = 5, project_root=None) -> dict:
    treg, mreg = _tools(), _models()
    runs = store.list_runs(manifest.name)
    counts: dict = {}
    for r in runs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    pending_approvals = store.list_approvals("pending")
    pending_jobs = store.list_jobs("pending")

    snapshot = {
        "agent": {
            "name": manifest.name,
            "version": manifest.version,
            "runtime": manifest.runtime,
            "environment": manifest.environment,
            "entrypoint": manifest.entrypoint,
            "instructions": manifest.instructions,
        },
        "handlers": {
            "event": (agent.event_handler() is not None) if agent else None,
            "jobs": list(agent._job_handlers.keys()) if agent else [],
            "cron": list(agent._cron_handlers.keys()) if agent else [],
        },
        "tools": [
            {"id": t.id, "permission": t.permission.value,
             "registered": treg.get(t.id) is not None,
             "externalSideEffects": getattr(treg.get(t.id), "external_side_effects", None)}
            for t in manifest.tools
        ],
        "models": [
            {"id": m.id, "type": m.type, "permission": m.permission.value,
             "version": getattr(mreg.get(m.id), "version", None)}
            for m in manifest.models
        ],
        "channels": [{"type": c.type, "enabled": c.enabled, "path": c.path} for c in manifest.channels],
        "triggers": [t.model_dump() for t in manifest.triggers],
        "memory": {
            "collections": manifest.memory.collections,
            "scopes": store.list_memory_scopes(),
        },
        "runtime": {
            "store": store.describe(),
            "llmProvider": resolve_provider(manifest.model.provider),
            "multiTenant": _multitenant(),
        },
        "runs": {
            "total": len(runs),
            "byStatus": counts,
            "recent": [
                {"id": r["id"], "status": r["status"], "trigger": r["trigger"],
                 "createdAt": r["createdAt"], "pendingApproval": r.get("pendingApproval")}
                for r in runs[:recent_limit]
            ],
        },
        "approvals": {
            "pendingCount": len(pending_approvals),
            "pending": [{"id": a["id"], "title": a["title"], "runId": a["runId"]} for a in pending_approvals],
        },
        "jobs": {"pendingCount": len(pending_jobs)},
        "rules": RULES,
        "next": _suggest_next(manifest, agent, runs, pending_approvals, pending_jobs),
    }
    # The orient call also tells the agent whether it's production-ready, so it
    # knows what to fix before deploy without a separate discovery step.
    if project_root is not None and agent is not None:
        from .readiness import check_readiness
        rep = check_readiness(manifest, store, agent, project_root)
        snapshot["readiness"] = {"ready": rep["ready"], "summary": rep["summary"],
                                 "blocks": rep["blocks"]}
    return snapshot




_VIOLATION_CODES = {"E_EGRESS_BLOCKED", "E_GROUNDING_BLOCKED", "E_TOOL_PERMISSION_DENIED",
                    "E_APPROVER_IDENTITY_REQUIRED", "E_BUDGET_EXCEEDED"}


def _governance(manifest, store, runs, project_root) -> dict:
    """The control-plane governance surface: what is enforced, under which
    policy version, what has been overridden, and what got blocked. Everything
    here reflects REAL enforcement state - nothing is aspirational."""
    import hashlib
    import json as _json

    env = os.environ
    guard_path = Path(project_root) / "rya.guard.yaml"
    guard_txt = guard_path.read_text() if guard_path.exists() else ""
    try:
        import yaml as _yaml
        gy = _yaml.safe_load(guard_txt) or {}
    except Exception:
        gy = {}
    grounding_on = bool((gy.get("grounding") or {}).get("enabled"))
    from .auth import jwt_configured
    from .seal import available as seal_available

    pinned = sum(1 for t in manifest.tools if getattr(t, "pin", None))
    gated = sum(1 for t in manifest.tools if t.permission.value == "approval_required")
    denied = sum(1 for t in manifest.tools if t.permission.value == "disabled")

    # The policy document is everything that constrains the agent. Its hash is
    # the version an auditor can pin a run to.
    policy_material = _json.dumps({
        "tools": [{"id": t.id, "permission": t.permission.value,
                   "pin": sorted(getattr(t, "pin", {}) or {})} for t in manifest.tools],
        "guard": guard_txt,
        "approverIdentityRequired": env.get("RYA_REQUIRE_APPROVER_IDENTITY") == "1",
        "multiTenant": _multitenant(),
    }, sort_keys=True)
    policy_hash = hashlib.sha256(policy_material.encode()).hexdigest()[:16]

    rc = store.load_memory("_runtime_config")
    overrides = [dict(v, tool=k.split(":", 1)[1]) for k, v in (rc.get("kv") or {}).items()
                 if k.startswith("tool:")]
    history = ((rc.get("collections") or {}).get("history") or [])[-10:]

    violations = []
    for r in runs:
        for ev in r.get("trace", []):
            kind = ev.get("kind", "")
            data = ev.get("data") or {}
            if kind.startswith("guard."):
                violations.append({"ts": ev.get("ts"), "runId": r["id"], "kind": kind,
                                   "detail": str(data.get("violations") or ev.get("label") or "")[:160]})
            elif kind == "run.failed" and str(ev.get("label")) in _VIOLATION_CODES:
                violations.append({"ts": ev.get("ts"), "runId": r["id"], "kind": ev["label"],
                                   "detail": str(data.get("message") or "")[:160]})
    violations = sorted(violations, key=lambda v: v.get("ts") or "", reverse=True)[:25]

    return {
        "policy": {"hash": policy_hash, "toolsGated": gated, "toolsDenied": denied,
                   "pinnedArgTools": pinned, "egressRules": len(gy.get("rules") or []),
                   "egressDefault": gy.get("default", "deny") if guard_txt else None},
        "enforcement": {
            "egressGuard": bool(guard_txt),
            "groundingGate": grounding_on,
            "approverIdentity": env.get("RYA_REQUIRE_APPROVER_IDENTITY") == "1",
            "perUserIdentity": jwt_configured(),
            "multiTenantRls": _multitenant(),
            "secretsSealed": seal_available(),
        },
        "switches": {"active": overrides, "history": list(reversed(history))},
        "violations": violations,
    }

def build_console(manifest, store, agent, project_root) -> dict:
    """Rich aggregate state for the web console — everything one dashboard needs
    in a single call, computed from the live runtime (not mocked)."""
    from pathlib import Path

    from .observability.usage import run_usage

    treg, mreg = _tools(), _models()
    runs = store.list_runs(manifest.name)

    # Status counts + token/cost totals computed from real run traces.
    counts: dict = {}
    in_tok = out_tok = 0
    cost = 0.0
    tool_calls: dict = {}
    model_calls: dict = {}
    models_seen: set = set()
    for r in runs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        u = run_usage(r)
        in_tok += u["inputTokens"]; out_tok += u["outputTokens"]
        if u["costUsd"]:
            cost += u["costUsd"]
        for ev in r.get("trace", []):
            if ev.get("kind") == "llm.respond":
                m = ((ev.get("data") or {}).get("result") or {}).get("model")
                if m:
                    models_seen.add(m)
            if ev.get("kind") == "tool.call":
                tool_calls[ev.get("label")] = tool_calls.get(ev.get("label"), 0) + 1
            elif ev.get("kind") == "model.call":
                model_calls[ev.get("label")] = model_calls.get(ev.get("label"), 0) + 1

    pending = store.list_approvals("pending")
    mem = store.load_memory("agent")
    kmem = store.load_memory("knowledge")
    sessions = store.list_sessions(manifest.name) if hasattr(store, "list_sessions") else []
    connections = store.list_connections() if hasattr(store, "list_connections") else []

    secrets = []
    env_path = Path(project_root) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                secrets.append(line.split("=", 1)[0].strip())

    manifest_yaml = ""
    mpath = Path(project_root) / "rya.agent.yaml"
    if mpath.exists():
        manifest_yaml = mpath.read_text()

    return {
        "ok": True,
        "governance": _governance(manifest, store, runs, project_root),
        "agent": {"name": manifest.name, "version": manifest.version,
                  "runtime": manifest.runtime, "environment": manifest.environment,
                  "status": "running",
                  "handlers": {"event": agent.event_handler() is not None,
                               "jobs": list(agent._job_handlers.keys())}},
        "runtime": {"store": store.describe().get("backend"),
                    "llmProvider": resolve_provider(manifest.model.provider),
                    "multiTenant": _multitenant()},
        "stats": {"runs": len(runs), "byStatus": counts,
                  "approvalsPending": len(pending),
                  "jobsPending": len(store.list_jobs("pending")),
                  "sessions": len(sessions),
                  "messages": sum(s.get("messageCount", 0) for s in sessions),
                  "inputTokens": in_tok, "outputTokens": out_tok,
                  "costUsd": round(cost, 4) if cost else None,
                  "models": sorted(models_seen)},
        "tools": [{"id": t.id, "permission": t.permission.value,
                   "externalSideEffects": getattr(treg.get(t.id), "external_side_effects", None),
                   "requiredSecrets": getattr(treg.get(t.id), "required_secrets", []),
                   "provider": getattr(t, "provider", None), "scopes": getattr(t, "scopes", []) or [],
                   # A tool is a mock ONLY if it resolves to a registry mock: a
                   # project @agent.tool handler or a url: decl is real code.
                   "mockImpl": bool(getattr(treg.get(t.id), "mock", False)
                                    and not getattr(t, "url", None)
                                    and not (agent is not None and agent.tool_handler(t.id))),
                   "calls": tool_calls.get(t.id, 0)} for t in manifest.tools],
        "models": [{"id": m.id, "type": m.type, "permission": m.permission.value,
                    "version": getattr(mreg.get(m.id), "version", None),
                    "calls": model_calls.get(m.id, 0)} for m in manifest.models],
        "channels": [{"type": c.type, "enabled": c.enabled, "path": c.path} for c in manifest.channels],
        "memory": {"collections": [{"name": n, "count": len(v)} for n, v in mem.get("collections", {}).items()],
                   "kvKeys": len(mem.get("kv", {})),
                   "blocks": [{"name": n, "chars": len(b.get("value", "")), "limit": b.get("limit"),
                               "updatedAt": b.get("updatedAt")} for n, b in mem.get("blocks", {}).items()],
                   "facts": len(mem.get("collections", {}).get("facts", []))},
        "knowledge": {"documents": kmem.get("documents", []),
                      "chunks": len(kmem.get("collections", {}).get("chunks", []))},
        "triggers": [t.model_dump() for t in manifest.triggers],
        "approvals": [{"id": a["id"], "title": a["title"], "body": a.get("body"),
                       "action": a.get("action"), "runId": a["runId"]} for a in pending],
        "runs": [{"id": r["id"], "trigger": r["trigger"], "status": r["status"],
                  "createdAt": r["createdAt"], "pendingApproval": r.get("pendingApproval"),
                  "tokens": run_usage(r)["inputTokens"] + run_usage(r)["outputTokens"]}
                 for r in runs[:30]],
        "connections": [{"id": c["id"], "provider": c.get("provider"), "scopes": c.get("scopes", []),
                         "owner": c.get("owner"), "status": c.get("status"),
                         "secretSet": c.get("secretSet"), "encrypted": c.get("encrypted"),
                         "label": c.get("label")} for c in connections],
        "sessions": [{"id": s["id"], "title": s.get("title"), "channel": s.get("channel"),
                      "externalId": s.get("externalId"), "status": s.get("status"),
                      "messageCount": s.get("messageCount", 0),
                      "lastMessageAt": s.get("lastMessageAt"),
                      "createdAt": s.get("createdAt")} for s in sessions[:50]],
        "secrets": secrets,
        "manifestYaml": manifest_yaml,
    }


def _suggest_next(manifest, agent, runs, pending_approvals, pending_jobs) -> list:
    nxt = []
    if agent is not None and agent.event_handler() is None:
        nxt.append("No @agent.on_event handler — add one in the entrypoint, then `rya dev`.")
    if pending_approvals:
        a = pending_approvals[0]
        nxt.append(f"Resolve pending approval: `rya approvals approve {a['id']}` (or reject).")
    if not runs:
        nxt.append("Trigger a test run: `rya events send --type message.received --payload '{\"email\":\"a@b.com\"}'`.")
    if pending_jobs:
        nxt.append("Run queued background jobs: `rya jobs run --all`.")
    if runs:
        nxt.append(f"Inspect the latest run: `rya runs trace {runs[0]['id']}`.")
    return nxt
