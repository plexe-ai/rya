"""Rya MCP server (FastMCP).

Exposes Rya's control-plane operations as MCP tools so MCP-native coding agents
(Claude Code, Cursor, etc.) can drive Rya directly instead of shelling out to
the CLI. Every tool returns structured JSON with a stable ``ok`` flag; failures
carry a stable error code + a suggested next action.

Run with::

    rya mcp                 # stdio transport (local agents)
    rya mcp --http          # remote MCP over HTTP (hosted; agents connect by URL)

Requires the [mcp] extra: ``pip install 'rya[mcp]'``.
"""

from __future__ import annotations

from typing import Optional

from . import ops

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError(
        "The Rya MCP server requires the [mcp] extra. Install with: pip install 'rya[mcp]'"
    ) from exc


# stateless_http: every request is self-contained (no server-side session
# affinity), which is what a hosted, multi-client remote MCP needs.
mcp = FastMCP("rya", stateless_http=True)


@mcp.tool()
def rya_create_agent(name: str, project_dir: Optional[str] = None) -> dict:
    """Scaffold a new Rya agent project named `name` under `project_dir` (default: cwd)."""
    return ops.create_agent(name, project_dir)


@mcp.tool()
def rya_get_agent(project_dir: Optional[str] = None) -> dict:
    """Return the full agent manifest (identity, tools, models, channels, triggers)."""
    return ops.get_agent(project_dir)


@mcp.tool()
def rya_check_readiness(project_dir: Optional[str] = None) -> dict:
    """Production-readiness checklist. Returns {ready, blocks, warnings}: each block
    must be fixed before deploy (it carries a stable `code` + an exact `fix`). Make
    this all-green — `ready: true` — to ship a production-grade agent safely."""
    return ops.check_readiness(project_dir)


