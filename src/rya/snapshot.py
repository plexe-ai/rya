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
from .config import current_environment

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


def run_summary(run: dict) -> dict:
    """The one run ROW every list surface sends.

    A run document carries its whole trace, so no list may ship documents: the
    console's runs table, the WebSocket's ``run`` message and the paged
    ``GET /agents/{id}/runs?summary=1`` all want the same compact header, and the
    trace has its own endpoint for when someone opens one.

    Defined here — and imported by ``api/app.py``, which is the direction the
    dependency already runs — because there were three near-identical copies of
    this projection, differing by a field each (this one grew ``createdAt``, the
    console's aggregate omitted ``costUsd`` and ``traceLength``). Three copies of
    a row shape is three chances for a table and its own totals to disagree.
    """
    from .observability.usage import run_usage
    u = run_usage(run)
    return {"id": run["id"], "status": run["status"], "trigger": run.get("trigger"),
            "createdAt": run.get("createdAt"),
            "pendingApproval": run.get("pendingApproval"), "error": run.get("error"),
            "traceLength": len(run.get("trace", [])),
            "tokens": u["inputTokens"] + u["outputTokens"], "costUsd": u.get("costUsd")}


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
            "environment": current_environment(),
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

# How far back the derived kill-switch history reaches. Each policy record can
# yield several rows (one per tool it changed), so this bounds reads, not rows.
_SWITCH_LOG_LIMIT = 20


def _switch_history(store, manifest, limit: int = _SWITCH_LOG_LIMIT) -> list:
    """Per-tool kill-switch transitions, DERIVED from the policy log.

    The console wants "who moved which tool from what to what, when". The log
    stores document snapshots: each record is the whole `{"tool:<id>": …}` map
    plus the whole map as it was before. So the per-tool transitions are not
    read, they are diffed out — the log never records which tool an operator
    touched, only the before and after of the map.

    That is why this exists rather than the view simply calling
    `policy_history`: the shapes do not line up, and the old code read a
    hand-maintained `history` collection in the legacy memory scope that nothing
    has written since §11.2 (audit §4.5).

    `actor` comes along, because `store.py` cites §12 risk 7 — "who reviewed this
    allowlist change is a feature" — and the field was being written on every
    record and shown on no screen.
    """
    from .sdk.context import POLICY_KILLSWITCHES

    reader = getattr(store, "policy_history", None)
    if reader is None:
        return []
    declared = {t.id: t.permission.value for t in manifest.tools}
    rows = []
    for rec in reader(POLICY_KILLSWITCHES, limit):   # newest first
        after = rec.get("value") or {}
        before = rec.get("previous") or {}
        common = {"ts": rec.get("changedAt"), "actor": rec.get("actor"),
                  "version": rec.get("version")}
        for k in sorted(set(after) | set(before)):
            if not k.startswith("tool:"):
                continue
            new, old = after.get(k), before.get(k)
            if (new or {}).get("permission") == (old or {}).get("permission"):
                continue  # the record changed some OTHER tool
            tool = k.split(":", 1)[1]
            prev = (old or {}).get("permission")
            if new is None:
                # Cleared: the override is gone and the manifest governs again.
                rows.append({**common, "tool": tool, "cleared": True,
                             "permission": declared.get(tool), "previous": prev})
            else:
                rows.append({**common, "tool": tool, "cleared": False,
                             "permission": new.get("permission"), "previous": prev,
                             "reason": new.get("reason")})
    return rows


