"""MCP operations — the actual logic behind every Rya MCP tool.

Each function returns a JSON-serializable dict with ``ok: true|false``. Failures
are returned as structured errors (never raised) so a coding agent always gets
machine-readable output with a stable code and a suggested next action — the
same contract as the CLI's ``--json`` mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..errors import RyaError
from ..manifest import find_manifest, load_manifest
from ..runtime import Engine, load_agent
from ..store import open_store
from ..cli import scaffold


def _err(e: RyaError) -> dict:
    return e.to_dict()


def _resolve_root(project_dir: Optional[str]) -> Path:
    """Find the project root (dir containing rya.agent.yaml)."""
    if project_dir:
        root = Path(project_dir).expanduser().resolve()
        if (root / "rya.agent.yaml").is_file():
            return root
        found = find_manifest(root)
        if found is not None:
            return found.parent
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            f"No rya.agent.yaml under {root}.",
            hint="Pass a project_dir that contains rya.agent.yaml, or call rya_create_agent first.",
        )
    found = find_manifest()
    if found is None:
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            "No rya.agent.yaml in the current directory or any parent.",
            hint="Call rya_create_agent first, or pass project_dir.",
        )
    return found.parent


def _store(project_dir):
    root = _resolve_root(project_dir)
    manifest = load_manifest(root / "rya.agent.yaml")
    store = open_store(root)
    return root, manifest, store


def _engine(project_dir) -> Engine:
    root = _resolve_root(project_dir)
    manifest = load_manifest(root / "rya.agent.yaml")
    agent = load_agent(manifest, root)
    return Engine(manifest, agent, open_store(root), root)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def create_agent(name: str, project_dir: Optional[str] = None, template: str = "minimal") -> dict:
    try:
        base = Path(project_dir).expanduser().resolve() if project_dir else Path.cwd()
        target = base / name
        files = scaffold.write_project(target, name, template=template)
        return {
            "ok": True,
            "name": name,
            "path": str(target),
            "files": files,
            "next": [
                f"rya_validate_manifest(project_dir='{target}')",
                "rya_trigger_event(type='message.received', payload={'email':'ada@example.com'})",
            ],
        }
    except RyaError as e:
        return _err(e)


def get_agent(project_dir: Optional[str] = None) -> dict:
    try:
        _, manifest, _ = _store(project_dir)
        return {"ok": True, "agent": manifest.model_dump(mode="json")}
    except RyaError as e:
        return _err(e)


def check_readiness(project_dir: Optional[str] = None) -> dict:
    """Production-readiness checklist — blocks (must fix) + warnings, each with a fix."""
    try:
        from ..readiness import check_readiness as _check
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        store = open_store(root)
        agent = load_agent(manifest, root)
        return {"ok": True, **_check(manifest, store, agent, root)}
    except RyaError as e:
        return _err(e)


def run_evals(only: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    """Run the declarative eval suite (rya.evals.yaml) against the runtime — each
    case fires a real event and scores the run. Returns {ok, passed, total, score,
    results}. Use this to prove the agent BEHAVES before deploy."""
    try:
        from ..evals import run_evals as _run
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        store = open_store(root)
        agent = load_agent(manifest, root)
        res = _run(manifest, agent, store, root, only=only)
        # `ok` is call-success; the suite's pass/fail is surfaced as `passing`.
        res["passing"] = res.pop("ok")
        return {"ok": True, **res}
    except RyaError as e:
        return _err(e)


def connect(provider: str, scopes: Optional[list] = None, token: Optional[str] = None,
            user: Optional[str] = None, label: Optional[str] = None,
            project_dir: Optional[str] = None) -> dict:
    """Create a scoped, vaulted connection a tool uses to act on a provider. The
    secret is vaulted (never returned/exposed to the model) and injected into tool
    calls at runtime; the runtime enforces required ⊆ (connection ∩ user)."""
    try:
        root = _resolve_root(project_dir)
        store = open_store(root)
        conn = store.create_connection(provider, scopes or [], secret=token, owner=user, label=label)
        return {"ok": True, "connection": conn}
    except RyaError as e:
        return _err(e)


def list_connections(project_dir: Optional[str] = None) -> dict:
    """List connections (provider, scopes, owner, status) — never the secret."""
    try:
        root = _resolve_root(project_dir)
        store = open_store(root)
        conns = store.list_connections()
        return {"ok": True, "connections": conns, "count": len(conns)}
    except RyaError as e:
        return _err(e)


def reseal_connections(project_dir: Optional[str] = None) -> dict:
    """Encrypt any legacy plaintext connection secrets at rest (idempotent migration)."""
    try:
        from ..seal import available, key_source
        root = _resolve_root(project_dir)
        if not available():
            return _err(RyaError("E_CRYPTO_UNAVAILABLE",
                                 "cryptography is not installed — cannot encrypt secrets.",
                                 hint="pip install cryptography."))
        store = open_store(root)
        return {"ok": True, "keySource": key_source(root), **store.reseal_connections()}
    except RyaError as e:
        return _err(e)


def provision(project_dir: Optional[str] = None, target: str = "auto", apply: bool = True) -> dict:
    """Stand up the full base infrastructure for a production agent — database,
    memory, sessions, auth, guardrails, the real-time WebSocket channel, jobs
    with retry + dead-letter, horizontal scale, observability, secrets — and
    report the inventory + the exact commands to serve it. Idempotent."""
    try:
        from ..provision import provision as _provision
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        store = open_store(root)
        agent = load_agent(manifest, root)
        return {"ok": True, **_provision(manifest, store, agent, root, target=target, apply=apply)}
    except RyaError as e:
        return _err(e)


def context(project_dir: Optional[str] = None, recent: int = 5) -> dict:
    """Whole live backend state in one call — so the agent doesn't explore."""
    try:
        from ..snapshot import build_snapshot
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        store = open_store(root)
        agent = load_agent(manifest, root)
        return {"ok": True, **build_snapshot(manifest, store, agent, recent, project_root=root)}
    except RyaError as e:
        return _err(e)


