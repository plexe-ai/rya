"""Control-plane + data-plane API — the deployable single-worker runtime.

Realizes the spec's "Minimum API" against the substrate-agnostic store, plus a
**live webhook trigger** (drive a real run over HTTP) and **auth**. Two modes:

- **Single-tenant** (default): one ``RYA_TOKEN`` operator token gates the control
  API; everything lives in workspace ``default``.
- **Multi-tenant** (``RYA_MULTITENANT=1`` + Postgres): each request authenticates
  with a per-workspace **API key** (``rya_sk_…``); the request is scoped to that
  workspace and the data plane connects as the non-superuser ``rya_app`` role so
  Postgres RLS enforces tenant isolation at the database layer.

Inbound webhooks (``/inbound``) can additionally require an HMAC signature
(``RYA_WEBHOOK_SECRET``) for third-party senders.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse

from .. import __version__ as RYA_VERSION
from .. import agents
from ..agents import AgentRef
from ..config import current_environment
from ..errors import RyaError

_STARTED_AT = time.time()

# When D28 Rule 6's unprefixed fallback stops answering. Stated on the wire as a
# `Sunset` header rather than only in a changelog, because the callers that need
# to move are scripts, not readers.
#
# The plan retires Rule 6 at the end of Phase 3 and Phase 3 has no date, so this
# is a deliberate outer bound rather than a schedule: a `Sunset` needs an instant,
# and one far enough out to be safe is more honest than omitting the header and
# leaving callers to discover the removal. Move it in when Phase 3 lands a date.
RULE6_SUNSET = "Thu, 31 Dec 2026 23:59:59 GMT"

# The reserved "whichever agent this deployment serves" path segment. Predates
# D28 — the console and `cloud.py` have always sent it — and survives it as the
# EXPLICIT spelling of Rule 6's fallback. See `Plane.agent`.
SOLE_AGENT_ALIAS = "_"


def build_infra(store, manifest=None) -> dict:
    """Live infrastructure facts about the running runtime — computed from the
    actual process/env, so the console reflects real platform state."""
    def auth_mode():
        if multitenant_enabled():
            return "multi-tenant · API keys + Postgres RLS"
        if os.environ.get("RYA_JWT_SECRET") or os.environ.get("RYA_JWKS_URL"):
            return "JWT (per-user identity)"
        if os.environ.get("RYA_TOKEN"):
            return "operator token"
        return "open (local dev)"

    def trace_export():
        if os.environ.get("LANGFUSE_HOST"):
            return "Langfuse"
        if os.environ.get("RYA_TRACE_WEBHOOK"):
            return "webhook"
        return "local trace store"

    return {
        "version": RYA_VERSION,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "pid": os.getpid(),
        "uptimeSeconds": int(time.time() - _STARTED_AT),
        "environment": current_environment(),
        "store": store.describe(),
        "auth": {"mode": auth_mode(),
                 "webhookSignature": bool(os.environ.get("RYA_WEBHOOK_SECRET")),
                 "rls": multitenant_enabled()},
        # `manifest` is now the ADDRESSED agent's, not the deployment's, and a
        # deployment serving none has no tracing declaration to report (D21).
        "observability": {"traces": getattr(manifest, "observability", None)
                          and manifest.observability.traces,
                          "export": trace_export()},
        "planes": {"controlPlane": "FastAPI (this process)", "dataPlane": "in-process worker"},
        "endpoints": ["POST /inbound", "POST /agents/:id/events", "WS /ws (real-time)",
                      "GET /runs/:id/trace", "POST /approvals/:id/approve", "GET /console"],
        "realtime": {"websocket": "/ws", "protocol": "json frames: event|message|replay|ping"},
    }


def _run_summary(run: dict) -> dict:
    """Compact run view sent over the WebSocket (full trace is streamed separately)."""
    from ..observability.usage import run_usage
    u = run_usage(run)
    return {"id": run["id"], "status": run["status"], "trigger": run.get("trigger"),
            "pendingApproval": run.get("pendingApproval"), "error": run.get("error"),
            "traceLength": len(run.get("trace", [])),
            "tokens": u["inputTokens"] + u["outputTokens"], "costUsd": u.get("costUsd")}


def _console_dist_dir():
    """Directory of the built React console, or None if it was never built.

    Absent is an ORDINARY state, not an error: `dist/` is gitignored build output
    (source lives in `web/console/`), so a fresh clone that has not run
    `npm run build` has no bundle. `/` then serves a short explainer naming the
    build command instead of 404ing.

    This used to be the *second* console. The legacy single-file SPA that held `/`
    is gone — every one of its 23 views now has a React component, so keeping a
    120KB inline-JS copy around would mean maintaining two consoles that drift.
    """
    import importlib.resources as ir
    try:
        d = ir.files("rya").joinpath("console/dist")
        return d if d.joinpath("index.html").is_file() else None
    except Exception:  # pragma: no cover - non-filesystem loaders (zipimport)
        return None


def _console_index() -> Optional[str]:
    if _CONSOLE_DIST is None:
        return None
    try:
        return _CONSOLE_DIST.joinpath("index.html").read_text(encoding="utf-8")
    except Exception:  # pragma: no cover - unreadable asset behaves as unbuilt
        return None


_CONSOLE_DIST = _console_dist_dir()

# Content-Security-Policy for the console.
#
# `script-src` is now plain 'self' with NO 'unsafe-inline', which is the one security
# win the build step buys outright: the legacy console was a single file with its whole
# application inline, so it could not have this. Now that the React bundle is the only
# console, the allowance is gone rather than merely unused.
#
# Styles keep 'unsafe-inline' because the components still use a few inline `style=`
# attributes. Fonts stay on Google's CDN, which is non-executable.
_CONSOLE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_CONSOLE_HEADERS = {
    "Content-Security-Policy": _CONSOLE_CSP,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
from ..manifest import load_manifest
from ..store import open_store


@dataclass
class Plane:
    """What a control-plane route gets handed. Deliberately **not** an `Engine`.

    Every route used to depend on `Engine`, which carries a manifest and an
    imported `Agent` — so "the api does not run tenant code" was a property of
    what the handlers happened to call, not of what they were given. D21 makes it
    a property of the type: there is no manifest and no handler on this object, so
    a route cannot execute tenant code by reaching for one, and a route that needs
    an agent's *declarations* has to say which agent it means.

    `project` is the mounted `rya.agent.yaml` when a single-tenant deployment has
    one — a dev convenience, never the deployment's identity (see `agents.py`).
    """

    store: Any
    root: Path
    environment: str
    multitenant: bool
    project: Any = None

    @property
    def workspace(self) -> str:
        return getattr(self.store, "workspace_id", "default") or "default"

    def agent(self, agent_id: str) -> "AgentRef":
        """The addressed agent, or `E_AGENT_NOT_FOUND` (D28 Rule 2).

        `_` is the reserved SOLE-AGENT alias, and it is what the console and
        `cloud.py` have always sent. Keeping it working is not keeping the old
        behaviour: `{agent_id}` used to be decorative, so *every* value resolved
        to the deployment's one manifest and a typo was indistinguishable from a
        hit. Now `_` means "the one agent, and refuse if there is more than one",
        which is the difference between an alias and a placeholder.
        """
        if agent_id == SOLE_AGENT_ALIAS:
            agent_id = self.sole_agent()
        return agents.resolve(self.store, agent_id, self.root, self.environment)

    def sole_agent(self) -> str:
        """The one agent served here, or `E_AGENT_AMBIGUOUS` naming the candidates."""
        found = self.agent_names()
        if len(found) == 1:
            return found[0]
        raise RyaError(
            "E_AGENT_AMBIGUOUS",
            f"This deployment serves {len(found)} agents, so '{SOLE_AGENT_ALIAS}' "
            "cannot say which one you mean." if found else
            "This deployment serves no agents yet.",
            hint=f"Address one explicitly: /agents/{{{'|'.join(found)}}}/…" if found else
                 "Publish one with `rya deploy`.",
        )

    def agent_names(self) -> list:
        return agents.names(self.store, self.root)


def _bearer(authorization: Optional[str], x_rya_token: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return x_rya_token


def _check_token(authorization: Optional[str], x_rya_token: Optional[str]) -> None:
    token = os.environ.get("RYA_TOKEN")
    if not token:
        return  # open mode (local dev)
    provided = _bearer(authorization, x_rya_token)
    if not provided or not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail={
            "code": "E_UNAUTHORIZED", "message": "Missing or invalid operator token.",
            "hint": "Send 'Authorization: Bearer $RYA_TOKEN' (or X-Rya-Token)."})


def _verify_signature(raw: bytes, signature: Optional[str]) -> None:
    secret = os.environ.get("RYA_WEBHOOK_SECRET")
    if not secret:
        return
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail={
            "code": "E_BAD_SIGNATURE", "message": "Missing or invalid webhook signature.",
            "hint": "X-Rya-Signature: sha256=HMAC_SHA256(body, RYA_WEBHOOK_SECRET)."})


def auth_enabled() -> bool:
    return bool(os.environ.get("RYA_TOKEN")) or multitenant_enabled()


# Re-exported from `config` so the execution plane can ask the same question
# without importing FastAPI (see config.multitenant_enabled). Kept as a
# module-level name here because callers and tests import it from this module.
from ..config import multitenant_enabled  # noqa: E402


def build_app(root: Path) -> FastAPI:
    root = Path(root)
    mt = multitenant_enabled()
    environment = current_environment()

    # ---- D21: the api boots manifest-free ---------------------------------
    #
    # `load_manifest(root / "rya.agent.yaml")` + `load_agent(...)` used to run
    # right here, and that pair WAS the one-agent limit: one manifest resolved at
    # startup meant every route answered for one agent no matter what `{agent_id}`
    # said, and the control plane imported tenant code just to learn a tool list.
    #
    # Both reads are gone. What agents exist and what they declare now comes from
    # the store (`agents.py`), so the same process serves as many agents as the
    # workspace has published — and imports none of them.
    #
    # A mounted project is now OPTIONAL and, where present, is a single-tenant dev
    # convenience rather than the deployment's identity. It is still validated at
    # boot so a typo in the operator's own file fails at startup rather than on a
    # request; `agents.py` re-reads it per call and treats a bad one as absent.
    project = None
    if not mt and (root / "rya.agent.yaml").is_file():
        project = load_manifest(root / "rya.agent.yaml")

    if mt:
        from ..tenancy import Tenancy
        from ..store_postgres import PostgresStore

        admin_dsn = os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL")
        tenancy = Tenancy(admin_dsn)
        app_data_dsn = tenancy.setup()  # idempotent: tables + rya_app + RLS

        def store_for(workspace_id: str, user_id: Optional[str] = None):
            # user_id (from a verified per-user JWT) drives the app.user_id GUC, so
            # Postgres per-user RLS isolates users WITHIN a workspace, not just by
            # workspace. None = shared/workspace-level (backward compatible).
            return PostgresStore(app_data_dsn, workspace_id, user_id)

        def authorize(authorization, x_rya_token) -> str:
            key = _bearer(authorization, x_rya_token)
            ws = tenancy.resolve_key(key) if key else None
            if not ws:
                raise HTTPException(status_code=401, detail={
                    "code": "E_UNAUTHORIZED", "message": "Missing or invalid API key.",
                    "hint": "Send 'Authorization: Bearer rya_sk_…' for your workspace."})
            return ws
    else:
        base_store = open_store(root)

    def _plane_for(store) -> Plane:
        return Plane(store=store, root=root, environment=environment,
                     multitenant=mt, project=project)

    from ..auth import jwt_configured, verify_jwt

    def _identity_from(authorization, x_rya_token, required: bool):
        """Resolve a verified user Identity from a JWT (single-tenant), or None."""
        if mt or not jwt_configured():
            return None
        tok = _bearer(authorization, x_rya_token)
        if not tok:
            if required:
                raise HTTPException(status_code=401, detail={
                    "code": "E_UNAUTHORIZED", "message": "JWT required.",
                    "hint": "Send 'Authorization: Bearer <jwt>'."})
            return None
        try:
            return verify_jwt(tok)
        except RyaError as e:
            raise HTTPException(status_code=401, detail=e.to_dict()["error"])

    def _actor_from(authorization, x_rya_token, x_rya_user_token) -> Optional[dict]:
        """Who is acting - {sub, email} from a verified user JWT, or None.

        MT: the workspace key authenticates the CALLER; X-Rya-User-Token (minted
        by POST /v1/token from a session) authenticates the USER. Single-tenant:
        the bearer JWT itself is the user. RYA_REQUIRE_APPROVER_IDENTITY=1 turns
        anonymous approval resolution into a 401 (bank mode: every approval must
        record who approved)."""
        actor = None
        if jwt_configured():
            try:
                if x_rya_user_token:
                    ident = verify_jwt(x_rya_user_token)
                    actor = {"sub": ident.sub, "email": ident.email}
                elif not mt:
                    ident = _identity_from(authorization, x_rya_token, required=False)
                    if ident:
                        actor = {"sub": ident.sub, "email": ident.email}
            except RyaError as e:
                raise HTTPException(status_code=401, detail=e.to_dict()["error"])
        if actor is None and os.environ.get("RYA_REQUIRE_APPROVER_IDENTITY") == "1":
            raise HTTPException(status_code=401, detail={
                "code": "E_APPROVER_IDENTITY_REQUIRED",
                "message": "This deployment requires a user identity to resolve approvals.",
                "hint": "POST /v1/token with your session, then send X-Rya-User-Token."})
        return actor

    async def get_plane(authorization: Optional[str] = Header(None),
                        x_rya_token: Optional[str] = Header(None),
                        x_rya_user_token: Optional[str] = Header(None)) -> Plane:
        if mt:
            ws = authorize(authorization, x_rya_token)
            # Optional per-user identity: the API key authenticates the WORKSPACE;
            # an additional verified user JWT (X-Rya-User-Token) authenticates the
            # USER and turns on per-user RLS for this request.
            user_id = None
            if x_rya_user_token and jwt_configured():
                try:
                    user_id = verify_jwt(x_rya_user_token).sub
                except RyaError as e:
                    raise HTTPException(status_code=401, detail=e.to_dict()["error"])
            return _plane_for(store_for(ws, user_id))
        if jwt_configured():
            _identity_from(authorization, x_rya_token, required=True)  # enforce JWT
        else:
            _check_token(authorization, x_rya_token)
        return _plane_for(base_store)

    # ---- D28 Rule 6: the time-boxed single-agent fallback -------------------
    #
    # Rule 2 moves the agent-scoped-but-unaddressed routes under the existing
    # `/agents/{agent_id}/…` prefix. Every one of them also keeps its old
    # unprefixed path for now, because without that the CLI, the console and
    # `e2e_platform.py` all break in a single commit — and a migration that has to
    # land atomically is a migration that gets deferred.
    #
    # The fallback is only sound while it is unambiguous, so it resolves the sole
    # agent or refuses, naming the candidates. Removed at the end of Phase 3;
    # `Deprecation`/`Sunset` say so on the wire rather than in a changelog.
    DEPRECATED_ROUTE_HEADERS = {
        "Deprecation": "true",
        "Sunset": RULE6_SUNSET,
        "Link": '</docs/MULTITENANT_DESIGN.md>; rel="deprecation"',
    }

    def _addressed(plane: Plane, agent_id: Optional[str],
                   response=None) -> AgentRef:
        """The agent a Rule-2 route is about.

        `agent_id` comes from the path on the prefixed route and is `None` on the
        deprecated unprefixed one.
        """
        if agent_id:
            return plane.agent(agent_id)
        ref = plane.agent(SOLE_AGENT_ALIAS)  # raises E_AGENT_AMBIGUOUS, naming both
        if response is not None:
            for k, v in DEPRECATED_ROUTE_HEADERS.items():
                response.headers[k] = v
        return ref

    # Remote MCP: mount the same MCP tools at /mcp so `rya serve` is a single
    # hosted origin for API + console + MCP. Optional (needs the [mcp] extra).
    mcp_asgi = None
    _mcp_sm = None
    try:
        from ..mcp.server import mounted_app
        mcp_asgi, _mcp_sm = mounted_app()
    except Exception:  # pragma: no cover - mcp extra absent / import issue
        mcp_asgi = None
        _mcp_sm = None

    # ---- inline execution: OFF whenever isolation has to mean something -----
    #
    # PLATFORM_DESIGN §11.7: `_sweeper_loop` and `_jobs_loop` iterate
    # `tenancy.list_workspaces()` and build an engine per workspace, so the API
    # process runs EVERY TENANT'S CODE. §5.1: "the api process must stop
    # executing handler code"; severing this is a precondition for D13, because
    # per-tenant process isolation is meaningless if one shared process executes
    # all of them.
    #
    # So: in multi-tenant mode the api process never runs a handler, full stop —
    # `rya worker` does. In single-tenant mode the inline loops stay on by
    # default, because there a bare `rya serve` IS the whole deployment and
    # silently ceasing to run scheduled jobs would be a worse failure than the
    # isolation gap it closes. Set RYA_API_INLINE_WORKER=0 to turn them off
    # there too; `rya dev` does exactly that and starts a real worker, so the
    # local shape matches the production shape (§10).
    def _inline_worker_enabled() -> bool:
        explicit = os.environ.get("RYA_API_INLINE_WORKER")
        if mt:
            if explicit == "1":
                import logging
                logging.getLogger("rya.api").warning(
                    "RYA_API_INLINE_WORKER=1 ignored: multi-tenant mode never executes "
                    "handler code in the api process (PLATFORM_DESIGN D13). Run `rya worker`.")
            return False
        return explicit != "0"

    # ---- the ONE place the api still imports tenant code -------------------
    _imported = {}

    def _inline_engine(store, agent_name: Optional[str] = None):
        """An `Engine` — i.e. imported handler code — or `None`.

        `None` is the answer everywhere the api must not execute, and callers
        enqueue instead of branching on mode. Two conditions, both narrow:

        1. `_inline_worker_enabled()`. Multi-tenant always says no.
        2. The agent is the **mounted project**. A published agent's code lives in
           a bundle this process has deliberately never unpacked, so the api could
           not run it even if it were allowed to. That is the shape of the D21
           boundary in a multi-agent single-tenant deployment: the control plane
           serves every agent, and `rya worker` executes the ones that are not the
           operator's own working tree.
        """
        if not _inline_worker_enabled() or project is None:
            return None
        if agent_name is not None and agent_name != project.name:
            return None
        from ..runtime import Engine, load_agent

        if "agent" not in _imported:
            # `load_agent` mutates sys.path and never unloads, so this happens at
            # most once per process — the same one-agent-per-process constraint
            # the worker documents.
            _imported["agent"] = load_agent(project, root)
        return Engine(project, _imported["agent"], store, root)

    # ---- global turn-reclaim sweeper (the built-in cron) ------------------
    # A crashed chat turn is reclaimable, but reclaim only happens when someone
    # runs it. In single-tenant mode this background loop is that someone; in
    # multi-tenant mode `rya worker` is, and this loop never starts.
    #
    # The multi-tenant arm this used to have — iterate `list_workspaces()`, build
    # an engine per tenant, run their handlers — is deleted rather than left
    # unreachable behind `_inline_worker_enabled()`. It is the exact code
    # PLATFORM_DESIGN §11.7 names ("the API process runs EVERY TENANT'S CODE"),
    # and a dead branch that does the forbidden thing is one refactor away from
    # being a live one.
    def _sweep_once() -> int:
        from .. import turns as _t
        engine = _inline_engine(base_store) if not mt else None
        if engine is None:
            return 0
        return len(_t.execute_pending(engine, worker_id="sweeper", limit=20))

    async def _sweeper_loop(interval: float):
        import asyncio
        import logging
        log = logging.getLogger("rya.turns.sweeper")
        while True:
            await asyncio.sleep(interval)
            try:
                ran = await asyncio.to_thread(_sweep_once)
                if ran:
                    log.info("reclaimed %d turn(s)", ran)
            except Exception:  # never let the sweeper kill the server
                log.warning("turn sweep failed", exc_info=True)

    # ---- background job worker -------------------------------------------
    # ctx.jobs.schedule() enqueues durable jobs, but a bare `rya serve` had
    # nothing running them (only `rya jobs run --due`). This loop makes served
    # agents complete their pipelines: every RYA_JOBS_WORKER_SECONDS (default 3;
    # 0 disables) it claims + runs every due job.
    _jobs_conc = max(1, int(os.environ.get("RYA_JOBS_CONCURRENCY", "4") or 1))

    def _work_once() -> int:
        engine = _inline_engine(base_store) if not mt else None
        if engine is None:
            return 0
        return len(engine.work_once(concurrency=_jobs_conc))

    async def _jobs_loop(interval: float):
        import asyncio
        import logging
        log = logging.getLogger("rya.jobs.worker")
        while True:
            await asyncio.sleep(interval)
            try:
                ran = await asyncio.to_thread(_work_once)
                if ran:
                    log.info("ran %d due job(s)", ran)
            except Exception:  # never let the worker kill the server
                log.warning("job worker tick failed", exc_info=True)

    from contextlib import AsyncExitStack, asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):
        import asyncio
        import logging
        inline = _inline_worker_enabled()
        if not inline:
            logging.getLogger("rya.api").info(
                "api process executes no handler code (%s) — run `rya worker` to "
                "drain jobs and reclaim turns",
                "multi-tenant" if mt else "RYA_API_INLINE_WORKER=0")
        sweep_seconds = float(os.environ.get("RYA_TURN_SWEEP_SECONDS", "30") or 0) if inline else 0
        task = asyncio.create_task(_sweeper_loop(sweep_seconds)) if sweep_seconds > 0 else None
        jobs_seconds = float(os.environ.get("RYA_JOBS_WORKER_SECONDS", "3") or 0) if inline else 0
        jobs_task = asyncio.create_task(_jobs_loop(jobs_seconds)) if jobs_seconds > 0 else None
        async with AsyncExitStack() as stack:
            if _mcp_sm is not None:
                await stack.enter_async_context(_mcp_sm.run())
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                if jobs_task is not None:
                    jobs_task.cancel()

    # The platform's own version, not an agent's. Under D21 the api serves many
    # agents and no single `manifest.version` describes it; using one would have
    # made the OpenAPI document claim whichever agent happened to be mounted.
    api = FastAPI(title="Rya Control Plane", version=RYA_VERSION, lifespan=_lifespan)

    # A RyaError that escapes a route must not become a 500. Routes that want a
    # specific status still catch locally and win; this is the backstop for codes
    # raised DEEP in a call — a quota refused inside `run_event`, a journal drift
    # inside replay — where every caller would otherwise need its own except.
    _ERROR_STATUS = {
        "E_QUOTA_EXCEEDED": 429,      # retry-after semantics, not a client bug
        "E_PROMOTION_BLOCKED": 422,
        "E_TOOL_PERMISSION_DENIED": 403,
        "E_SCOPE_DENIED": 403,
        "E_POLICY_READONLY": 403,
        "E_UNAUTHORIZED": 401,
        "E_JOURNAL_DRIFT": 409,
        "E_VERSION_NOT_FOUND": 404,
        "E_RUN_NOT_FOUND": 404,
        # D21: `{agent_id}` used to be decorative, so an unknown one could not be
        # wrong. Now it names a real row and a miss is a 404 like any other.
        "E_AGENT_NOT_FOUND": 404,
        "E_AGENT_MANIFEST_INVALID": 500,  # the platform's stored record, not the request
        "E_APPROVAL_NOT_FOUND": 404,
        "E_APPROVAL_NOT_PENDING": 409,
        "E_RUN_NOT_PAUSED": 409,
        "E_VALIDATION": 400,
        "E_BUNDLE_MISMATCH": 409,
        "E_BUNDLE_NOT_FOUND": 404,
        "E_BUNDLE_STORE": 503,        # the operator's bucket, not the caller's request
    }

    @api.exception_handler(RyaError)
    async def _rya_error_handler(request: Request, exc: RyaError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=_ERROR_STATUS.get(exc.code, 400),
                            content=exc.to_dict()["error"])

    # CORS: the console is served SAME-ORIGIN by `rya serve`, so no CORS is needed
    # by default. Cross-origin callers (a dev console on another port) must be
    # explicitly allow-listed via RYA_CORS_ORIGINS (comma-separated). Never the
    # wildcard on a control plane that can approve actions and edit the guard.
    _cors = [o.strip() for o in os.environ.get("RYA_CORS_ORIGINS", "").split(",") if o.strip()]
    if _cors:
        from fastapi.middleware.cors import CORSMiddleware
        api.add_middleware(CORSMiddleware, allow_origins=_cors,
                           allow_methods=["GET", "POST", "PUT"],
                           allow_headers=["Authorization", "Content-Type", "X-Rya-Token"],
                           allow_credentials=False, max_age=600)

    @api.middleware("http")
    async def _security_headers(request: Request, call_next):
        # Remote MCP is privileged (it drives the whole control plane), so when an
        # operator token is configured it is REQUIRED on /mcp.
        p = request.url.path
        if p == "/mcp" or p.startswith("/mcp/"):
            need = os.environ.get("RYA_TOKEN")
            if need:
                authz = request.headers.get("authorization", "")
                tok = authz[7:] if authz.lower().startswith("bearer ") else request.headers.get("x-rya-token")
                if not tok or not hmac.compare_digest(tok, need):
                    from starlette.responses import JSONResponse
                    return JSONResponse(status_code=401, content={"error": {
                        "code": "E_UNAUTHORIZED",
                        "message": "Remote MCP requires the operator token (Authorization: Bearer $RYA_TOKEN)."}})
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    # Project-shipped product UI: if the baked project has a web/ dir, serve it
    # at /app - same origin as the API, so the app needs no CORS and no config.
    _webdir = root / "web"
    if _webdir.is_dir():
        from fastapi.staticfiles import StaticFiles
        api.mount("/app", StaticFiles(directory=str(_webdir), html=True), name="app")

    # The React console (source: web/console/) is now THE console, served at `/`.
    #
    # It used to live at /v2 beside the legacy single-file SPA - the Prefect
    # `ui`/`ui-v2` pattern - so views could land one at a time without taking a
    # working console away from anyone. That migration is finished: all 23 views have
    # React components, so the legacy file is deleted rather than left to drift.
    #
    # Assets are mounted at a NARROW prefix on purpose. A `StaticFiles` mount at "/"
    # would be a route that matches every path, and Starlette matches in registration
    # order - so it would shadow every API route declared after it, which is most of
    # them. Serving index.html from an explicit route and mounting only `/assets`
    # keeps the ordering hazard out of the design instead of relying on this block
    # staying last in the function.
    if _CONSOLE_DIST is not None:
        from fastapi.staticfiles import StaticFiles

        class _ConsoleStatic(StaticFiles):
            """StaticFiles that stamps the console's security headers on every hit.

            A mount bypasses route-level `headers=`, so the CSP has to be attached
            here or the bundle would be served with no policy at all.
            """

            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                for k, v in _CONSOLE_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp

        _assets = _CONSOLE_DIST.joinpath("assets")
        if _assets.is_dir():
            api.mount("/assets", _ConsoleStatic(directory=str(_assets)), name="console_assets")

    @api.get("/", response_class=HTMLResponse)
    def console_page():
        # The console page itself is public (it loads, then authenticates its own
        # data calls). `rya serve` ships the dashboard at its own origin.
        #
        # An unbuilt bundle is an ordinary state for a source checkout, and it must
        # never be a 404 or an import-time crash: say what is missing and name the one
        # command that fixes it. This matters more now than it did at /v2, because
        # there is no longer a second console to fall back to.
        html = _console_index()
        if html is None:
            return HTMLResponse(
                "<!doctype html><title>Rya</title>"
                "<h1>Console bundle not built</h1>"
                "<p>Build it with <code>cd web/console &amp;&amp; npm install &amp;&amp; npm run build</code> "
                "(or <code>scripts/build_console.sh</code> from the repo root), "
                "then restart <code>rya serve</code>.</p>"
                "<p>The API is unaffected: only this page needs the bundle.</p>",
                status_code=503,
                headers=_CONSOLE_HEADERS,
            )
        return HTMLResponse(html, headers=_CONSOLE_HEADERS)

    @api.get("/v2")
    @api.get("/v2/")
    def console_v2_moved():
        # Kept as a redirect rather than deleted: /v2 was the console's address for the
        # whole migration, so it is in bookmarks and in docs. A 308 preserves the
        # method and tells caches the move is permanent.
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=308)

    @api.get("/favicon.ico")
    def favicon():
        from fastapi.responses import Response
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
               "<rect width='32' height='32' rx='7' fill='#1d1c1a'/>"
               "<text x='16' y='22' font-family='monospace' font-size='18' "
               "fill='#fff' text-anchor='middle'>R</text></svg>")
        return Response(svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @api.get("/console")
    def console_state(agent: Optional[str] = None, plane: Plane = Depends(get_plane)):
        # Rich aggregate for the web console — auth-gated like the control routes.
        # Works in BOTH modes: the dependency-injected store is workspace-scoped in
        # multi-tenant mode, so the console shows only the caller's data.
        #
        # `?agent=` selects which one this page is about (D21/D28); omitted, it is
        # the sole agent. `agents` carries the whole list so the console can render
        # a selector without a second round trip, and `agent: null` is a real state
        # — a fresh workspace with nothing published yet still has a dashboard.
        from ..snapshot import build_console
        store = plane.store
        ws_id = getattr(store, "workspace_id", "default")
        ws_name = ws_id
        if mt and ws_id != "default":
            try:
                import psycopg
                with psycopg.connect(os.environ["RYA_DATABASE_URL"], autocommit=True) as c_, c_.cursor() as cur:
                    cur.execute("SELECT name FROM rya_workspaces WHERE id=%s", (ws_id,))
                    row = cur.fetchone()
                    if row:
                        ws_name = row[0]
            except Exception:
                pass

        served = agents.list_refs(store, root, environment)
        selected = next((r for r in served if r.name == agent), None) if agent else \
            (served[0] if len(served) == 1 else None)
        if agent and selected is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_AGENT_NOT_FOUND", "message": f"No agent named '{agent}' is served here.",
                "candidates": [r.name for r in served]})

        viewer = {"workspace": ws_name, "workspaceId": ws_id,
                  "mode": "multi-tenant" if mt else "single-tenant",
                  "user": (selected.manifest.owner if (selected and not mt) else None)}
        listing = {"agents": [r.describe() for r in served],
                   "selectedAgent": selected.name if selected else None}
        if selected is None:
            return {"ok": True, **listing, "agent": None, "viewer": viewer,
                    "infra": build_infra(store)}
        # Handler introspection needs the imported code, so it is only available
        # where the api is already allowed to import it. Elsewhere it is null, not
        # false — "we did not look" and "there is no handler" are different facts.
        eng = _inline_engine(store, selected.name)
        snap = build_console(selected.manifest, store,
                             eng.agent if eng is not None else None, root)
        return {**snap, **listing,
                "infra": build_infra(store, selected.manifest), "viewer": viewer}

    # ---- durable turns: the ONE streaming path (D6) -----------------------
    # `on_token`/`on_trace`/`on_ui` used to be raw Python closures handed into
    # the engine and on into the provider's SSE parser. That only works while the
    # process holding the browser socket is the process executing the handler —
    # which the api/worker split ends. Every stream now goes through the durable
    # turn buffer: the executor APPENDS frames, the endpoint TAILS them by seq.
    # A dropped client resumes with Last-Event-ID; a crashed executor's reclaim
    # just appends more frames.
    from .. import turns as _turns

    def _turn_source(plane: Plane, ref: AgentRef) -> "_turns.TurnSource":
        """What enqueuing a turn needs, without an `Engine`.

        The version comes from the agent's environment pointer rather than from
        the enqueuing process, which is the substantive change D21 makes here.
        The api used to enqueue unpinned — it had no version, being a working tree
        — so any worker claimed the turn and ran it against whatever code it held.
        Now the control plane pins to what is promoted, and `queue.claim`'s
        version filter routes the turn to a worker actually serving it.
        """
        return _turns.TurnSource(store=plane.store, manifest=ref.manifest,
                                 version=ref.version)

    def _kick_turn() -> None:
        """Run a due turn in this process — ONLY where that is allowed (§11.7).
        When it is not, the turn sits on the queue with a lease and `rya worker`
        claims it; latency differs, durability does not.

        A standalone engine on a fresh store, never the request's: sharing a
        connection across the response boundary is how a background task ends up
        writing through a closed one. It took the request's credentials before
        D21, to build a per-workspace engine — the multi-tenant arm is gone (that
        process never executes) and the single-tenant one has exactly one store,
        so there is nothing left to authenticate.
        """
        if mt:
            return  # multi-tenant never executes here; `rya worker` claims it
        engine = _inline_engine(open_store(root))
        if engine is None:
            return
        try:
            _turns.execute_pending(engine, worker_id="inline", limit=1)
        except Exception:  # reclaim will re-drive it; never fail the request
            import logging
            logging.getLogger("rya.turns").warning("inline turn execution failed", exc_info=True)

    def _guard_source(plane: Plane, ref: AgentRef):
        """`(source, key)` for this agent's guard policy.

        The governed store where policy storage exists (workspace-scoped,
        versioned, audited), else the project file for `rya dev`. D28 makes the
        store key agent-qualified, so two agents in one workspace no longer share
        a policy — and the key has to travel with the source, because
        `resolve_policy` would otherwise read the unqualified row the api just
        stopped writing.

        The file arm is reachable for **the mounted project's own agent only**.
        A `rya.guard.yaml` ships inside one project's bundle, so writing another
        agent's policy into this tree would put one agent's governance in a
        different agent's artifact — which is how, before this, publishing a
        second agent and editing its guard silently rewrote the first one's.
        Every other agent is governed in the store from its first write.
        """
        from ..guard import GUARD_FILE, POLICY_KEY, policy_key, store_key_for
        store = plane.store
        if hasattr(store, "policy_get"):
            key = store_key_for(store, ref.name)
            try:
                if store.policy_get(key) is not None:
                    return store, key
            except Exception:
                return store, key  # a read failure must fail closed, not fall back
        if project is not None and ref.name == project.name:
            return str(root / GUARD_FILE), POLICY_KEY
        return store, policy_key(ref.name)

    def _actor(authorization: Optional[str], x_rya_token: Optional[str]) -> Optional[str]:
        """The principal a governance write is attributed to. A verified user
        subject where one exists, else the workspace whose API key was used —
        an anonymous audit trail answers nobody's question."""
        from ..auth import jwt_configured, verify_jwt
        tok = _bearer(authorization, x_rya_token)
        if tok and jwt_configured():
            try:
                return f"user:{verify_jwt(tok).sub}"
            except RyaError:
                pass
        if mt and tok:
            ws = tenancy.resolve_key(tok)
            if ws:
                return f"workspace:{ws}"
        return "operator" if tok else None

    _TURN_IDLE_SECONDS = float(os.environ.get("RYA_TURN_STREAM_IDLE_SECONDS", "60"))

    async def _tail_turn(store, turn_id: str, after: int = -1,
                         is_disconnected=None, stop_on_pause: bool = False):
        """Yield a turn's durable frames in seq order until it should stop.

        The single tail implementation behind /ws, /events/stream and
        /turns/{id}/stream, so all three resume identically.

        ``stop_on_pause`` is the difference between the two contracts. A `run`
        frame with `waiting_approval` is a PAUSE marker, not an ending — the
        approval resolution appends the continuation and the real terminal frame
        to this same buffer, which is why the RESUMABLE endpoint keeps tailing.
        The one-shot transports (/ws, /events/stream) promise that `run` is
        always the last frame the client must wait for, so they stop there and
        the client reconnects with the turn handle after approving.
        """
        cursor = after
        idle = 0
        idle_limit = max(1, int(_TURN_IDLE_SECONDS / 0.3))
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            frames = await asyncio.to_thread(store.stream_read, turn_id, cursor)
            if frames:
                idle = 0
                for f in frames:
                    cursor = f["seq"]
                    yield f
                    if _turns.is_terminal([f]):
                        return
                    if stop_on_pause and f["kind"] == "run":
                        return
            else:
                idle += 1
                if idle >= idle_limit:
                    yield {"seq": cursor, "kind": "idle", "data": None}
                    return
                if idle % 15 == 0:
                    yield {"seq": cursor, "kind": "keepalive", "data": None}
                await asyncio.sleep(0.3)

    @api.websocket("/ws")
    async def agent_ws(websocket: WebSocket):
        """Real-time bidirectional channel to drive the agent and watch it run.

        A client connects (browser, another service, or a coding agent), then:
          - ``{"type":"event","eventType":...,"payload":{...}}`` triggers a real run
            and the run's trace is streamed back live, step by step, ending with a
            ``{"type":"run",...}`` summary.
          - ``{"type":"message","channel":...,"externalId":...,"content":...}`` is the
            conversational form: it threads into a session and streams the reply.
          - ``{"type":"replay","runId":...}`` re-streams a stored run's trace.
          - ``{"type":"ping"}`` → ``{"type":"pong"}``.
        Auth mirrors the HTTP API: ``?token=`` carries the operator token
        (single-tenant) or the ``rya_sk_…`` API key (multi-tenant). ``?agent=``
        selects which agent the socket drives; omitted, it is the sole agent
        (D28 Rule 6), and a deployment serving several refuses rather than
        guessing.
        """
        import asyncio
        await websocket.accept()
        token = websocket.query_params.get("token")
        # ---- authenticate the socket -------------------------------------
        if mt:
            ws_id = tenancy.resolve_key(token) if token else None
            if not ws_id:
                await websocket.send_json({"type": "error", "code": "E_UNAUTHORIZED",
                                           "message": "API key required (?token=rya_sk_…)."})
                await websocket.close(code=4401)
                return
            plane = _plane_for(store_for(ws_id))
        else:
            need = os.environ.get("RYA_TOKEN")
            if need and (not token or not hmac.compare_digest(token, need)):
                await websocket.send_json({"type": "error", "code": "E_UNAUTHORIZED",
                                           "message": "operator token required (?token=…)."})
                await websocket.close(code=4401)
                return
            plane = _plane_for(base_store)

        # ---- resolve which agent this socket drives ------------------------
        wanted = websocket.query_params.get("agent")
        try:
            served = plane.agent_names()
            if not wanted and len(served) != 1:
                raise RyaError(
                    "E_AGENT_AMBIGUOUS",
                    f"This deployment serves {len(served)} agents; name one with ?agent=."
                    if served else "This deployment serves no agents yet.",
                    hint=f"Candidates: {', '.join(served)}" if served else
                         "Publish one with `rya deploy`.")
            ref = plane.agent(wanted or served[0])
        except RyaError as e:
            await websocket.send_json({"type": "error", **e.to_dict()["error"]})
            await websocket.close(code=4400)
            return

        await websocket.send_json({"type": "ready", "agent": ref.name,
                                   "version": ref.manifest.version,
                                   "agents": served})
        loop = asyncio.get_running_loop()

        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid JSON"})
                continue
            mtype = msg.get("type")

            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if mtype == "replay":
                run = plane.store.get_run(msg.get("runId", ""))
                if run is None:
                    await websocket.send_json({"type": "error", "code": "E_RUN_NOT_FOUND"})
                    continue
                for ev in run.get("trace", []):
                    await websocket.send_json({"type": "trace", "event": ev})
                await websocket.send_json({"type": "run", "run": _run_summary(run)})
                continue

            if mtype in ("event", "message"):
                if mtype == "message":
                    ext = msg.get("externalId") or "web-user"
                    channel = msg.get("channel", "web")
                    event_type = "message.received"
                    payload = {"email": ext, "body": msg.get("content", ""),
                               "channel": channel, "externalId": ext}
                else:
                    event_type = msg.get("eventType", "message.received")
                    payload = msg.get("payload", {})

                # D6: enqueue a durable turn and tail its buffer. The socket is
                # no longer the thing keeping the run alive — a dropped
                # connection or a crashed executor no longer strands it, and the
                # executing process need not be this one.
                try:
                    started = _turns.create_turn(_turn_source(plane, ref), event_type, payload)
                except RyaError as e:
                    await websocket.send_json({"type": "error", **e.to_dict()["error"]})
                    continue
                turn_id = started["turnId"]
                # Give the client the handle immediately so it can reconnect to
                # GET /agents/{id}/turns/{turnId}/stream if the socket drops.
                await websocket.send_json({"type": "turn", "turnId": turn_id})
                # Concurrent with the tail, so frames reach the socket as they
                # are appended rather than in one burst at the end.
                kick = asyncio.create_task(asyncio.to_thread(_kick_turn))
                try:
                    async for f in _tail_turn(plane.store, turn_id, stop_on_pause=True):
                        kind, data = f["kind"], f.get("data")
                        if kind == "keepalive":
                            continue
                        if kind == "idle":
                            await websocket.send_json({"type": "idle", "turnId": turn_id,
                                                       "after": f["seq"]})
                            break
                        if kind == "token":
                            await websocket.send_json({"type": "token", "text": (data or {}).get("text", "")})
                        elif kind == "trace":
                            await websocket.send_json({"type": "trace", "event": data})
                        elif kind == "ui":
                            await websocket.send_json({"type": "ui", **(data or {})})
                        elif kind == "message":
                            await websocket.send_json({"type": "message", "message": data})
                        elif kind == "run":
                            await websocket.send_json({"type": "run", "run": data})
                        elif kind == "error":
                            await websocket.send_json({"type": "error", **(data or {})})
                        else:  # `restart` — a reclaimed executor re-ran the turn
                            await websocket.send_json({"type": kind, "data": data})
                finally:
                    kick.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await kick
                continue

            await websocket.send_json({"type": "error", "message": f"unknown type '{mtype}'"})

    @api.post("/agents/{agent_id}/events/stream")
    async def post_event_stream(agent_id: str, request: Request, background: BackgroundTasks,
                                after: int = -1,
                                plane: Plane = Depends(get_plane),
                                authorization: Optional[str] = Header(None),
                                x_rya_token: Optional[str] = Header(None)):
        """Trigger a run and stream it back as Server-Sent Events.

        The default client transport (plain HTTP - works through ALBs, proxies,
        and `fetch` with no connection upgrade). Frames, in order of arrival:

          event: turn     {"turnId": ...}        - FIRST; the resume handle
          event: token    {"text": ...}          - each streamed LLM chunk
          event: trace    {trace event}          - each journaled step
          event: message  {assistant message}    - session replies (chat agents)
          event: run      {run summary}          - ALWAYS the terminal frame
          event: error    {code, message}        - fatal error (then closes)

        Clients wait for `run` and never have to guess whether more is coming.

        Since D6 this is a thin view over a DURABLE turn: the run is enqueued
        with a lease, its frames are appended to a store-backed buffer, and this
        endpoint only tails that buffer. A mid-turn crash no longer strands the
        run, and a dropped connection resumes with the `turn` handle against
        GET /agents/{id}/turns/{turnId}/stream (or `?after=<lastSeq>` here).
        """
        from fastapi.responses import StreamingResponse

        body = await request.json()
        event_type = body.get("type", "message.received")
        payload = body.get("payload", {})
        identity = _identity_from(authorization, x_rya_token, required=False)

        started = _turns.create_turn(_turn_source(plane, plane.agent(agent_id)),
                                     event_type, payload, identity=identity)
        turn_id = started["turnId"]

        async def sse():
            # The kick must run CONCURRENTLY with the tail, not before it and not
            # as a response background task: a StreamingResponse's background
            # tasks fire only after the body is fully sent, so scheduling it there
            # deadlocks the stream against the turn it is waiting for.
            kick = asyncio.create_task(asyncio.to_thread(_kick_turn))
            try:
                yield f"event: turn\ndata: {json.dumps({'turnId': turn_id})}\n\n"
                async for f in _tail_turn(plane.store, turn_id, after=after,
                                          is_disconnected=request.is_disconnected,
                                          stop_on_pause=True):
                    if f["kind"] == "keepalive":
                        yield ": keep-alive\n\n"      # comment frame; defeats idle timeouts
                    elif f["kind"] == "idle":
                        yield ": idle-timeout\n\n"    # client reconnects with Last-Event-ID
                    else:
                        yield (f"id: {f['seq']}\nevent: {f['kind']}\n"
                               f"data: {json.dumps(f['data'], default=str)}\n\n")
            finally:
                kick.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await kick

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @api.post("/agents/{agent_id}/turns")
    async def create_turn_ep(agent_id: str, request: Request, background: BackgroundTasks,
                             plane: Plane = Depends(get_plane),
                             authorization: Optional[str] = Header(None),
                             x_rya_token: Optional[str] = Header(None)):
        """Start a DURABLE chat turn. Returns ``{turnId}`` immediately; the turn
        runs on a worker (kicked inline here, reclaimed on crash) and streams via
        GET /agents/{id}/turns/{turnId}/stream."""
        body = await request.json()
        identity = _identity_from(authorization, x_rya_token, required=False)
        res = _turns.create_turn(_turn_source(plane, plane.agent(agent_id)),
                                 body.get("type", "message.received"),
                                 body.get("payload", {}), identity=identity)
        background.add_task(_kick_turn)
        return res

    @api.get("/agents/{agent_id}/turns/{turn_id}/stream")
    async def turn_stream_ep(agent_id: str, turn_id: str, request: Request,
                             after: int = -1, plane: Plane = Depends(get_plane)):
        """Tail a turn's durable stream as SSE. Resumable: reconnect with
        ``?after=<lastSeq>`` (or the browser's Last-Event-ID header) to continue
        exactly where the dropped connection left off. Ends on the terminal
        run/error frame.

        D28 Rule 1: the turn id already determines everything, so `{agent_id}` is
        not used to look anything up. It is still RESOLVED — one indexed read on
        connect, not per frame — because an id that is accepted and never checked
        is exactly the decorative path segment D21 spent this phase removing.
        """
        from fastapi.responses import StreamingResponse

        plane.agent(agent_id)
        last_id = request.headers.get("last-event-id")
        start = int(last_id) if (last_id and last_id.lstrip("-").isdigit()) else after

        async def sse():
            async for f in _tail_turn(plane.store, turn_id, after=start,
                                      is_disconnected=request.is_disconnected):
                if f["kind"] == "keepalive":
                    yield ": keep-alive\n\n"
                elif f["kind"] == "idle":
                    yield ": idle-timeout\n\n"  # client reconnects w/ Last-Event-ID
                else:
                    yield (f"id: {f['seq']}\nevent: {f['kind']}\n"
                           f"data: {json.dumps(f['data'], default=str)}\n\n")

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/agents/{agent_id}/turns/reclaim")
    def reclaim_turns_ep(agent_id: str, plane: Plane = Depends(get_plane)):
        """Reclaim + run any pending or crashed (lease-expired) chat turns for
        this workspace. The durability backstop - call periodically (cron / a
        `rya` worker loop) so an interrupted turn always finishes."""
        engine = _inline_engine(plane.store, plane.agent(agent_id).name)
        if engine is None:
            # Reclaim is EXECUTION, so it belongs to the execution plane. Saying
            # so beats returning `reclaimed: []`, which reads as "nothing was
            # stranded" when it means "nobody looked".
            raise HTTPException(status_code=409, detail={
                "code": "E_NO_INLINE_WORKER",
                "message": "This api process executes no handler code, so it cannot "
                           "reclaim turns.",
                "hint": "`rya worker` reclaims expired leases on every tick — that is "
                        "the durability backstop this endpoint was standing in for."})
        ran = _turns.execute_pending(engine, worker_id="reclaimer", limit=50)
        return {"reclaimed": ran, "count": len(ran)}

    # ---- guard (D28 Rule 2: agent-scoped, addressed under /agents/…) --------
    def _guard_get(plane: Plane, ref: AgentRef):
        from ..guard import resolve_policy, run_tests
        source, key = _guard_source(plane, ref)
        gp = resolve_policy(source, key=key)
        return {"agent": ref.name, "policy": gp.policy, "tests": run_tests(gp),
                "exists": gp.enforced, **gp.describe()}

    @api.get("/agents/{agent_id}/guard")
    def get_guard_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _guard_get(plane, plane.agent(agent_id))

    @api.get("/guard")
    def get_guard(response: Response, plane: Plane = Depends(get_plane)):
        return _guard_get(plane, _addressed(plane, None, response))

    async def _guard_put(request: Request, plane: Plane, ref: AgentRef,
                         authorization, x_rya_token):
        from ..guard import policy_key, run_tests, save_policy
        body = await request.json()
        policy = body.get("policy", body)
        source, _read_key = _guard_source(plane, ref)
        # WRITES always go to the qualified key, even when the READ fell back to
        # the shared pre-D28 row: editing one agent's guard must not rewrite the
        # policy another agent is still inheriting.
        record = save_policy(policy, source=source,
                             actor=_actor(authorization, x_rya_token),
                             key=policy_key(ref.name))
        return {"ok": True, "agent": ref.name, "tests": run_tests(policy),
                "version": record.get("version"), "record": record}

    @api.put("/agents/{agent_id}/guard")
    async def put_guard_for(agent_id: str, request: Request,
                            plane: Plane = Depends(get_plane),
                            authorization: Optional[str] = Header(None),
                            x_rya_token: Optional[str] = Header(None)):
        """Write the agent's guard policy. Versioned, diffed and attributed to a
        principal — §12 risk 7: for a governance product, "who reviewed this
        allowlist change" is a feature, not a residual."""
        return await _guard_put(request, plane, plane.agent(agent_id),
                                authorization, x_rya_token)

    @api.put("/guard")
    async def put_guard(request: Request, response: Response,
                        plane: Plane = Depends(get_plane),
                        authorization: Optional[str] = Header(None),
                        x_rya_token: Optional[str] = Header(None)):
        return await _guard_put(request, plane, _addressed(plane, None, response),
                                authorization, x_rya_token)

    def _guard_log(plane: Plane, ref: AgentRef, limit: int):
        """The policy audit trail: every change, its diff, and who made it.

        Both keys are read. A workspace that predates D28 has its whole history
        under the unqualified `guard` key, and an audit trail that started over at
        the upgrade would be an audit trail with a hole in it.
        """
        from ..guard import POLICY_KEY, policy_key
        history = getattr(plane.store, "policy_history", None)
        if history is None:
            return {"agent": ref.name, "entries": []}
        entries = list(history(policy_key(ref.name), limit)) + list(history(POLICY_KEY, limit))
        entries.sort(key=lambda e: e.get("changedAt") or "", reverse=True)
        return {"agent": ref.name, "entries": entries[:limit]}

    @api.get("/agents/{agent_id}/guard/log")
    def guard_log_for(agent_id: str, limit: int = 50, plane: Plane = Depends(get_plane)):
        return _guard_log(plane, plane.agent(agent_id), limit)

    @api.get("/guard/log")
    def guard_log(response: Response, limit: int = 50, plane: Plane = Depends(get_plane)):
        return _guard_log(plane, _addressed(plane, None, response), limit)

    def _guard_test(plane: Plane, ref: AgentRef):
        from ..guard import resolve_policy, run_tests
        source, key = _guard_source(plane, ref)
        return run_tests(resolve_policy(source, key=key))

    @api.post("/agents/{agent_id}/guard/test")
    def test_guard_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _guard_test(plane, plane.agent(agent_id))

    @api.post("/guard/test")
    def test_guard(response: Response, plane: Plane = Depends(get_plane)):
        return _guard_test(plane, _addressed(plane, None, response))

    # ---- evals -------------------------------------------------------------
    # Both routes read the MOUNTED project's `rya.evals.yaml`, so they only mean
    # anything for the mounted agent. Addressing them is still worth doing —
    # otherwise `/evals` on a two-agent deployment silently answers for whichever
    # project happens to be on disk.
    @api.get("/agents/{agent_id}/evals")
    def get_evals_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _evals_list(plane.agent(agent_id))

    @api.get("/evals")
    def get_evals(response: Response, plane: Plane = Depends(get_plane)):
        return _evals_list(_addressed(plane, None, response))

    def _evals_list(ref: AgentRef):
        from ..evals import EVALS_FILE, load_evals
        if project is None or ref.name != project.name:
            return {"agent": ref.name, "cases": [], "exists": False,
                    "note": "Eval cases live in the project tree; this deployment does "
                            f"not have '{ref.name}' mounted."}
        return {"agent": ref.name, "cases": load_evals(root),
                "exists": (root / EVALS_FILE).is_file()}

    def _evals_run(plane: Plane, ref: AgentRef):
        from ..evals import run_evals
        engine = _inline_engine(plane.store, ref.name)
        if engine is None:
            # Running evals imports and executes the agent, which is the one thing
            # this process must not do (D21). Named rather than 400'd as
            # "single-tenant only", because the real constraint is the plane
            # boundary and it applies to a single-tenant api with the inline worker
            # off too.
            raise HTTPException(status_code=409, detail={
                "code": "E_NO_INLINE_WORKER",
                "message": f"This api process cannot execute '{ref.name}', so it cannot "
                           "run its evals.",
                "hint": "Run them where the code is: `rya eval` in the project, or as a "
                        "readiness attestation in CI before promoting."})
        return run_evals(engine.manifest, engine.agent, plane.store, root)

    @api.post("/agents/{agent_id}/evals/run")
    def post_evals_run_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _evals_run(plane, plane.agent(agent_id))

    @api.post("/evals/run")
    def post_evals_run(response: Response, plane: Plane = Depends(get_plane)):
        return _evals_run(plane, _addressed(plane, None, response))

    @api.get("/healthz")
    def healthz():
        # Report the active store backend so a deploy can confirm it's on Postgres.
        # `agent` is gone: a manifest-free api serves many, and reporting one of
        # them would have been whichever file happened to be mounted (D21). Use
        # `GET /agents` for the list — health is about the process.
        backend = "postgres" if mt else base_store.describe().get("backend")
        return {"ok": True, "authEnabled": auth_enabled(),
                "multiTenant": mt, "store": backend}

    # ---- inbound webhooks --------------------------------------------------
    def _dispatch_event(plane: Plane, ref: AgentRef, event_type: str, payload: dict,
                        source: str, identity=None) -> dict:
        """Start a run for `ref` — inline where that is allowed, queued otherwise.

        The single seam every "fire an event at the agent" route goes through, so
        the plane boundary is decided in one place rather than re-argued per
        route. `_inline_engine` returns None in multi-tenant mode and wherever
        `RYA_API_INLINE_WORKER=0`, and then the run is created, pinned and handed
        to `rya worker` (`turns.enqueue_event`).
        """
        engine = _inline_engine(plane.store, ref.name)
        if engine is not None:
            run = engine.run_event(event_type, payload, source, identity=identity)
            return {"runId": run["id"], "status": run["status"],
                    "pendingApproval": run.get("pendingApproval")}
        out = _turns.enqueue_event(_turn_source(plane, ref), event_type, payload,
                                   trigger_source=source, identity=identity,
                                   environment=environment)
        _kick_turn()  # a no-op unless inline execution is enabled here
        return out

    def _webhook_plane(authorization, x_rya_token) -> Plane:
        """The store an inbound webhook writes through.

        Deliberately NOT `Depends(get_plane)`: single-tenant webhooks are gated by
        SIGNATURE only, because a third-party sender holds the signing secret and
        not the operator token. Multi-tenant still needs the API key, since
        nothing else says which workspace the event belongs to.
        """
        if mt:
            return _plane_for(store_for(authorize(authorization, x_rya_token)))
        return _plane_for(base_store)

    @api.post("/agents/{agent_id}/inbound")
    async def inbound_for(agent_id: str, request: Request,
                          authorization: Optional[str] = Header(None),
                          x_rya_token: Optional[str] = Header(None)):
        plane = _webhook_plane(authorization, x_rya_token)
        return await _inbound(request, plane, plane.agent(agent_id))

    @api.post("/inbound")
    async def inbound(request: Request, response: Response,
                      authorization: Optional[str] = Header(None),
                      x_rya_token: Optional[str] = Header(None)):
        plane = _webhook_plane(authorization, x_rya_token)
        return await _inbound(request, plane, _addressed(plane, None, response))

    async def _inbound(request: Request, plane: Plane, ref: AgentRef):
        raw = await request.body()
        _verify_signature(raw, request.headers.get("x-rya-signature"))
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION", "message": "Body is not valid JSON."})
        event_type = request.headers.get("x-rya-event-type") or (
            body.get("type") if isinstance(body, dict) else None) or "webhook.received"
        payload = body if isinstance(body, dict) else {"data": body}
        try:
            return _dispatch_event(plane, ref, event_type, payload, "webhook")
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])

    @api.post("/agents/{agent_id}/slack/events")
    async def slack_events_for(agent_id: str, request: Request):
        return await _slack_events(request, agent_id)

    @api.post("/slack/events")
    async def slack_events(request: Request):
        return await _slack_events(request, None)

    async def _slack_events(request: Request, agent_id: Optional[str]):
        """Real Slack inbound adapter: verify Slack's signature, answer the
        url_verification handshake, and turn event_callbacks into agent runs.

        Single-tenant only, as before: Slack's signature identifies Slack, not a
        workspace, so there is nothing to scope a multi-tenant request to.
        """
        secret = os.environ.get("RYA_SLACK_SIGNING_SECRET")
        if not secret:
            raise HTTPException(status_code=404, detail={"code": "E_VALIDATION",
                                "message": "Slack adapter not configured (RYA_SLACK_SIGNING_SECRET)."})
        raw = await request.body()
        ts = request.headers.get("x-slack-request-timestamp", "")
        basestring = f"v0:{ts}:{raw.decode()}".encode()
        expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-slack-signature", "")):
            raise HTTPException(status_code=401, detail={"code": "E_BAD_SIGNATURE",
                                "message": "Invalid Slack signature."})
        body = json.loads(raw or b"{}")
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge")}
        if body.get("type") == "event_callback":
            ev = body.get("event", {})
            if mt:
                raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                                    "message": "Slack adapter is single-tenant only."})
            plane = _plane_for(base_store)
            ref = plane.agent(agent_id) if agent_id else _addressed(plane, None)
            out = _dispatch_event(plane, ref, f"slack.{ev.get('type', 'message')}",
                                  ev, "slack")
            return {"ok": True, **out}
        return {"ok": True}

    @api.get("/agents")
    def list_agents_ep(plane: Plane = Depends(get_plane)):
        """Every agent this deployment serves (D21).

        The route that did not exist while a deployment served exactly one agent,
        and the one a client needs before it can address anything under D28.
        """
        served = agents.list_refs(plane.store, root, environment)
        return {"agents": [r.describe() for r in served], "count": len(served)}

    @api.get("/agents/{agent_id}")
    def get_agent(agent_id: str, plane: Plane = Depends(get_plane)):
        ref = plane.agent(agent_id)
        return {**ref.manifest.model_dump(mode="json"), "_source": ref.describe()}

    @api.post("/agents/{agent_id}/events")
    async def post_event(agent_id: str, request: Request, plane: Plane = Depends(get_plane),
                         authorization: Optional[str] = Header(None), x_rya_token: Optional[str] = Header(None)):
        """Fire an event at the agent.

        Returns a run id synchronously in every mode. Whether the run has already
        FINISHED by the time you get it is the difference: `status: "queued"`
        means the control plane created and pinned the run and a worker will
        execute it, which is what this route does everywhere the api is not also
        the worker (D21). See `_dispatch_event`.
        """
        body = await request.json()
        identity = _identity_from(authorization, x_rya_token, required=False)
        out = _dispatch_event(plane, plane.agent(agent_id),
                              body.get("type", "message.received"),
                              body.get("payload", {}), body.get("source", "api"),
                              identity=identity)
        return {**out, "identity": identity.to_dict() if identity else None}

    @api.get("/agents/{agent_id}/runs")
    def list_runs(agent_id: str, plane: Plane = Depends(get_plane)):
        return {"runs": plane.store.list_runs(plane.agent(agent_id).name)}

    @api.post("/agents/{agent_id}/runs/ingest")
    async def ingest_run_for(agent_id: str, request: Request,
                             plane: Plane = Depends(get_plane)):
        return await _ingest_run(request, plane, plane.agent(agent_id))

    @api.post("/runs/ingest")
    async def ingest_run(request: Request, response: Response,
                         plane: Plane = Depends(get_plane)):
        return await _ingest_run(request, plane, _addressed(plane, None, response))

    async def _ingest_run(request: Request, plane: Plane, ref: AgentRef):
        """Ingest a run that executed OUTSIDE Rya (an external agent loop), so
        its trace shows in Runs & traces next to native runs - the single-pane
        step of a sidecar migration. The caller maps its events onto Rya's
        trace vocabulary (tool.call, llm.respond {result: {model, usage}}, log,
        run.completed...); token usage and cost then render natively. The
        caller is responsible for scrubbing PII before shipping.

        Body: {trigger?, status, event?, error?, createdAt?, source?,
               agentVersion?, trace: [{kind, label?, ts?, data?}, ...]}
        """
        from ..store import now_iso
        body = await request.json()
        status = body.get("status")
        if status not in ("completed", "failed", "running", "waiting_approval", "rejected",
                          "needs_reconnect"):
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION",
                "message": "status must be one of completed|failed|running|waiting_approval|"
                           "rejected|needs_reconnect."})
        raw_trace = body.get("trace")
        if not isinstance(raw_trace, list) or not raw_trace or len(raw_trace) > 1000:
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION", "message": "trace must be a non-empty list (max 1000 events)."})
        trace = []
        for i, ev in enumerate(raw_trace):
            if not isinstance(ev, dict) or not ev.get("kind"):
                raise HTTPException(status_code=400, detail={
                    "code": "E_VALIDATION", "message": f"trace[{i}] must be an object with a 'kind'."})
            trace.append({"seq": i, "ts": str(ev.get("ts") or now_iso())[:32],
                          "kind": str(ev["kind"])[:60], "label": str(ev.get("label") or "")[:200],
                          "data": ev.get("data") if isinstance(ev.get("data"), dict) else {}})
        run = {
            "id": plane.store.new_run_id(),
            "agent": ref.name,
            "agentVersion": str(body.get("agentVersion") or "external")[:40],
            "trigger": str(body.get("trigger") or "ingested")[:60],
            "status": status,
            "event": body.get("event") if isinstance(body.get("event"), dict) else None,
            "job": None, "journal": {}, "trace": trace,
            "pendingApproval": None,
            "error": body.get("error") if isinstance(body.get("error"), dict) else None,
            "scheduledJobs": [], "parentRunId": None,
            "createdAt": str(body.get("createdAt") or now_iso())[:32],
            "ingested": True,
            "sourceSystem": str(body.get("source") or "external")[:60],
        }
        plane.store.save_run(run)
        # Onward export (Langfuse/OTLP/webhook) like any finished run - best effort.
        if status in ("completed", "failed", "rejected"):
            try:
                from ..observability.export import export_run
                from ..sdk.context import load_env
                export_run(run, load_env(root))
            except Exception:
                pass
        return {"ok": True, "runId": run["id"], "events": len(trace)}

    # ---- files (uploaded documents) -------------------------------------
    @api.post("/agents/{agent_id}/files")
    async def upload_file_for(agent_id: str, request: Request,
                              plane: Plane = Depends(get_plane)):
        return await _upload_file(request, plane, plane.agent(agent_id))

    @api.post("/files")
    async def upload_file(request: Request, response: Response,
                          plane: Plane = Depends(get_plane)):
        # Rule 3 for the READS (workspace storage), Rule 2 for this one: the
        # upload fires `file.uploaded` AT an agent, so it has to name one — but
        # only when the notification is actually requested.
        notify = request.query_params.get("event", "true").lower() != "false"
        ref = _addressed(plane, None, response) if notify else None
        return await _upload_file(request, plane, ref)

    async def _upload_file(request: Request, plane: Plane, ref: Optional[AgentRef]):
        """Store a file (raw request body) and, by default, fire a
        ``file.uploaded`` event at the agent so a waiting workflow can resume.

        Query params: ``name`` (required), ``tag.<key>=<value>`` (repeatable,
        becomes the file's tags), ``event=false`` to store without notifying.
        """
        name = request.query_params.get("name")
        if not name:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                                                         "message": "query param 'name' is required"})
        content = await request.body()
        if not content:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                                                         "message": "request body is empty"})
        max_bytes = int(os.environ.get("RYA_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail={"code": "E_VALIDATION",
                                                         "message": f"file exceeds {max_bytes} bytes"})
        tags = {k[4:]: v for k, v in request.query_params.items() if k.startswith("tag.")}
        meta = plane.store.save_file(name, content,
                                      content_type=request.headers.get("content-type"),
                                      tags=tags)
        out = {"ok": True, "file": meta}
        if ref is not None and request.query_params.get("event", "true").lower() != "false":
            fired = _dispatch_event(plane, ref, "file.uploaded",
                                    {"fileId": meta["id"], "name": meta["name"],
                                     "tags": meta["tags"], "size": meta["size"],
                                     "contentType": meta["contentType"]},
                                    "upload")
            out["runId"] = fired["runId"]
            out["runStatus"] = fired["status"]
        return out

    @api.post("/files/presign")
    async def presign_file(request: Request, plane: Plane = Depends(get_plane)):
        """Large-file path: register metadata, return a presigned S3 PUT URL.
        The browser uploads DIRECTLY to S3, then calls /files/{id}/confirm."""
        from .. import files_s3
        if not files_s3.bucket():
            raise HTTPException(status_code=409, detail={
                "code": "E_VALIDATION",
                "message": "Presigned uploads need RYA_FILES_S3_BUCKET."})
        body = await request.json()
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                                                         "message": "'name' is required"})
        ctype = body.get("contentType") or "application/octet-stream"
        meta = plane.store.save_file(name, b"", content_type=ctype,
                                      tags={**(body.get("tags") or {}), "_storage": "s3",
                                            "_pending": "1"})
        return {"ok": True, "fileId": meta["id"],
                "uploadUrl": files_s3.presign_put(meta["id"], ctype)}

    @api.post("/agents/{agent_id}/files/{file_id}/confirm")
    def confirm_file_for(agent_id: str, file_id: str, plane: Plane = Depends(get_plane)):
        return _confirm_file(file_id, plane, plane.agent(agent_id))

    @api.post("/files/{file_id}/confirm")
    def confirm_file(file_id: str, response: Response,
                     plane: Plane = Depends(get_plane)):
        return _confirm_file(file_id, plane, _addressed(plane, None, response))

    def _confirm_file(file_id: str, plane: Plane, ref: AgentRef):
        """After a presigned PUT: verify the object landed, fire file.uploaded."""
        from .. import files_s3
        meta = plane.store.get_file(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_NOT_FOUND", "message": f"No file '{file_id}'.",
                "hint": "Presign first (POST /files/presign) — confirm applies to a file the "
                        "platform already knows about."})
        h = files_s3.head(file_id)
        if h is None:
            raise HTTPException(status_code=409, detail={
                "code": "E_VALIDATION", "message": "object not found in S3 - upload first"})
        tags = {k: v for k, v in (meta.get("tags") or {}).items() if k != "_pending"}
        fired = _dispatch_event(plane, ref, "file.uploaded",
                                {"fileId": file_id, "name": meta["name"], "tags": tags,
                                 "size": h["size"], "contentType": h.get("contentType")},
                                "upload")
        return {"ok": True, "runId": fired["runId"], "runStatus": fired["status"],
                "size": h["size"]}

    @api.get("/files")
    def list_files(plane: Plane = Depends(get_plane)):
        return {"files": plane.store.list_files()}

    @api.get("/files/{file_id}")
    def get_file(file_id: str, plane: Plane = Depends(get_plane)):
        meta = plane.store.get_file(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_NOT_FOUND", "message": f"No file '{file_id}'.",
                "hint": "List what exists with GET /files."})
        return meta

    @api.get("/runs/{run_id}")
    def get_run(run_id: str, plane: Plane = Depends(get_plane)):
        run = plane.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "E_RUN_NOT_FOUND"})
        return run

    @api.get("/runs/{run_id}/trace")
    def get_trace(run_id: str, plane: Plane = Depends(get_plane)):
        run = plane.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "E_RUN_NOT_FOUND"})
        return {"runId": run_id, "trace": run["trace"]}

    @api.get("/connections")
    def list_connections(plane: Plane = Depends(get_plane)):
        # Metadata only — the store never returns secret values.
        return {"connections": plane.store.list_connections()}

    # Knowledge is stored in the workspace's `knowledge` memory scope, which has
    # no agent axis. D28 lists it under Rule 2, so both prefixed routes exist —
    # but they are addressing, not isolation, and saying so beats implying an
    # per-agent corpus that is not there. Splitting the storage is its own change.
    @api.get("/agents/{agent_id}/knowledge")
    def knowledge_for(agent_id: str, plane: Plane = Depends(get_plane)):
        plane.agent(agent_id)  # 404 on an unknown agent rather than an empty list
        return _knowledge(plane)

    @api.get("/knowledge")
    def knowledge(plane: Plane = Depends(get_plane)):
        return _knowledge(plane)

    def _knowledge(plane: Plane):
        km = plane.store.load_memory("knowledge")
        return {"documents": km.get("documents", []),
                "chunks": len(km.get("collections", {}).get("chunks", []))}

    @api.post("/agents/{agent_id}/knowledge/search")
    async def knowledge_search_for(agent_id: str, request: Request,
                                   plane: Plane = Depends(get_plane)):
        plane.agent(agent_id)
        return await knowledge_search(request, plane)

    @api.post("/knowledge/search")
    async def knowledge_search(request: Request, plane: Plane = Depends(get_plane)):
        from ..providers.embeddings import cosine, embed
        import re
        body = await request.json()
        query = (body or {}).get("query", "")
        km = plane.store.load_memory("knowledge")
        chunks = km.get("collections", {}).get("chunks", [])
        env = os.environ
        qv = embed(query, env)
        qtokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored = []
        for c in chunks:
            vec = cosine(qv, c.get("_embedding")) if c.get("_embedding") else 0.0
            ctok = set(re.findall(r"[a-z0-9]+", c.get("text", "").lower()))
            lex = (len(qtokens & ctok) / len(qtokens)) if qtokens else 0.0
            score = vec + 0.5 * lex
            if score > 0:
                scored.append({"text": c["text"], "source": c.get("source"),
                               "docId": c.get("docId"), "_score": round(score, 4)})
        scored.sort(key=lambda r: r["_score"], reverse=True)
        return {"query": query, "hits": scored[:int((body or {}).get("limit", 5))]}

    @api.get("/agents/{agent_id}/sessions")
    def list_sessions_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return {"sessions": plane.store.list_sessions(plane.agent(agent_id).name)}

    @api.get("/sessions")
    def list_sessions(response: Response, plane: Plane = Depends(get_plane)):
        ref = _addressed(plane, None, response)
        return {"sessions": plane.store.list_sessions(ref.name)}

    @api.get("/agents/{agent_id}/sessions/find")
    def find_session_for(agent_id: str, channel: str, externalId: str,
                         plane: Plane = Depends(get_plane)):
        return _find_session(plane, plane.agent(agent_id), channel, externalId)

    @api.get("/sessions/find")
    def find_session(channel: str, externalId: str, response: Response,
                     plane: Plane = Depends(get_plane)):
        return _find_session(plane, _addressed(plane, None, response), channel, externalId)

    def _find_session(plane: Plane, ref: AgentRef, channel: str, externalId: str):
        """Resolve the session for a (channel, externalId) identity - how a chat
        client locates its own thread after a reload without listing everything."""
        session = plane.store.find_session(ref.name, channel, externalId)
        if session is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND",
                "message": f"No session for {channel}/{externalId}.",
                "hint": "A session is created by the first turn on that identity — send one, "
                        "then resolve it."})
        return session

    @api.get("/sessions/{session_id}")
    def get_session(session_id: str, plane: Plane = Depends(get_plane)):
        session = plane.store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND", "message": f"No session '{session_id}'.",
                "hint": "Resolve it by identity with GET /sessions/find?channel=…&externalId=…"})
        return session

    @api.get("/sessions/{session_id}/messages")
    def session_messages(session_id: str, limit: int = 200,
                         plane: Plane = Depends(get_plane)):
        """The durable transcript, oldest first - restored on client reload
        (render instantly; never replay a typewriter)."""
        if plane.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND", "message": f"No session '{session_id}'.",
                "hint": "An empty transcript and an unknown session are different answers; "
                        "this one is unknown."})
        msgs = plane.store.list_messages(session_id)
        return {"messages": msgs[-limit:]}

    def _approvals_of(plane: Plane, agent: Optional[str], status: Optional[str]):
        """Approvals, optionally narrowed to one agent.

        `list_approvals` has no agent axis in storage, so the filter is applied
        over the runs the approvals belong to. Honest but O(n) in approvals, which
        is fine for a pending list and is why only the AGENT-addressed route pays
        it — the workspace-level view stays a single read.
        """
        rows = plane.store.list_approvals(status)
        if agent is None:
            return rows
        out = []
        for a in rows:
            run = plane.store.get_run(a.get("runId") or "")
            if run is not None and run.get("agent") == agent:
                out.append(a)
        return out

    @api.get("/agents/{agent_id}/approvals")
    def list_approvals_for(agent_id: str, status: Optional[str] = None,
                           plane: Plane = Depends(get_plane)):
        return {"approvals": _approvals_of(plane, plane.agent(agent_id).name, status)}

    @api.get("/approvals")
    def list_approvals(status: Optional[str] = None, plane: Plane = Depends(get_plane)):
        # Rule 3, not Rule 6: "everything awaiting a human in this workspace" is a
        # real question and the one an inbox asks. No deprecation header — this
        # route is not going away, it just stops being the only one.
        return {"approvals": _approvals_of(plane, None, status)}

    @api.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, plane: Plane = Depends(get_plane),
                authorization: Optional[str] = Header(None),
                x_rya_token: Optional[str] = Header(None),
                x_rya_user_token: Optional[str] = Header(None)):
        """Approve, and resume the run.

        D28 Rule 1: the approval names its run and the run names its agent and its
        version, so nothing has to be addressed.

        Approving is the one governance action that RUNS TENANT CODE — it executes
        the approved action and then replays the handler against the resolved
        journal — so where the api may not execute, it records the decision and a
        worker carries it out (`turns.enqueue_resume`). The response says which
        happened: `queued: true` with `runStatus: "resuming"` means a worker has
        it. That also ends `E_JOURNAL_DRIFT` on a published run, because the
        resume is pinned to the version that paused it rather than to whatever the
        api imported at boot.
        """
        actor = _actor_from(authorization, x_rya_token, x_rya_user_token)
        approval = plane.store.get_approval(approval_id)
        run = plane.store.get_run((approval or {}).get("runId") or "") if approval else None
        engine = _inline_engine(plane.store, (run or {}).get("agent"))
        try:
            if engine is not None:
                resumed = _turns.resolve_on_stream(engine, approval_id, approve=True,
                                                   actor=actor)
                return {"approvalId": approval_id, "runStatus": resumed["status"],
                        "runId": resumed["id"], "turnId": resumed.get("turnId"),
                        "queued": False, "resolvedBy": actor}
            job = _turns.enqueue_resume(plane.store, approval_id, actor=actor)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": "resuming",
                "runId": (run or {}).get("id"), "turnId": (run or {}).get("turnId"),
                "queued": True, "jobId": job["id"], "resolvedBy": actor}

    @api.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, plane: Plane = Depends(get_plane),
               authorization: Optional[str] = Header(None),
               x_rya_token: Optional[str] = Header(None),
               x_rya_user_token: Optional[str] = Header(None)):
        """Reject, and fail the run.

        Stays synchronous in every mode, unlike `/approve`: rejecting executes no
        action and replays no handler, so there is no tenant code to keep out of
        this process (`turns.reject_approval`). Making it async for symmetry would
        add latency to the path an operator uses to STOP something.
        """
        actor = _actor_from(authorization, x_rya_token, x_rya_user_token)
        try:
            run = _turns.reject_on_stream(plane.store, approval_id, actor=actor)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"],
                "runId": run["id"], "turnId": run.get("turnId"),
                "queued": False, "resolvedBy": actor}

    # ---- queue: durable jobs for external workers (Sim et al.) -----------
    from .. import queue as q

    _Q_HTTP = {"E_JOB_NOT_FOUND": 404, "E_QUEUE_CONFLICT": 409, "E_VALIDATION": 400}

    def _q_err(e: RyaError) -> HTTPException:
        return HTTPException(status_code=_Q_HTTP.get(e.code, 400), detail=e.to_dict()["error"])

    @api.post("/queue/jobs")
    async def queue_enqueue(request: Request, plane: Plane = Depends(get_plane)):
        body = await request.json()
        try:
            job = q.enqueue(
                plane.store, body.get("type"), body.get("payload"),
                job_id=body.get("jobId"), max_attempts=body.get("maxAttempts", 1),
                delay_seconds=body.get("delaySeconds", 0), priority=body.get("priority", 0),
                tags=body.get("tags"), metadata=body.get("metadata"),
                concurrency_key=body.get("concurrencyKey"),
                concurrency_limit=body.get("concurrencyLimit"),
                retry_delay_seconds=body.get("retryDelaySeconds"))
        except RyaError as e:
            raise _q_err(e)
        return {"job": job}

    @api.post("/queue/jobs/batch")
    async def queue_enqueue_batch(request: Request, plane: Plane = Depends(get_plane)):
        body = await request.json()
        try:
            jobs = q.enqueue_batch(plane.store, body.get("type"), body.get("items") or [])
        except RyaError as e:
            raise _q_err(e)
        return {"jobs": jobs, "ids": [j["id"] for j in jobs]}

    @api.post("/queue/claim")
    async def queue_claim(request: Request, plane: Plane = Depends(get_plane)):
        """Claim due jobs for an external worker. Optional short long-poll via
        waitSeconds (capped) so idle workers don't hammer the API."""
        import asyncio
        body = await request.json()
        worker_id = body.get("workerId")
        wait = min(float(body.get("waitSeconds") or 0), 25.0)
        deadline = time.monotonic() + wait

        def _claim():
            # `agent` is optional and defaults to None (claim anything), which is
            # what keeps D14's SDK-free surface working for foreign consumers that
            # know nothing about agents. A caller that does know names it (D22).
            return q.claim(plane.store, worker_id, types=body.get("types"),
                           limit=body.get("limit", 1),
                           lease_seconds=body.get("leaseSeconds", q.DEFAULT_LEASE_SECONDS),
                           agent=body.get("agent"))
        try:
            jobs = await asyncio.to_thread(_claim)
            while not jobs and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                jobs = await asyncio.to_thread(_claim)
        except RyaError as e:
            raise _q_err(e)
        return {"jobs": jobs}

    @api.get("/queue/jobs/{job_id}")
    def queue_get_job(job_id: str, plane: Plane = Depends(get_plane)):
        job = plane.store.queue_get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "E_JOB_NOT_FOUND"})
        return {"job": job}

    @api.get("/queue/jobs")
    def queue_list_jobs(status: Optional[str] = None, type: Optional[str] = None,
                        plane: Plane = Depends(get_plane)):
        return {"jobs": plane.store.queue_list(status, type)}

    @api.post("/queue/jobs/{job_id}/heartbeat")
    async def queue_heartbeat(job_id: str, request: Request, plane: Plane = Depends(get_plane)):
        body = await request.json()
        try:
            return q.heartbeat(plane.store, job_id, body.get("workerId"),
                               body.get("extendSeconds", q.DEFAULT_LEASE_SECONDS))
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/complete")
    async def queue_complete(job_id: str, request: Request, plane: Plane = Depends(get_plane)):
        body = await request.json()
        try:
            return {"job": q.complete(plane.store, job_id, body.get("workerId"),
                                      body.get("output"))}
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/fail")
    async def queue_fail(job_id: str, request: Request, plane: Plane = Depends(get_plane)):
        body = await request.json()
        try:
            return {"job": q.fail(plane.store, job_id, body.get("workerId"),
                                  body.get("error") or "unknown error")}
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/cancel")
    async def queue_cancel(job_id: str, request: Request, plane: Plane = Depends(get_plane)):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        return q.cancel(plane.store, job_id, force=bool((body or {}).get("force")))

    @api.post("/queue/jobs/{job_id}/retry")
    def queue_retry(job_id: str, plane: Plane = Depends(get_plane)):
        try:
            return {"job": q.retry(plane.store, job_id)}
        except RyaError as e:
            raise _q_err(e)

    @api.get("/queue/stats")
    def queue_stats(plane: Plane = Depends(get_plane)):
        return q.stats(plane.store)

    def _killswitches(store) -> dict:
        """Read the kill switches the runtime will actually honour.

        §11.2 moved these out of the `_runtime_config` memory scope — which a
        bundle could overwrite through ctx.memory.set — into privileged policy
        state. The legacy scope is still READ so a switch set before the move
        keeps working; nothing writes it any more.

        Still ONE workspace-wide map keyed `tool:<id>`, not per agent. That is
        defensible where the guard key was not: a kill switch answers "stop
        calling `refund.issue` anywhere in this workspace, now", which is exactly
        the blast radius an incident wants. `/tools` is agent-addressed because
        the DECLARATIONS are the agent's; the override that beats them is the
        operator's.
        """
        from ..sdk.context import POLICY_KILLSWITCHES
        getter = getattr(store, "policy_get", None)
        if getter is not None:
            switches = getter(POLICY_KILLSWITCHES)
            if switches is not None:
                return switches
        return (store.load_memory("_runtime_config") or {}).get("kv") or {}

    def _tools_of(plane: Plane, ref: AgentRef):
        # Effective permission = manifest, unless a runtime kill switch overrides.
        overrides = _killswitches(plane.store)
        out = []
        for t in ref.manifest.tools:
            ov = overrides.get(f"tool:{t.id}")
            out.append({
                "id": t.id,
                "permission": t.permission.value,
                "effectivePermission": (ov or {}).get("permission") or t.permission.value,
                "override": ov,
            })
        return {"agent": ref.name, "tools": out}

    @api.get("/agents/{agent_id}/tools")
    def tools_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _tools_of(plane, plane.agent(agent_id))

    @api.get("/tools")
    def tools(response: Response, plane: Plane = Depends(get_plane)):
        return _tools_of(plane, _addressed(plane, None, response))

    @api.put("/agents/{agent_id}/tools/{tool_id}/permission")
    async def set_tool_permission_for(agent_id: str, tool_id: str, request: Request,
                                      plane: Plane = Depends(get_plane),
                                      authorization: Optional[str] = Header(None),
                                      x_rya_token: Optional[str] = Header(None)):
        return await _set_tool_permission(tool_id, request, plane, plane.agent(agent_id),
                                          authorization, x_rya_token)

    @api.put("/tools/{tool_id}/permission")
    async def set_tool_permission(tool_id: str, request: Request, response: Response,
                                  plane: Plane = Depends(get_plane),
                                  authorization: Optional[str] = Header(None),
                                  x_rya_token: Optional[str] = Header(None)):
        return await _set_tool_permission(tool_id, request, plane,
                                          _addressed(plane, None, response),
                                          authorization, x_rya_token)

    async def _set_tool_permission(tool_id: str, request: Request, plane: Plane,
                                   ref: AgentRef, authorization, x_rya_token):
        """Runtime kill switch: override a tool's permission NOW, without a
        redeploy. Body: {"permission": "...", "reason": "..."} or {"clear": true}
        to drop the override and fall back to the manifest.

        Written to privileged policy state (§11.2), so the change is versioned,
        attributed and append-only auditable — and the bundle whose tool is being
        killed cannot write it back.

        The agent is addressed so the tool can be CHECKED against a real
        declaration; the switch itself is workspace-wide (see `_killswitches`).
        """
        from ..manifest.schema import Permission as Perm
        from ..sdk.context import POLICY_KILLSWITCHES
        from ..store import now_iso

        body = await request.json()
        decl = next((t for t in ref.manifest.tools if t.id == tool_id), None)
        if decl is None:
            raise HTTPException(status_code=404, detail={"code": "E_TOOL_NOT_FOUND",
                                "message": f"Tool '{tool_id}' is not declared by '{ref.name}'."})
        setter = getattr(plane.store, "policy_set", None)
        if setter is None:
            raise HTTPException(status_code=501, detail={
                "code": "E_RUNTIME",
                "message": "This store backend does not support privileged policy writes."})

        switches = dict(_killswitches(plane.store))
        prev = (switches.get(f"tool:{tool_id}") or {}).get("permission") or decl.permission.value
        ts = now_iso()
        if body.get("clear"):
            switches.pop(f"tool:{tool_id}", None)
            new = decl.permission.value
        else:
            perm = body.get("permission")
            if perm not in {p.value for p in Perm}:
                raise HTTPException(status_code=400, detail={
                    "code": "E_VALIDATION",
                    "message": f"permission must be one of {sorted(p.value for p in Perm)}."})
            new = perm
            switches[f"tool:{tool_id}"] = {"permission": new, "ts": ts,
                                           "reason": body.get("reason")}
        record = setter(POLICY_KILLSWITCHES, switches,
                        actor=_actor(authorization, x_rya_token))
        return {"ok": True, "tool": tool_id, "permission": new, "previous": prev,
                "cleared": bool(body.get("clear")), "reason": body.get("reason"),
                "version": record.get("version"), "actor": record.get("actor"), "ts": ts}

    @api.get("/tools/log")
    def tool_permission_log(limit: int = 50, plane: Plane = Depends(get_plane)):
        """Who changed which kill switch, when, and what it was before."""
        from ..sdk.context import POLICY_KILLSWITCHES
        history = getattr(plane.store, "policy_history", None)
        return {"entries": history(POLICY_KILLSWITCHES, limit) if history else []}

    # ---- deployments: versions + environments (D11, D12, §9) --------------
    def _deployments():
        from .. import deployments as _d
        return _d

    def _dep_err(e: RyaError) -> HTTPException:
        status = {"E_VERSION_NOT_FOUND": 404, "E_ENVIRONMENT_NOT_FOUND": 404,
                  "E_VERSION_IN_USE": 409, "E_VERSION_RETIRED": 409,
                  "E_BUNDLE_MISMATCH": 409,
                  # 503, not 400: the caller's request was fine and the operator's
                  # bucket is not. Telling a publisher to fix its request would
                  # send it in exactly the wrong direction.
                  "E_BUNDLE_STORE": 503, "E_BUNDLE_NOT_FOUND": 404,
                  # 422: the request is well-formed and the version exists — it is
                  # the version's *evidence* that does not satisfy the gate. A 403
                  # would say "you may not do this"; the caller may, once the
                  # requirements are met.
                  "E_PROMOTION_BLOCKED": 422}.get(e.code, 400)
        return HTTPException(status_code=status, detail=e.to_dict()["error"])

    @api.post("/agents/{agent_id}/versions")
    async def publish_version_ep(agent_id: str, request: Request,
                                 plane: Plane = Depends(get_plane),
                                 authorization: Optional[str] = Header(None),
                                 x_rya_token: Optional[str] = Header(None)):
        """Upload a packed bundle as a new immutable version. The §9 pipeline over
        HTTP: `rya deploy --env` without needing the database or the bucket.

        Body: the raw ``.tar.gz`` from ``bundles.pack``. Query: ``hash``
        (required, the client's content hash), ``env``, ``promote``, ``actor``,
        and repeatable ``meta.<key>=<value>`` provenance — the same shape as
        ``POST /files``.

        **This never imports the bundle** (D13): the control plane must not run
        tenant code, so it verifies bytes, records a version, and flips a pointer.
        The consequence is that readiness is NOT evaluated and no readiness
        attestation is filed — the response says so, and an environment whose gate
        requires readiness will refuse the promotion.
        """
        from .. import bundles, gates

        # An open control plane means anonymous code upload to a box whose worker
        # imports it — categorically worse than the read/write routes that are
        # also open in dev mode, so this one refuses rather than shrugging.
        if not auth_enabled() and os.environ.get("RYA_ALLOW_UNAUTHENTICATED_PUBLISH") != "1":
            raise HTTPException(status_code=403, detail={
                "code": "E_UNAUTHORIZED",
                "message": "Publishing is disabled while the control plane is unauthenticated.",
                "hint": "Set RYA_TOKEN (generate one with `rya token`), or set "
                        "RYA_ALLOW_UNAUTHENTICATED_PUBLISH=1 for a local-only loop."})

        claimed = (request.query_params.get("hash") or "").strip().lower()
        if not claimed:
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION", "message": "query param 'hash' is required.",
                "hint": "Send the hash from `rya bundle --json`; the platform rebuilds and compares it."})

        max_bytes = int(os.environ.get("RYA_MAX_BUNDLE_BYTES", str(20 * 1024 * 1024)))
        content = await request.body()
        if not content:
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION", "message": "request body is empty",
                "hint": "POST the bundle archive as the raw body with content-type application/gzip."})
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail={
                "code": "E_VALIDATION",
                "message": f"bundle exceeds {max_bytes} bytes",
                "hint": "Trim the project with a `.ryaignore`, or raise RYA_MAX_BUNDLE_BYTES."})

        meta = {k[5:]: v for k, v in request.query_params.items() if k.startswith("meta.")}
        env_name = (request.query_params.get("env") or "").strip() or None
        promote_it = (request.query_params.get("promote", "true").lower() != "false")
        actor = (request.query_params.get("actor") or "").strip() or _actor(authorization, x_rya_token)

        with tempfile.TemporaryDirectory(prefix="rya-publish-") as td:
            tmp = Path(td)
            archive = tmp / "upload.tar.gz"
            archive.write_bytes(content)
            unpacked = tmp / "tree"
            try:
                # Rebuild the hash from the bytes we received rather than trusting
                # the sidecar: D12's whole point is that the CONTENT proves the
                # version. This is `bundles.verify` inlined — it is unpack + build
                # + compare — so the tree is not extracted twice.
                bundles.unpack(archive, unpacked, max_total_bytes=max_bytes * 20)
                rebuilt = bundles.build_bundle(unpacked)
            except RyaError as e:
                raise _dep_err(e) from e

            if rebuilt.hash != claimed:
                raise _dep_err(_hash_mismatch(rebuilt, claimed, unpacked))

            # **Obsolete, not relaxed (D21).** This used to also require
            # `agent_id == manifest.name` — the deployment's single mounted
            # manifest — because a version filed under any other name "would be
            # listed by nothing and executed by nobody". That was true while one
            # deployment served one agent, and it is what made publishing a second
            # agent impossible. Now the version record IS the agent's existence, so
            # a name this process has never heard of is a new agent, not an error.
            #
            # What survives is the check that was always about the ARTIFACT: the
            # bundle's own manifest must agree with the path it is filed under, or
            # the content ends up in a namespace it does not claim and becomes
            # promotable into the wrong pointer.
            declared = str((rebuilt.manifest or {}).get("name") or "")
            if declared != agent_id:
                raise HTTPException(status_code=400, detail={
                    "code": "E_VALIDATION",
                    "message": f"Bundle declares agent '{declared or '?'}' but the path says "
                               f"'{agent_id}'.",
                    "hint": f"Publish to /agents/{declared}/versions, or change `name:` in "
                            f"rya.agent.yaml to '{agent_id}'."})

            try:
                # D20: publish into the caller's own tenant namespace, taken from
                # the request-scoped store rather than from configuration — in a
                # multi-tenant api one process serves every tenant, so an
                # ambient value would namespace them all identically.
                archive_store = bundles.resolve_bundle_store(
                    root, workspace=bundles.workspace_of(plane.store))
                stored = bundles.store_bundle(rebuilt, archive_store)
            except RyaError as e:
                raise _dep_err(e) from e

            try:
                version = _deployments().create_version(
                    plane.store, agent=agent_id, bundle=rebuilt, actor=actor,
                    metadata={**meta, "publishedVia": "http"})
            except RyaError as e:
                raise _dep_err(e) from e

        gate_result = None
        if env_name and promote_it:
            try:
                gate_result = gates.check_promotion(plane.store, version=version,
                                                   environment=env_name, actor=actor)
                _deployments().promote(plane.store, environment=env_name, agent=agent_id,
                                       version_id=version["id"], actor=actor)
            except RyaError as e:
                raise _dep_err(e) from e

        return {
            "ok": True, "agent": agent_id, "versionId": version["id"],
            "bundleHash": rebuilt.hash, "fileCount": rebuilt.fileCount,
            "sizeBytes": rebuilt.sizeBytes, "sdkVersion": rebuilt.sdkVersion,
            "lockfile": rebuilt.lockfile, "archive": str(stored),
            "environment": env_name, "promoted": bool(env_name and promote_it),
            **({"gate": gate_result.to_dict()} if gate_result else {}),
            # Stated, not implied: the one thing `rya deploy --env` does that this
            # path cannot.
            "attested": False, "notAttested": ["readiness"],
            "note": "Readiness was not evaluated: the control plane does not import bundles. "
                    "A gate requiring readiness will refuse this version.",
        }

    def _hash_mismatch(rebuilt, claimed: str, unpacked: Path) -> RyaError:
        """E_BUNDLE_MISMATCH, with the SDK-skew case named.

        `content_hash` folds the SDK version into the digest, so a client and a
        platform on different `rya` versions disagree about the hash of BYTE
        IDENTICAL trees. The generic "the artifact was modified" message would
        send an operator hunting for tampering that never happened.
        """
        base = (f"Bundle content hash {rebuilt.hash[:12]} does not match the "
                f"claimed {claimed[:12]}.")
        from ..bundles import BUNDLE_META_NAME

        try:
            sidecar = json.loads((unpacked / BUNDLE_META_NAME).read_text())
            client_sdk = str(sidecar.get("sdkVersion") or "")
        except (OSError, json.JSONDecodeError, ValueError):
            client_sdk = ""
        if client_sdk and client_sdk != rebuilt.sdkVersion:
            return RyaError(
                "E_BUNDLE_MISMATCH",
                f"{base} The client built it with rya SDK {client_sdk} and this "
                f"platform runs {rebuilt.sdkVersion}.",
                hint="The content hash includes the SDK version, so the same files hash "
                     "differently across SDK versions. Align them and re-publish.")
        return RyaError(
            "E_BUNDLE_MISMATCH", base,
            hint="The upload does not match the hash it claimed — re-run `rya bundle` and "
                 "publish again rather than editing an artifact in flight.")

    @api.get("/agents/{agent_id}/versions")
    def list_versions_ep(agent_id: str, state: Optional[str] = None,
                         plane: Plane = Depends(get_plane)):
        return {"versions": _deployments().list_versions(
            plane.store, agent=plane.agent(agent_id).name, state=state)}

    @api.get("/versions/{version_id}")
    def get_version_ep(version_id: str, plane: Plane = Depends(get_plane)):
        v = plane.store.version_get(version_id)
        if v is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        return v

    @api.get("/versions/{version_id}/pinned-runs")
    def pinned_runs_ep(version_id: str, plane: Plane = Depends(get_plane)):
        """Why a retire was refused: the runs still pinned to this version."""
        runs = _deployments().pinned_runs(plane.store, version_id)
        return {"runs": runs, "count": len(runs)}

    @api.get("/versions/{version_id}/runs")
    def version_runs_ep(version_id: str, limit: int = 50, plane: Plane = Depends(get_plane)):
        """Every run pinned to this version — terminal ones included.

        Deliberately NOT the same question as `/versions/{id}/pinned-runs`, which
        answers "what blocks a retire" and therefore excludes terminal runs by
        construction (`deployments.pinned_runs`, §6 "Version retirement"). The
        console's environment → version → runs drill-down (§11 item 12) needs the
        history too: "what ran on the hash that is on prod" is mostly finished
        runs, and `/console`'s run list is capped at 30 and carries no versionId.
        """
        version = plane.store.version_get(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        # D28 Rule 1: the version row names its agent, so nothing is addressed.
        rows = [r for r in plane.store.list_runs(version.get("agent"))
                if r.get("versionId") == version_id]
        rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        live = {r["id"] for r in _deployments().pinned_runs(plane.store, version_id)}
        runs = [{**_run_summary(r), "createdAt": r.get("createdAt"),
                 "environment": r.get("environment"), "agentVersion": r.get("agentVersion"),
                 # "pinned" in the retention sense: still holding the version alive.
                 "pinned": r["id"] in live}
                for r in rows[:max(1, min(int(limit or 50), 500))]]
        return {"runs": runs, "count": len(rows), "pinnedCount": len(live)}

    @api.post("/versions/{version_id}/retire")
    async def retire_version_ep(version_id: str, request: Request,
                                plane: Plane = Depends(get_plane)):
        body = await request.json() if await request.body() else {}
        try:
            return _deployments().retire(plane.store, version_id,
                                         force=bool(body.get("force")))
        except RyaError as e:
            raise _dep_err(e)

    @api.get("/agents/{agent_id}/environments")
    def list_environments_ep(agent_id: str, plane: Plane = Depends(get_plane)):
        return {"environments": _deployments().list_environments(
            plane.store, agent=plane.agent(agent_id).name)}

    @api.get("/agents/{agent_id}/environments/{env_name}")
    def describe_environment_ep(agent_id: str, env_name: str,
                                plane: Plane = Depends(get_plane)):
        try:
            return _deployments().describe_environment(plane.store, env_name,
                                                       plane.agent(agent_id).name)
        except RyaError as e:
            raise _dep_err(e)

    @api.post("/agents/{agent_id}/environments/{env_name}/promote")
    async def promote_ep(agent_id: str, env_name: str, request: Request,
                         plane: Plane = Depends(get_plane),
                         authorization: Optional[str] = Header(None),
                         x_rya_token: Optional[str] = Header(None)):
        """Flip the environment's current-version pointer (§9). Atomic: new runs
        go to the new version, in-flight runs finish on theirs."""
        body = await request.json()
        try:
            return _deployments().promote(plane.store, environment=env_name,
                                          agent=plane.agent(agent_id).name,
                                          version_id=body["versionId"],
                                          actor=_actor(authorization, x_rya_token),
                                          force=bool(body.get("force")))
        except KeyError:
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION", "message": "versionId is required."})
        except RyaError as e:
            raise _dep_err(e)

    @api.post("/agents/{agent_id}/environments/{env_name}/rollback")
    async def rollback_ep(agent_id: str, env_name: str, request: Request,
                          plane: Plane = Depends(get_plane),
                          authorization: Optional[str] = Header(None),
                          x_rya_token: Optional[str] = Header(None)):
        body = await request.json() if await request.body() else {}
        try:
            return _deployments().rollback(plane.store, environment=env_name,
                                           agent=plane.agent(agent_id).name,
                                           to_version_id=body.get("versionId"),
                                           actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)

    @api.get("/agents/{agent_id}/environments/{env_name}/history")
    def environment_history_ep(agent_id: str, env_name: str,
                               plane: Plane = Depends(get_plane)):
        return {"history": _deployments().history(plane.store, env_name,
                                                  plane.agent(agent_id).name)}

    # ---- promotion gates (§9) ---------------------------------------------
    # The readiness/eval gate as a server-side ADMISSION check. Config is
    # privileged platform state (D7), so these routes sit behind the same auth as
    # every other governance write and every change is audited.
    def _gates():
        from .. import gates as _g
        return _g

    def _gate_get(plane: Plane, ref: AgentRef, env: Optional[str]):
        g = _gates()
        names = [env] if env else sorted(
            {e["name"] for e in _deployments().list_environments(plane.store, agent=ref.name)}
            | set((g.gate_policy(plane.store, ref.name) or {}).get("environments") or {}))
        try:
            return {"agent": ref.name,
                    "gates": [g.resolve_gate(plane.store, n, agent=ref.name).describe()
                              for n in names],
                    "default": g.resolve_gate(plane.store, "default", agent=ref.name).describe()}
        except RyaError as e:
            raise _dep_err(e)

    @api.get("/agents/{agent_id}/gate")
    def get_gate_for(agent_id: str, env: Optional[str] = None,
                     plane: Plane = Depends(get_plane)):
        return _gate_get(plane, plane.agent(agent_id), env)

    @api.get("/gate")
    def get_gate_ep(response: Response, env: Optional[str] = None,
                    plane: Plane = Depends(get_plane)):
        return _gate_get(plane, _addressed(plane, None, response), env)

    async def _gate_put(request: Request, plane: Plane, ref: AgentRef,
                        authorization, x_rya_token):
        """Replace the agent's promotion gate policy. Validated on write so a
        mistyped requirement is rejected by the operator who typed it."""
        body = await request.json() if await request.body() else {}
        try:
            record = _gates().set_gate(plane.store, body or None, agent=ref.name,
                                       actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)
        return {"ok": True, "agent": ref.name, "version": record.get("version"),
                "policy": record.get("value")}

    @api.put("/agents/{agent_id}/gate")
    async def put_gate_for(agent_id: str, request: Request,
                           plane: Plane = Depends(get_plane),
                           authorization: Optional[str] = Header(None),
                           x_rya_token: Optional[str] = Header(None)):
        return await _gate_put(request, plane, plane.agent(agent_id),
                               authorization, x_rya_token)

    @api.put("/gate")
    async def put_gate_ep(request: Request, response: Response,
                          plane: Plane = Depends(get_plane),
                          authorization: Optional[str] = Header(None),
                          x_rya_token: Optional[str] = Header(None)):
        return await _gate_put(request, plane, _addressed(plane, None, response),
                               authorization, x_rya_token)

    def _gate_check(plane: Plane, ref: AgentRef, env: str, version_id: Optional[str],
                    authorization, x_rya_token):
        """Dry-run the gate — what a promotion would refuse, before attempting it."""
        try:
            if version_id:
                version = plane.store.version_get(version_id)
                if version is None:
                    raise RyaError("E_VERSION_NOT_FOUND", f"Version '{version_id}' not found.")
            else:
                version = _deployments().current_version(plane.store, env, ref.name)
                if version is None:
                    raise RyaError("E_ENVIRONMENT_NOT_FOUND",
                                   f"Nothing is promoted to '{env}' and no versionId was given.")
            # `check_promotion` derives the agent from the version row (Rule 1), so
            # a `?version_id=` belonging to another agent is evaluated against ITS
            # gate rather than this one's — which is the honest answer to "would
            # this version promote".
            result = _gates().check_promotion(plane.store, version=version, environment=env,
                                              actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)
        return {"versionId": version["id"], **result.to_dict()}

    @api.get("/agents/{agent_id}/gate/check")
    def check_gate_for(agent_id: str, env: str, version_id: Optional[str] = None,
                       plane: Plane = Depends(get_plane),
                       authorization: Optional[str] = Header(None),
                       x_rya_token: Optional[str] = Header(None)):
        return _gate_check(plane, plane.agent(agent_id), env, version_id,
                           authorization, x_rya_token)

    @api.get("/gate/check")
    def check_gate_ep(env: str, response: Response, version_id: Optional[str] = None,
                      plane: Plane = Depends(get_plane),
                      authorization: Optional[str] = Header(None),
                      x_rya_token: Optional[str] = Header(None)):
        return _gate_check(plane, _addressed(plane, None, response), env, version_id,
                           authorization, x_rya_token)

    @api.get("/versions/{version_id}/attestations")
    def version_attestations_ep(version_id: str, plane: Plane = Depends(get_plane)):
        """The evidence filed against a version: readiness, evals, overrides."""
        if plane.store.version_get(version_id) is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        rows = _gates().attestations(plane.store, version_id)
        return {"attestations": rows, "count": len(rows)}

    # ---- quotas (§11.12, D13) ---------------------------------------------
    def _quotas():
        from .. import quotas as _q
        return _q

    def _require_operator(authorization: Optional[str], x_rya_token: Optional[str]) -> None:
        """Quota WRITES need the operator, not the tenant.

        A limit the limited party can raise is not a limit. In multi-tenant mode a
        workspace key authenticates a tenant, so quota writes demand
        ``RYA_ADMIN_TOKEN`` — the same gate provisioning uses. In single-tenant
        self-hosting the operator and the tenant are the same person and the
        workspace's own auth is already the operator's auth.
        """
        if not mt:
            return
        admin = os.environ.get("RYA_ADMIN_TOKEN")
        if not admin:
            raise HTTPException(status_code=501, detail={
                "code": "E_UNAUTHORIZED",
                "message": "Quota changes are disabled: no RYA_ADMIN_TOKEN is configured.",
                "hint": "Set RYA_ADMIN_TOKEN on the api process, then send it as a bearer token."})
        provided = _bearer(authorization, x_rya_token)
        if not provided or not hmac.compare_digest(provided, admin):
            raise HTTPException(status_code=403, detail={
                "code": "E_UNAUTHORIZED",
                "message": "Changing a workspace quota requires the admin token.",
                "hint": "A tenant cannot raise its own quota — that is the point of a quota."})

    @api.get("/quotas")
    def get_quotas_ep(plane: Plane = Depends(get_plane)):
        """This workspace's limits and what it is currently consuming.

        Plus the **org rollup** (D29), when there is one. It comes from the derived
        policy row a privileged reconciler writes, not from a live cross-workspace
        query — see `rya.orgs`, and note that this route runs on the *tenant* plane
        and therefore holds no connection that could compute one. Absent when the
        reconciler has never run, which is honest: a missing rollup and an
        all-clear rollup are different states and an operator needs to tell them
        apart.
        """
        from .. import orgs as _orgs

        q = _quotas()
        try:
            policy = q.resolve_quota(plane.store)
            usage = q.usage_snapshot(plane.store)
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        out = {"quota": policy.describe(), "usage": usage,
               "admission": q.check_admission(plane.store, kind="any",
                                              usage=usage).to_dict()["violations"]}
        verdict = _orgs.read_verdict(plane.store)
        if verdict is not None:
            out["org"] = verdict
        return out

    @api.put("/quotas")
    async def put_quotas_ep(request: Request, plane: Plane = Depends(get_plane),
                            authorization: Optional[str] = Header(None),
                            x_rya_token: Optional[str] = Header(None)):
        _require_operator(authorization, x_rya_token)
        body = await request.json() if await request.body() else {}
        try:
            record = _quotas().set_quota(plane.store, body or None,
                                         actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        return {"ok": True, "version": record.get("version"), "quota": record.get("value")}

    @api.get("/workers")
    def list_workers_ep(status: Optional[str] = "alive", version_id: Optional[str] = None,
                        plane: Plane = Depends(get_plane)):
        """Which execution-plane processes are live, on which version (§6).

        `status` is derived, not stored: a worker that stopped heartbeating comes
        back `lost` rather than `alive` even though the row still says alive (see
        `store.worker_liveness`). `?status=` with no value means every status —
        which is what the console asks for, because a crashed worker vanishing
        from the list would read as scale-to-zero, the one thing §6 says this view
        must never confuse an outage with.
        """
        listing = getattr(plane.store, "worker_list", None)
        return {"workers": listing(version_id=version_id, status=status or None)
                if listing else []}

    @api.get("/posture")
    def posture_ep(verify: bool = False):
        """The launch gate, over HTTP: is this deployment untrusted-tenant safe?

        Platform-level rather than agent-scoped (D28's second category): the answer is a
        property of the deployment, not of an agent. Read-only and unauthenticated for
        the same reason ``/readiness`` is — an operator checking whether their own
        deployment is safe should not need a token to find out, and every field here is
        about *configuration*, never a credential's value.

        ``verify=true`` probes the substrate for real and costs a container or pod
        start, so it is opt-in.
        """
        from ..broker.inventory import take_inventory
        from ..execution.drivers import check_untrusted_posture, resolve_driver

        try:
            driver = resolve_driver()
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        report = check_untrusted_posture(driver, verify=verify)
        inventory = take_inventory()
        return {**report.describe(), "driver": driver.describe(),
                # `violations` names the KIND of credential, never the value — the
                # inventory is designed so this response is safe to expose.
                "credentials": {"clean": inventory.clean,
                                "violations": [f.describe() for f in inventory.violations]}}

    @api.get("/lifecycle")
    def lifecycle_ep(plane: Plane = Depends(get_plane)):
        """This workspace's deletion state (D31): active, disabled or purged.

        A read, and deliberately the only lifecycle route. `disable` and `purge` are
        **not** exposed over the api: a tenant must not be able to disable itself into a
        support ticket, and nothing should be able to trigger an irreversible purge over
        HTTP. Both are admin-plane CLI acts (`rya workspaces disable|purge`), which is
        the same reasoning that keeps quota *writes* behind the admin token — except
        stronger, because a purge has no undo to fall back on.
        """
        from ..purge import lifecycle

        return {"lifecycle": lifecycle(plane.store).describe()}

    @api.get("/usage")
    def usage_ep(since: Optional[str] = None, until: Optional[str] = None,
                 group_by: Optional[str] = None, plane: Plane = Depends(get_plane)):
        """Billable facts from the durable meter (D10) — not from run traces.

        Metered at ``workspace_id`` and rolled up at ``org_id``, which is D29's split
        rule stated as a payload: ``usage`` is this tenant's own consumption and is
        the authoritative record, and ``org`` is the shared budget it counts against.
        The rollup is a *sum of rows like these*, never a substitute for them — so a
        bill can always be traced back to the workspace that incurred it.
        """
        from .. import orgs as _orgs
        from ..observability.usage import workspace_usage
        try:
            out = {"usage": workspace_usage(plane.store, since=since, until=until,
                                            group_by=group_by)}
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        verdict = _orgs.read_verdict(plane.store)
        if verdict is not None:
            out["org"] = {"orgId": verdict.get("orgId"), "usage": verdict.get("usage"),
                          "budget": verdict.get("budget"),
                          "exhausted": verdict.get("exhausted"),
                          "workspaces": verdict.get("workspaces"),
                          "computedAt": verdict.get("computedAt")}
        return out

    def _models_of(ref: AgentRef):
        return {"agent": ref.name,
                "models": [{"id": m.id, "type": m.type, "permission": m.permission.value}
                           for m in ref.manifest.models]}

    def _channels_of(ref: AgentRef):
        return {"agent": ref.name,
                "channels": [c.model_dump() for c in ref.manifest.channels]}

    @api.get("/agents/{agent_id}/models")
    def models_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _models_of(plane.agent(agent_id))

    @api.get("/models")
    def models(response: Response, plane: Plane = Depends(get_plane)):
        return _models_of(_addressed(plane, None, response))

    @api.get("/agents/{agent_id}/channels")
    def channels_for(agent_id: str, plane: Plane = Depends(get_plane)):
        return _channels_of(plane.agent(agent_id))

    @api.get("/channels")
    def channels(response: Response, plane: Plane = Depends(get_plane)):
        return _channels_of(_addressed(plane, None, response))

    @api.get("/v1/info")
    def cloud_info(request: Request):
        # Discovery: how an agent connects to this (possibly hosted) instance.
        base = str(request.base_url).rstrip("/")
        return {
            "service": "rya", "version": RYA_VERSION,
            "multiTenant": mt, "authRequired": auth_enabled(),
            "remoteMcp": f"{base}/mcp" if mcp_asgi is not None else None,
            "api": base, "console": f"{base}/",
            "webhook": f"{base}/inbound", "websocket": base.replace("http", "ws", 1) + "/ws",
            "provisionProjects": mt,  # self-serve project creation available in multi-tenant mode
        }

    @api.post("/v1/projects")
    async def create_project(request: Request, authorization: Optional[str] = Header(None),
                             x_rya_token: Optional[str] = Header(None)):
        # Self-serve project provisioning (the `npx login`/create-project equivalent):
        # mint a new workspace + API key against a hosted instance. Multi-tenant only,
        # and gated by RYA_ADMIN_TOKEN so signup isn't open to the world.
        if not mt:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                "message": "Project provisioning requires multi-tenant mode (RYA_MULTITENANT=1 + Postgres)."})
        admin = os.environ.get("RYA_ADMIN_TOKEN")
        if not admin:
            # Fail CLOSED: never allow open self-serve provisioning by accident.
            raise HTTPException(status_code=403, detail={"code": "E_PROVISIONING_DISABLED",
                "message": "Self-serve project provisioning is disabled.",
                "hint": "Set RYA_ADMIN_TOKEN to enable it, then send it as a bearer token."})
        provided = _bearer(authorization, x_rya_token)
        if not provided or not hmac.compare_digest(provided, admin):
            raise HTTPException(status_code=401, detail={"code": "E_UNAUTHORIZED",
                "message": "Project provisioning requires the admin token (RYA_ADMIN_TOKEN)."})
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        name = (body or {}).get("name") or "project"
        ws = tenancy.create_workspace(name)
        key = tenancy.create_api_key(ws["id"], label=(body or {}).get("label", "default"))
        base = str(request.base_url).rstrip("/")
        return {"ok": True, "workspaceId": ws["id"], "name": ws["name"],
                "apiKey": key["key"],  # shown ONCE — only the hash is stored
                "remoteMcp": f"{base}/mcp" if mcp_asgi is not None else None, "api": base}

    # ---- self-serve onboarding (sign up → workspace → key) ----------------
    def _require_mt():
        if not mt:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                "message": "Onboarding/accounts require multi-tenant mode (RYA_MULTITENANT=1 + Postgres)."})

    def _session(authorization: Optional[str], x_rya_session: Optional[str]):
        from ..accounts import verify_session
        tok = x_rya_session or _bearer(authorization, None)
        payload = verify_session(tok)
        if not payload:
            raise HTTPException(status_code=401, detail={"code": "E_UNAUTHORIZED",
                "message": "Sign in first.", "hint": "POST /v1/login to get a session token."})
        return payload

    @api.post("/v1/signup")
    async def signup(request: Request):
        from ..accounts import issue_session
        _require_mt()
        body = await request.json()
        try:
            res = tenancy.signup(body.get("email", ""), body.get("password", ""),
                                 body.get("workspaceName") or "My workspace")
        except RyaError as e:
            code = 409 if e.code == "E_EMAIL_TAKEN" else 400
            raise HTTPException(status_code=code, detail=e.to_dict()["error"])
        token = issue_session(res["user"]["id"], res["user"]["email"])
        base = str(request.base_url).rstrip("/")
        return {"ok": True, "token": token, "user": res["user"], "workspace": res["workspace"],
                "apiKey": res["apiKey"],  # shown ONCE — save it
                "remoteMcp": f"{base}/mcp" if mcp_asgi is not None else None, "api": base}

    @api.post("/v1/login")
    async def login_account(request: Request):
        from ..accounts import issue_session
        _require_mt()
        body = await request.json()
        user = tenancy.authenticate(body.get("email", ""), body.get("password", ""))
        if not user:
            raise HTTPException(status_code=401, detail={"code": "E_UNAUTHORIZED",
                "message": "Invalid email or password."})
        token = issue_session(user["id"], user["email"])
        # Claim any invites sent to this email before the account existed.
        tenancy.claim_invites(user["email"], user["id"])
        return {"ok": True, "token": token, "user": user,
                "workspaces": tenancy.list_user_workspaces(user["id"])}

    @api.post("/v1/token")
    def mint_user_token(authorization: Optional[str] = Header(None),
                        x_rya_session: Optional[str] = Header(None)):
        """The session-to-JWT bridge: exchange a valid session for a short-lived
        HS256 user JWT the data plane verifies (X-Rya-User-Token), so every run
        and approval records WHO acted."""
        from ..auth import issue_jwt
        _require_mt()
        s = _session(authorization, x_rya_session)
        try:
            token = issue_jwt(s["sub"], email=s.get("email"))
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"userToken": token, "expiresInSeconds": 12 * 3600}

    @api.get("/v1/me")
    def whoami_account(authorization: Optional[str] = Header(None),
                       x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        return {"user": {"id": s["sub"], "email": s["email"]},
                "workspaces": tenancy.list_user_workspaces(s["sub"])}

    @api.post("/v1/workspaces")
    async def create_user_workspace(request: Request, authorization: Optional[str] = Header(None),
                                    x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        body = await request.json()
        ws = tenancy.create_workspace(body.get("name") or "Workspace", owner_user_id=s["sub"])
        key = tenancy.create_api_key(ws["id"], label=body.get("label", "default"))
        return {"ok": True, "workspace": ws, "apiKey": key["key"]}

    # ---- team access: invites + per-workspace keys ------------------------
    def _require_access(ws_id: str, user_id: str, need_owner: bool = False) -> str:
        role = tenancy.workspace_access(ws_id, user_id)
        if role is None or (need_owner and role != "owner"):
            raise HTTPException(status_code=403, detail={
                "code": "E_UNAUTHORIZED",
                "message": "You are not " + ("the owner of" if need_owner else "a member of") + " this workspace."})
        return role

    @api.post("/v1/workspaces/{ws_id}/members")
    async def invite_workspace_member(ws_id: str, request: Request,
                                      authorization: Optional[str] = Header(None),
                                      x_rya_session: Optional[str] = Header(None)):
        """Owner invites a teammate by email. If the account exists, access is
        immediate; otherwise the invite is claimed automatically at signup."""
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"], need_owner=True)
        body = await request.json()
        try:
            m = tenancy.invite_member(ws_id, body.get("email"), invited_by=s["sub"])
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        return {"ok": True, **m}

    @api.get("/v1/workspaces/{ws_id}/members")
    def list_workspace_members(ws_id: str, authorization: Optional[str] = Header(None),
                               x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"])
        return {"members": tenancy.list_members(ws_id)}

    @api.post("/v1/workspaces/{ws_id}/keys")
    async def mint_workspace_key(ws_id: str, request: Request,
                                 authorization: Optional[str] = Header(None),
                                 x_rya_session: Optional[str] = Header(None)):
        """Mint an API key for an EXISTING workspace the caller can access
        (owner or member) - this is how an invited teammate opens the shared
        workspace in the console."""
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"])
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        key = tenancy.create_api_key(ws_id, label=(body or {}).get("label") or s["email"],
                                     created_by=s["sub"])
        ws = next((w for w in tenancy.list_user_workspaces(s["sub"]) if w["id"] == ws_id), {"id": ws_id})
        return {"ok": True, "workspace": ws, "apiKey": key["key"]}

    @api.get("/v1/workspaces/{ws_id}/keys")
    def list_workspace_keys(ws_id: str, authorization: Optional[str] = Header(None),
                            x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"], need_owner=True)
        return {"keys": tenancy.list_keys(ws_id)}

    @api.delete("/v1/workspaces/{ws_id}/keys/{key_id}")
    def revoke_workspace_key(ws_id: str, key_id: str, authorization: Optional[str] = Header(None),
                             x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"], need_owner=True)
        return {"ok": tenancy.revoke_key(ws_id, key_id)}

    @api.delete("/v1/workspaces/{ws_id}/members/{email}")
    def remove_workspace_member(ws_id: str, email: str, authorization: Optional[str] = Header(None),
                                x_rya_session: Optional[str] = Header(None)):
        """Owner removes a member; every key that member minted for this
        workspace is revoked with them."""
        _require_mt()
        s = _session(authorization, x_rya_session)
        _require_access(ws_id, s["sub"], need_owner=True)
        return {"ok": True, **tenancy.remove_member(ws_id, email)}

    @api.post("/v1/password")
    async def change_password(request: Request, authorization: Optional[str] = Header(None),
                              x_rya_session: Optional[str] = Header(None)):
        _require_mt()
        s = _session(authorization, x_rya_session)
        body = await request.json()
        try:
            tenancy.change_password(s["sub"], body.get("current") or "", body.get("new") or "")
        except RyaError as e:
            code = 401 if e.code == "E_UNAUTHORIZED" else 400
            raise HTTPException(status_code=code, detail=e.to_dict()["error"])
        return {"ok": True}

    # Mount remote MCP last so its catch-all under /mcp doesn't shadow API routes.
    if mcp_asgi is not None:
        api.mount("/mcp", mcp_asgi)

    return api