@mcp.tool()
def rya_eval(only: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    """Run the declarative eval suite (rya.evals.yaml) against the runtime — each
    case fires a REAL event and scores the resulting run (status, tools called,
    approvals, tokens/cost, trace contents, optional LLM judge). Returns {passing,
    passed, total, score, results}. Make `passing: true` to prove the agent
    behaves before deploy. Pass `only` to run a single case by id."""
    return ops.run_evals(only, project_dir)


@mcp.tool()
def rya_connect(provider: str, scopes: Optional[list] = None, token: Optional[str] = None,
                user: Optional[str] = None, label: Optional[str] = None,
                project_dir: Optional[str] = None) -> dict:
    """Create a SCOPED, VAULTED connected credential a tool uses to act on a
    provider (github, slack, stripe…). The secret is vaulted — never returned or
    visible to the model — and injected into tool calls by the runtime. Tools bind
    via `provider:` + `scopes:` in the manifest; at call time the runtime enforces
    the intersection rule: required scopes ⊆ (connection scopes ∩ user scopes).
    Pass `user` to bind to a specific user, else it's workspace-shared."""
    return ops.connect(provider, scopes, token, user, label, project_dir)


@mcp.tool()
def rya_list_connections(project_dir: Optional[str] = None) -> dict:
    """List scoped connections (provider, scopes, owner, status, secretSet,
    encrypted) — the secret value is NEVER included."""
    return ops.list_connections(project_dir)


@mcp.tool()
def rya_reseal_connections(project_dir: Optional[str] = None) -> dict:
    """Encrypt any legacy plaintext connection secrets at rest — a one-time,
    idempotent migration for connections created before encryption-at-rest. Returns
    {scanned, resealed, alreadyEncrypted, noSecret, keySource}."""
    return ops.reseal_connections(project_dir)


@mcp.tool()
def rya_provision(project_dir: Optional[str] = None, target: str = "auto", apply: bool = True) -> dict:
    """Stand up the FULL base infrastructure for a production-grade agent in one
    call: durable database, memory, conversation sessions, authentication,
    guardrails (egress firewall), the real-time WebSocket channel, background
    jobs with retry + dead-letter, horizontal scale, observability, and secrets.
    Returns the component inventory (each with a status + fix), what was
    provisioned, and the exact commands to serve it. Idempotent — re-running
    converges. Use `target: postgres` for durable/multi-worker production,
    `apply: false` to inspect without writing."""
    return ops.provision(project_dir, target, apply)


@mcp.tool()
def rya_context(project_dir: Optional[str] = None, recent: int = 5) -> dict:
    """One-shot snapshot of the WHOLE agent backend — manifest, tools+permissions,
    models, channels, handlers, recent runs, pending approvals/jobs, active
    store/LLM backend, the rules to respect, and suggested next actions. Call this
    FIRST to avoid discovering state through trial and error."""
    return ops.context(project_dir, recent)


@mcp.tool()
def rya_validate_manifest(project_dir: Optional[str] = None) -> dict:
    """Validate the manifest AND that the agent code imports + registers a handler."""
    return ops.validate_manifest(project_dir)


@mcp.tool()
def rya_deploy_agent(project_dir: Optional[str] = None) -> dict:
    """Validate the project and report a deploy plan. Ships NOTHING.

    Deliberately not a deploy: `rya deploy --env` and `rya publish` record an
    immutable version and can flip an environment pointer, and a coding agent
    should not do that without a human running the command. Run those in a shell.
    """
    return ops.deploy_agent(project_dir)


@mcp.tool()
def rya_trigger_event(type: str = "message.received", payload: Optional[dict] = None,
                      source: str = "mcp", project_dir: Optional[str] = None) -> dict:
    """Trigger a run by sending a test event. Returns the run id + status; a run may
    pause for human approval (status `waiting_approval`)."""
    return ops.trigger_event(type, payload, source, project_dir)


@mcp.tool()
def rya_list_runs(agent: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    """List recent runs with their status and pending-approval id (if any)."""
    return ops.list_runs(agent, project_dir)


@mcp.tool()
def rya_get_run_trace(run_id: str, project_dir: Optional[str] = None) -> dict:
    """Return the full durable trace for a run: every tool/model/memory/approval step."""
    return ops.get_run_trace(run_id, project_dir)


@mcp.tool()
def rya_list_tools(project_dir: Optional[str] = None) -> dict:
    """List declared tools and their permission levels."""
    return ops.list_tools(project_dir)


@mcp.tool()
def rya_register_tool(id: str, permission: str = "allowed", project_dir: Optional[str] = None) -> dict:
    """Declare a tool in the manifest. permission: read_only|allowed|approval_required|disabled."""
    return ops.register_tool(id, permission, project_dir)


@mcp.tool()
def rya_list_approvals(status: Optional[str] = None, project_dir: Optional[str] = None) -> dict:
    """List human approvals (optionally filter by pending|approved|rejected)."""
    return ops.list_approvals(status, project_dir)


@mcp.tool()
def rya_approve_action(approval_id: str, project_dir: Optional[str] = None) -> dict:
    """Approve a pending action; the paused run resumes and the action executes."""
    return ops.approve_action(approval_id, project_dir)


@mcp.tool()
def rya_reject_action(approval_id: str, project_dir: Optional[str] = None) -> dict:
    """Reject a pending action; the run terminates as rejected."""
    return ops.reject_action(approval_id, project_dir)


@mcp.tool()
def rya_list_models(project_dir: Optional[str] = None) -> dict:
    """List declared models, their type, permission, and version."""
    return ops.list_models(project_dir)


@mcp.tool()
def rya_register_model(id: str, type: str = "external", permission: str = "allowed",
                       project_dir: Optional[str] = None) -> dict:
    """Declare a model in the manifest. type: external|custom|llm."""
    return ops.register_model(id, type, permission, project_dir)


@mcp.tool()
def rya_create_schedule(id: str, schedule: str, handler: str, project_dir: Optional[str] = None) -> dict:
    """Add a cron trigger to the manifest. `schedule` is a cron expression."""
    return ops.create_schedule(id, schedule, handler, project_dir)


@mcp.tool()
def rya_list_channels(project_dir: Optional[str] = None) -> dict:
    """List configured channels (webhook, slack, email, ...) and whether they're enabled."""
    return ops.list_channels(project_dir)


@mcp.tool()
def rya_connect_channel(type: str, project_dir: Optional[str] = None) -> dict:
    """Enable a channel in the manifest."""
    return ops.connect_channel(type, project_dir)


@mcp.tool()
def rya_status(project_dir: Optional[str] = None) -> dict:
    """Overall runtime status: run counts by status, pending approvals, pending jobs."""
    return ops.status(project_dir)


def run() -> None:
    """Entry point for `rya mcp` — serve over stdio (local agents)."""
    mcp.run()


def run_http(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the same tools over HTTP (remote MCP). Agents connect to
    ``http://host:port/mcp``. This is the hosted/cloud transport."""
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


def mounted_app():
    """ASGI app to mount at ``/mcp`` on the control plane, so ``rya serve``
    exposes the API, the console, AND remote MCP from one origin. The internal
    path is normalized to root so the external path is exactly ``/mcp``.

    Each call builds a FRESH FastMCP (cloning the registered tools) so it has its
    OWN session manager — multiple control-plane apps in one process (e.g. a test
    suite, or a future multi-bind server) never share a single-use session
    manager. Returns ``(asgi_app, session_manager)``; the caller must run
    ``session_manager.run()`` inside the host app's lifespan.
    """
    fresh = FastMCP("rya", stateless_http=True)
    fresh.settings.streamable_http_path = "/"
    for t in mcp._tool_manager.list_tools():
        fresh.add_tool(t.fn, name=t.name, description=t.description)
    app = fresh.streamable_http_app()
    return app, fresh.session_manager


if __name__ == "__main__":  # pragma: no cover
    run()