def validate_manifest(project_dir: Optional[str] = None) -> dict:
    try:
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        agent = load_agent(manifest, root)
        return {
            "ok": True,
            "valid": True,
            "agent": manifest.name,
            "version": manifest.version,
            "eventHandler": agent.event_handler() is not None,
            "jobHandlers": list(agent._job_handlers.keys()),
            "tools": [t.id for t in manifest.tools],
            "models": [m.id for m in manifest.models],
            "ready": agent.event_handler() is not None,
        }
    except RyaError as e:
        return _err(e)


def deploy_agent(project_dir: Optional[str] = None) -> dict:
    try:
        root = _resolve_root(project_dir)
        manifest = load_manifest(root / "rya.agent.yaml")
        load_agent(manifest, root)  # validates the code imports
        return {
            "ok": True,
            "deployed": False,
            "validated": True,
            "target": "local",
            "agent": manifest.name,
            "version": manifest.version,
            "message": "Validated locally. Hosted control-plane deploy is a later milestone.",
        }
    except RyaError as e:
        return _err(e)


def trigger_event(type: str = "message.received", payload: Optional[dict] = None,
                  source: str = "mcp", project_dir: Optional[str] = None) -> dict:
    try:
        engine = _engine(project_dir)
        run = engine.run_event(type, payload or {}, source)
        out = {
            "ok": True,
            "runId": run["id"],
            "status": run["status"],
            "pendingApproval": run.get("pendingApproval"),
        }
        if run["status"] == "waiting_approval":
            out["guidance"] = (
                f"Run is paused for human approval. Call rya_list_approvals to see it, "
                f"then rya_approve_action('{run['pendingApproval']}') or rya_reject_action(...)."
            )
        return out
    except RyaError as e:
        return _err(e)