def _governance(manifest, store, runs, project_root) -> dict:
    """The control-plane governance surface: what is enforced, under which
    policy version, what has been overridden, and what got blocked. Everything
    here reflects REAL enforcement state - nothing is aspirational.

    That last sentence was not true and is the point of this function's shape.
    Audit §4.5: both governed sources had moved and neither read moved with them.
    Kill switches came from `load_memory("_runtime_config")`, the pre-§11.2 scope
    that nothing writes any more — so an operator who killed a tool during an
    incident saw "No overrides." on the one screen they opened to confirm it. And
    egress came from `rya.guard.yaml` on disk while the Guard editor writes the
    store, so on a published bundle (no file) this reported "off · 0 rules" over
    a deny-default policy that was actively refusing requests.

    Three rules follow, and every value below obeys them:

      * **Read what enforces.** `read_killswitches` and `effective_policy` are
        the readers the runtime itself uses. Not a copy of them.
      * **Report the EFFECTIVE state.** Tool counts are manifest ∘ kill switches,
        because "Denied: 0" while a tool is killed is the same lie in a tile.
      * **A source that cannot be read says so.** `switchesError`/`egressError`
        exist so an unreachable policy store renders as "could not read" and
        never as the empty table that means "nothing is overridden".
    """
    import hashlib
    import json as _json

    from .auth import jwt_configured
    from .guard import GUARD_FILE, effective_policy
    from .sdk.context import read_killswitches
    from .seal import available as seal_available

    env = os.environ

    # ---- egress + grounding: the policy actually in force -------------------
    gp = effective_policy(store, manifest.name,
                          guard_file=Path(project_root) / GUARD_FILE if project_root else None)
    gy = gp.policy if gp.enforced else {}
    grounding_on = bool((gy.get("grounding") or {}).get("enabled"))

    # ---- tool permissions: manifest, as overridden --------------------------
    try:
        switches, switches_error = read_killswitches(store), None
    except Exception as e:
        # Fail LOUD, not empty. `_effective_tool_permission` fails closed on the
        # same error; a dashboard's job is to say the reading is unavailable.
        switches, switches_error = {}, f"{type(e).__name__}: {e}"

    def _effective(t) -> str:
        return (switches.get(f"tool:{t.id}") or {}).get("permission") or t.permission.value

    effective = {t.id: _effective(t) for t in manifest.tools}
    pinned = sum(1 for t in manifest.tools if getattr(t, "pin", None))
    gated = sum(1 for v in effective.values() if v == "approval_required")
    denied = sum(1 for v in effective.values() if v == "disabled")
    overridden = sum(1 for t in manifest.tools if effective[t.id] != t.permission.value)

    # The policy document is everything that constrains the agent. Its hash is
    # the version an auditor can pin a run to — so it has to cover what is in
    # force, not what was declared. It hashes the guard's ETAG (a content hash of
    # the normalised live policy, whatever source it came from) rather than file
    # bytes, and effective permissions rather than manifest ones. Before this the
    # hash moved for NEITHER a kill switch nor a store-backed allowlist change,
    # which made it an auditable pin to a document nobody was enforcing.
    policy_material = _json.dumps({
        "tools": [{"id": t.id, "permission": effective[t.id],
                   "pin": sorted(getattr(t, "pin", {}) or {})} for t in manifest.tools],
        "guard": gp.etag,
        "approverIdentityRequired": env.get("RYA_REQUIRE_APPROVER_IDENTITY") == "1",
        "multiTenant": _multitenant(),
    }, sort_keys=True)
    policy_hash = hashlib.sha256(policy_material.encode()).hexdigest()[:16]

    overrides = [dict(v, tool=k.split(":", 1)[1]) for k, v in switches.items()
                 if k.startswith("tool:")]
    overrides.sort(key=lambda o: o["tool"])
    try:
        history = _switch_history(store, manifest)
    except Exception as e:
        history, switches_error = [], switches_error or f"{type(e).__name__}: {e}"

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
        "policy": {
            "hash": policy_hash,
            "toolsGated": gated, "toolsDenied": denied, "pinnedArgTools": pinned,
            # How many of those counts are an operator override rather than the
            # manifest, so the tiles can distinguish "the agent declares this" from
            # "someone killed it at 03:00".
            "toolsOverridden": overridden,
            "egressRules": len(gy.get("rules") or []),
            "egressDefault": gy.get("default") if gp.enforced else None,
            # Provenance: `store` / `file:…` / `none`, plus the guard's own version
            # — the same string `PUT /guard` returns, so the two screens can be
            # matched — and any read failure, which is a deny-everything state.
            "egressSource": gp.source,
            "egressVersion": gp.version if gp.enforced else None,
            "egressError": gp.error,
        },
        "enforcement": {
            "egressGuard": gp.enforced,
            "groundingGate": grounding_on,
            "approverIdentity": env.get("RYA_REQUIRE_APPROVER_IDENTITY") == "1",
            "perUserIdentity": jwt_configured(),
            "multiTenantRls": _multitenant(),
            "secretsSealed": seal_available(),
        },
        "switches": {
            "active": overrides,
            "history": history,
            # The policy log versions the whole switches MAP, so there is no
            # per-tool version to show; this is the document's. The console used
            # to render `v{o.version}` off the per-override dicts, which never
            # carried one.
            "version": history[0]["version"] if history else None,
            "error": switches_error,
        },
        "violations": violations,
    }