def list_runs(agent: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    try:
        _, _, store = _store(project_dir)
        runs = store.list_runs(agent)
        return {"ok": True, "count": len(runs), "runs": [
            {"id": r["id"], "status": r["status"], "trigger": r["trigger"],
             "createdAt": r["createdAt"], "pendingApproval": r.get("pendingApproval")}
            for r in runs
        ]}
    except RyaError as e:
        return _err(e)


def get_run_trace(run_id: str, project_dir: Optional[str] = None) -> dict:
    try:
        _, _, store = _store(project_dir)
        run = store.get_run(run_id)
        if run is None:
            return _err(RyaError("E_RUN_NOT_FOUND", f"Run '{run_id}' not found.",
                                 hint="Call rya_list_runs to see valid run ids."))
        return {"ok": True, "run": run}
    except RyaError as e:
        return _err(e)


def list_tools(project_dir: Optional[str] = None) -> dict:
    try:
        from ..tools.registry import default_registry
        _, manifest, _ = _store(project_dir)
        reg = default_registry()
        items = []
        for t in manifest.tools:
            spec = reg.get(t.id)
            items.append({"id": t.id, "permission": t.permission.value,
                          "registered": spec is not None,
                          "externalSideEffects": spec.external_side_effects if spec else None})
        return {"ok": True, "tools": items}
    except RyaError as e:
        return _err(e)


def register_tool(id: str, permission: str = "allowed", project_dir: Optional[str] = None) -> dict:
    try:
        import yaml
        root = _resolve_root(project_dir)
        p = root / "rya.agent.yaml"
        raw = yaml.safe_load(p.read_text()) or {}
        tools = raw.setdefault("tools", [])
        if any(t.get("id") == id for t in tools):
            return _err(RyaError("E_VALIDATION", f"Tool '{id}' already declared.",
                                 hint="Edit it directly in rya.agent.yaml."))
        tools.append({"id": id, "permission": permission})
        p.write_text(yaml.safe_dump(raw, sort_keys=False))
        return {"ok": True, "registered": id, "permission": permission}
    except RyaError as e:
        return _err(e)


def list_approvals(status: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    try:
        _, _, store = _store(project_dir)
        items = store.list_approvals(status)
        return {"ok": True, "count": len(items), "approvals": [
            {"id": a["id"], "status": a["status"], "title": a["title"],
             "body": a["body"], "action": a["action"], "runId": a["runId"]}
            for a in items
        ]}
    except RyaError as e:
        return _err(e)


def approve_action(approval_id: str, project_dir: Optional[str] = None) -> dict:
    try:
        engine = _engine(project_dir)
        run = engine.approve(approval_id)
        return {"ok": True, "approvalId": approval_id, "runId": run["id"], "runStatus": run["status"]}
    except RyaError as e:
        return _err(e)


def reject_action(approval_id: str, project_dir: Optional[str] = None) -> dict:
    try:
        engine = _engine(project_dir)
        run = engine.reject(approval_id)
        return {"ok": True, "approvalId": approval_id, "runId": run["id"], "runStatus": run["status"]}
    except RyaError as e:
        return _err(e)


def list_models(project_dir: Optional[str] = None) -> dict:
    try:
        from ..models.registry import default_registry
        _, manifest, _ = _store(project_dir)
        reg = default_registry()
        items = []
        for m in manifest.models:
            spec = reg.get(m.id)
            items.append({"id": m.id, "type": m.type, "permission": m.permission.value,
                          "version": spec.version if spec else None, "registered": spec is not None})
        return {"ok": True, "models": items}
    except RyaError as e:
        return _err(e)


def register_model(id: str, type: str = "external", permission: str = "allowed",
                   project_dir: Optional[str] = None) -> dict:
    try:
        import yaml
        root = _resolve_root(project_dir)
        p = root / "rya.agent.yaml"
        raw = yaml.safe_load(p.read_text()) or {}
        models = raw.setdefault("models", [])
        if any(m.get("id") == id for m in models):
            return _err(RyaError("E_VALIDATION", f"Model '{id}' already declared."))
        models.append({"id": id, "type": type, "permission": permission})
        p.write_text(yaml.safe_dump(raw, sort_keys=False))
        return {"ok": True, "registered": id}
    except RyaError as e:
        return _err(e)


def create_schedule(id: str, schedule: str, handler: str, project_dir: Optional[str] = None) -> dict:
    try:
        import yaml
        root = _resolve_root(project_dir)
        p = root / "rya.agent.yaml"
        raw = yaml.safe_load(p.read_text()) or {}
        triggers = raw.setdefault("triggers", [])
        if any(t.get("id") == id for t in triggers):
            return _err(RyaError("E_VALIDATION", f"Trigger '{id}' already exists."))
        triggers.append({"id": id, "type": "cron", "schedule": schedule, "handler": handler})
        p.write_text(yaml.safe_dump(raw, sort_keys=False))
        return {"ok": True, "created": id, "schedule": schedule}
    except RyaError as e:
        return _err(e)


def list_channels(project_dir: Optional[str] = None) -> dict:
    try:
        _, manifest, _ = _store(project_dir)
        return {"ok": True, "channels": [c.model_dump() for c in manifest.channels]}
    except RyaError as e:
        return _err(e)


def connect_channel(type: str, project_dir: Optional[str] = None) -> dict:
    try:
        import yaml
        root = _resolve_root(project_dir)
        p = root / "rya.agent.yaml"
        raw = yaml.safe_load(p.read_text()) or {}
        channels = raw.setdefault("channels", [])
        found = next((c for c in channels if c.get("type") == type), None)
        if found is None:
            channels.append({"type": type, "enabled": True})
        else:
            found["enabled"] = True
        p.write_text(yaml.safe_dump(raw, sort_keys=False))
        return {"ok": True, "connected": type}
    except RyaError as e:
        return _err(e)


def status(project_dir: Optional[str] = None) -> dict:
    try:
        _, manifest, store = _store(project_dir)
        runs = store.list_runs()
        counts: dict = {}
        for r in runs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {
            "ok": True,
            "agent": manifest.name,
            "version": manifest.version,
            "runs": {"total": len(runs), "byStatus": counts},
            "approvalsPending": len(store.list_approvals("pending")),
            "jobsPending": len(store.list_jobs("pending")),
        }
    except RyaError as e:
        return _err(e)