def _branding(project_root) -> Optional[dict]:
    """Env-driven whitelabel for the console: RYA_BRAND_NAME + optional
    RYA_BRAND_TAGLINE and RYA_BRAND_LOGO (path to a small png/svg, embedded
    as a data URI). Absent env -> None -> stock Rya chrome."""
    env = dict(os.environ)
    try:
        from dotenv import dotenv_values
        for k, v in (dotenv_values(Path(project_root) / ".env") or {}).items():
            env.setdefault(k, v)  # process env wins; .env bakes defaults into the image
    except Exception:
        pass
    name = env.get("RYA_BRAND_NAME")
    if not name:
        return None
    out = {"name": name, "tagline": env.get("RYA_BRAND_TAGLINE") or "agent control plane"}
    logo = env.get("RYA_BRAND_LOGO")
    if logo:
        lp = Path(logo) if Path(logo).is_absolute() else Path(project_root) / logo
        if lp.is_file() and lp.stat().st_size <= 200_000:
            import base64
            mime = "image/svg+xml" if lp.suffix == ".svg" else f"image/{lp.suffix.lstrip('.')}"
            out["logo"] = f"data:{mime};base64," + base64.b64encode(lp.read_bytes()).decode()
    return out

def build_console(manifest, store, agent, project_root) -> dict:
    """Rich aggregate state for the web console — everything one dashboard needs
    in a single call, computed from the live runtime (not mocked).

    ``agent`` is the IMPORTED handler set and is now optional (D21): a
    manifest-free api serves agents whose code it has deliberately never
    unpacked, so it can report what an agent *declares* without being able to
    report which handlers it registered. ``handlers`` is then ``None`` rather
    than ``false`` — ``build_snapshot`` has drawn that distinction all along, and
    "we did not look" must not render as "there is no event handler".
    """
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

    # Which agent each pending approval belongs to. `list_approvals` has no agent axis
    # in storage, so it is resolved through the run — the same way `app.py`'s
    # `_approvals_of` does it. `runs` is already loaded for THIS agent, so the common
    # case (every approval is the selected agent's) costs no extra reads and only a
    # foreign approval pays for one.
    _run_agent = {r["id"]: r.get("agent") for r in runs}

    def _agent_of(a: dict):
        rid = a.get("runId") or ""
        if rid in _run_agent:
            return _run_agent[rid]
        run = store.get_run(rid)
        return run.get("agent") if run else None

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
        "branding": _branding(project_root),
        "governance": _governance(manifest, store, runs, project_root),
        "agent": {"name": manifest.name, "version": manifest.version,
                  "runtime": manifest.runtime, "environment": current_environment(),
                  "status": "running",
                  "handlers": ({"event": agent.event_handler() is not None,
                                "jobs": list(agent._job_handlers.keys())}
                               if agent is not None else None)},
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
        # `pending` is `list_approvals("pending")` — deliberately WORKSPACE-wide, the
        # same inbox `GET /approvals` serves ("everything awaiting a human here" is a
        # real question, `app.py: list_approvals`). Every other key in this snapshot is
        # scoped to one agent, so each row carries `agent` and the console says which
        # ones are not the selected one. Narrowing here instead would be worse: an
        # approval is the only irreversible human gate in the product, and hiding one
        # because a different agent happens to be selected is how a run waits forever.
        "approvals": [{"id": a["id"], "title": a["title"], "body": a.get("body"),
                       "action": a.get("action"), "runId": a["runId"],
                       "agent": _agent_of(a)} for a in pending],
        # A PREVIEW, and named one now that it is not a view's dataset: the
        # console's Runs table pages `GET /agents/{id}/runs` instead of filtering
        # inside this cap, which is what made a search for run 31 answer "No runs
        # match" (audit §5.1). `stats.runs` and `stats.byStatus` above are the
        # authoritative totals and are computed over every run, not over the cap.
        "runs": [run_summary(r) for r in runs[:30]],
        "connections": [{"id": c["id"], "provider": c.get("provider"), "scopes": c.get("scopes", []),
                         "owner": c.get("owner"), "status": c.get("status"),
                         "secretSet": c.get("secretSet"), "encrypted": c.get("encrypted"),
                         "label": c.get("label")} for c in connections],
        # Also a preview — see the note on `runs`. The Conversations view pages
        # `GET /agents/{id}/sessions`, because conversation 51 was unreachable
        # from the console at all (audit §5.2); `stats.sessions` is the total.
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
