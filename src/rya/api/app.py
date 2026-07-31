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
from pathlib import Path
from typing import Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse

from .. import __version__ as RYA_VERSION
from ..config import current_environment
from ..errors import RyaError

_STARTED_AT = time.time()


def build_infra(manifest, store) -> dict:
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
        "observability": {"traces": manifest.observability.traces, "export": trace_export()},
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


def _load_console_asset(name: str) -> str:
    import importlib.resources as ir
    return ir.files("rya").joinpath(f"console/{name}").read_text(encoding="utf-8")


def _load_console_html() -> str:
    try:
        return _load_console_asset("index.html")
    except Exception:  # pragma: no cover - fallback if asset missing
        return "<!doctype html><title>Rya</title><h1>Rya runtime</h1><p>Console asset not bundled.</p>"


def _load_lucide() -> str:
    try:
        return _load_console_asset("lucide.min.js")
    except Exception:  # pragma: no cover
        return "window.lucide={createIcons:function(){}};"  # graceful no-op


_CONSOLE_HTML = _load_console_html()
_LUCIDE_JS = _load_lucide()

# Content-Security-Policy for the console. Scripts/styles are 'self' + 'unsafe-inline'
# (the SPA is a single inline-script file); the icon library is now self-hosted so
# no third-party script origin is allowed. Fonts stay on Google's CDN (non-exec).
_CONSOLE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
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
from ..runtime import Engine, load_agent
from ..store import open_store


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


def multitenant_enabled() -> bool:
    has_pg = bool(os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    return os.environ.get("RYA_MULTITENANT") == "1" and has_pg


def build_app(root: Path) -> FastAPI:
    root = Path(root)
    manifest = load_manifest(root / "rya.agent.yaml")
    agent = load_agent(manifest, root)
    mt = multitenant_enabled()

    # Single-tenant: one engine on the configured store. Multi-tenant: per-request
    # engine scoped to the caller's workspace, on the RLS-enforced rya_app role.
    if mt:
        from ..tenancy import Tenancy
        from ..store_postgres import PostgresStore

        admin_dsn = os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL")
        tenancy = Tenancy(admin_dsn)
        app_data_dsn = tenancy.setup()  # idempotent: tables + rya_app + RLS

        def engine_for(workspace_id: str, user_id: Optional[str] = None) -> Engine:
            # user_id (from a verified per-user JWT) drives the app.user_id GUC, so
            # Postgres per-user RLS isolates users WITHIN a workspace, not just by
            # workspace. None = shared/workspace-level (backward compatible).
            return Engine(manifest, agent, PostgresStore(app_data_dsn, workspace_id, user_id), root)

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
        base_engine = Engine(manifest, agent, base_store, root)

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

    async def get_engine(authorization: Optional[str] = Header(None),
                         x_rya_token: Optional[str] = Header(None),
                         x_rya_user_token: Optional[str] = Header(None)) -> Engine:
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
            return engine_for(ws, user_id)
        if jwt_configured():
            _identity_from(authorization, x_rya_token, required=True)  # enforce JWT
        else:
            _check_token(authorization, x_rya_token)
        return base_engine

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

    # ---- global turn-reclaim sweeper (the built-in cron) ------------------
    # A crashed chat turn is reclaimable, but reclaim only happens when someone
    # runs it. In single-tenant mode this background loop is that someone; in
    # multi-tenant mode `rya worker` is, and this loop never starts.
    def _sweep_once() -> int:
        from .. import turns as _t
        total = 0
        if mt:
            for ws in tenancy.list_workspaces():
                eng = engine_for(ws["id"])
                try:
                    total += len(_t.execute_pending(eng, worker_id="sweeper", limit=20))
                finally:
                    if hasattr(eng.store, "close"):
                        eng.store.close()
        else:
            total += len(_t.execute_pending(base_engine, worker_id="sweeper", limit=20))
        return total

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
        total = 0
        if mt:
            for ws in tenancy.list_workspaces():
                eng = engine_for(ws["id"])
                try:
                    total += len(eng.work_once(concurrency=_jobs_conc))
                finally:
                    if hasattr(eng.store, "close"):
                        eng.store.close()
        else:
            total += len(base_engine.work_once(concurrency=_jobs_conc))
        return total

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

    api = FastAPI(title="Rya Control Plane", version=manifest.version, lifespan=_lifespan)

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

    @api.get("/", response_class=HTMLResponse)
    @api.get("/console.html", response_class=HTMLResponse)
    def console_page():
        # The console page itself is public (it loads, then authenticates its own
        # data calls). `rya serve` ships the dashboard at its own origin.
        return HTMLResponse(_CONSOLE_HTML, headers=_CONSOLE_HEADERS)

    @api.get("/lucide.min.js")
    def lucide_asset():
        # Self-hosted icon library — no third-party CDN dependency (works
        # air-gapped, and lets the CSP keep script-src to 'self').
        from fastapi.responses import Response
        return Response(_LUCIDE_JS, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})

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
    def console_state(engine: Engine = Depends(get_engine)):
        # Rich aggregate for the web console — auth-gated like the control routes.
        # Works in BOTH modes: the dependency-injected engine is workspace-scoped
        # in multi-tenant mode, so the console shows only the caller's data.
        from ..snapshot import build_console
        store = engine.store
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
        viewer = {"workspace": ws_name, "workspaceId": ws_id,
                  "mode": "multi-tenant" if mt else "single-tenant",
                  "user": (manifest.owner if not mt else None)}
        return {**build_console(manifest, store, agent, root),
                "infra": build_infra(manifest, store), "viewer": viewer}

    # ---- durable turns: the ONE streaming path (D6) -----------------------
    # `on_token`/`on_trace`/`on_ui` used to be raw Python closures handed into
    # the engine and on into the provider's SSE parser. That only works while the
    # process holding the browser socket is the process executing the handler —
    # which the api/worker split ends. Every stream now goes through the durable
    # turn buffer: the executor APPENDS frames, the endpoint TAILS them by seq.
    # A dropped client resumes with Last-Event-ID; a crashed executor's reclaim
    # just appends more frames.
    from .. import turns as _turns

    def _fresh_engine_for(authorization, x_rya_token) -> Engine:
        """A standalone engine for background turn execution - never shares the
        request engine's connection across the response boundary."""
        if mt:
            return engine_for(authorize(authorization, x_rya_token))
        return base_engine

    def _kick_turn(authorization, x_rya_token) -> None:
        """Run a due turn in this process — ONLY where that is allowed (§11.7).
        When it is not, the turn sits on the queue with a lease and `rya worker`
        claims it; latency differs, durability does not."""
        if not _inline_worker_enabled():
            return
        try:
            _turns.execute_pending(_fresh_engine_for(authorization, x_rya_token),
                                   worker_id="inline", limit=1)
        except Exception:  # reclaim will re-drive it; never fail the request
            import logging
            logging.getLogger("rya.turns").warning("inline turn execution failed", exc_info=True)

    def _guard_source(engine: Engine):
        """Where this workspace's guard policy comes from: the governed store
        when it supports policy storage (workspace-scoped, versioned, audited),
        else the project file for `rya dev`."""
        from ..guard import GUARD_FILE
        store = engine.store
        if hasattr(store, "policy_get"):
            try:
                if store.policy_get("guard") is not None:
                    return store
            except Exception:
                return store  # a read failure must fail closed, not fall back
        return str(root / GUARD_FILE)

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

    async def _tail_turn(engine: Engine, turn_id: str, after: int = -1,
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
            frames = await asyncio.to_thread(engine.store.stream_read, turn_id, cursor)
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
        (single-tenant) or the ``rya_sk_…`` API key (multi-tenant).
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
            engine = engine_for(ws_id)
        else:
            need = os.environ.get("RYA_TOKEN")
            if need and (not token or not hmac.compare_digest(token, need)):
                await websocket.send_json({"type": "error", "code": "E_UNAUTHORIZED",
                                           "message": "operator token required (?token=…)."})
                await websocket.close(code=4401)
                return
            engine = base_engine

        await websocket.send_json({"type": "ready", "agent": manifest.name,
                                   "version": manifest.version})
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
                run = engine.store.get_run(msg.get("runId", ""))
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
                    started = _turns.create_turn(engine, event_type, payload)
                except RyaError as e:
                    await websocket.send_json({"type": "error", **e.to_dict()["error"]})
                    continue
                turn_id = started["turnId"]
                # Give the client the handle immediately so it can reconnect to
                # GET /agents/{id}/turns/{turnId}/stream if the socket drops.
                await websocket.send_json({"type": "turn", "turnId": turn_id})
                # Concurrent with the tail, so frames reach the socket as they
                # are appended rather than in one burst at the end.
                kick = asyncio.create_task(asyncio.to_thread(_kick_turn, token, token))
                try:
                    async for f in _tail_turn(engine, turn_id, stop_on_pause=True):
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
                                engine: Engine = Depends(get_engine),
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

        started = _turns.create_turn(engine, event_type, payload, identity=identity)
        turn_id = started["turnId"]

        async def sse():
            # The kick must run CONCURRENTLY with the tail, not before it and not
            # as a response background task: a StreamingResponse's background
            # tasks fire only after the body is fully sent, so scheduling it there
            # deadlocks the stream against the turn it is waiting for.
            kick = asyncio.create_task(asyncio.to_thread(_kick_turn, authorization, x_rya_token))
            try:
                yield f"event: turn\ndata: {json.dumps({'turnId': turn_id})}\n\n"
                async for f in _tail_turn(engine, turn_id, after=after,
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
                             engine: Engine = Depends(get_engine),
                             authorization: Optional[str] = Header(None),
                             x_rya_token: Optional[str] = Header(None)):
        """Start a DURABLE chat turn. Returns ``{turnId}`` immediately; the turn
        runs on a worker (kicked inline here, reclaimed on crash) and streams via
        GET /agents/{id}/turns/{turnId}/stream."""
        body = await request.json()
        identity = _identity_from(authorization, x_rya_token, required=False)
        res = _turns.create_turn(engine, body.get("type", "message.received"),
                                 body.get("payload", {}), identity=identity)
        background.add_task(_kick_turn, authorization, x_rya_token)
        return res

    @api.get("/agents/{agent_id}/turns/{turn_id}/stream")
    async def turn_stream_ep(agent_id: str, turn_id: str, request: Request,
                             after: int = -1, engine: Engine = Depends(get_engine)):
        """Tail a turn's durable stream as SSE. Resumable: reconnect with
        ``?after=<lastSeq>`` (or the browser's Last-Event-ID header) to continue
        exactly where the dropped connection left off. Ends on the terminal
        run/error frame."""
        from fastapi.responses import StreamingResponse

        last_id = request.headers.get("last-event-id")
        start = int(last_id) if (last_id and last_id.lstrip("-").isdigit()) else after

        async def sse():
            async for f in _tail_turn(engine, turn_id, after=start,
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
    def reclaim_turns_ep(agent_id: str, engine: Engine = Depends(get_engine)):
        """Reclaim + run any pending or crashed (lease-expired) chat turns for
        this workspace. The durability backstop - call periodically (cron / a
        `rya` worker loop) so an interrupted turn always finishes."""
        ran = _turns.execute_pending(engine, worker_id="reclaimer", limit=50)
        return {"reclaimed": ran, "count": len(ran)}

    @api.get("/guard")
    def get_guard(engine: Engine = Depends(get_engine)):
        from ..guard import resolve_policy, run_tests
        gp = resolve_policy(_guard_source(engine))
        return {"policy": gp.policy, "tests": run_tests(gp), "exists": gp.enforced,
                **gp.describe()}

    @api.put("/guard")
    async def put_guard(request: Request, engine: Engine = Depends(get_engine),
                        authorization: Optional[str] = Header(None),
                        x_rya_token: Optional[str] = Header(None)):
        """Write the guard policy. Versioned, diffed and attributed to a
        principal — §12 risk 7: for a governance product, "who reviewed this
        allowlist change" is a feature, not a residual."""
        from ..guard import save_policy, run_tests
        body = await request.json()
        policy = body.get("policy", body)
        record = save_policy(policy, source=_guard_source(engine),
                             actor=_actor(authorization, x_rya_token))
        return {"ok": True, "tests": run_tests(policy), "version": record.get("version"),
                "record": record}

    @api.get("/guard/log")
    def guard_log(limit: int = 50, engine: Engine = Depends(get_engine)):
        """The policy audit trail: every change, its diff, and who made it."""
        from ..guard import POLICY_KEY
        history = getattr(engine.store, "policy_history", None)
        return {"entries": history(POLICY_KEY, limit) if history else []}

    @api.post("/guard/test")
    def test_guard(engine: Engine = Depends(get_engine)):
        from ..guard import run_tests
        return run_tests(_guard_source(engine))

    @api.get("/evals")
    def get_evals(engine: Engine = Depends(get_engine)):
        # List the declarative cases (no run — runs are fired on demand below).
        from ..evals import load_evals, EVALS_FILE
        cases = load_evals(root)
        return {"cases": cases, "exists": (root / EVALS_FILE).is_file()}

    @api.post("/evals/run")
    def post_evals_run(engine: Engine = Depends(get_engine)):
        from ..evals import run_evals
        if mt:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION",
                                "message": "Evals are single-tenant only."})
        return run_evals(manifest, agent, base_store, root)

    @api.get("/healthz")
    def healthz():
        # Report the active store backend so a deploy can confirm it's on Postgres.
        if mt:
            backend = "postgres"
        else:
            backend = base_store.describe().get("backend")
        return {"ok": True, "agent": manifest.name, "authEnabled": auth_enabled(),
                "multiTenant": mt, "store": backend}

    @api.post("/inbound")
    async def inbound(request: Request, authorization: Optional[str] = Header(None),
                      x_rya_token: Optional[str] = Header(None)):
        raw = await request.body()
        _verify_signature(raw, request.headers.get("x-rya-signature"))
        # Single-tenant: webhooks are gated by SIGNATURE only (third-party senders
        # hold the signing secret, not the operator token). Multi-tenant: the API
        # key is required to identify which workspace the event belongs to.
        engine = engine_for(authorize(authorization, x_rya_token)) if mt else base_engine
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail={"code": "E_VALIDATION", "message": "Body is not valid JSON."})
        event_type = request.headers.get("x-rya-event-type") or (
            body.get("type") if isinstance(body, dict) else None) or "webhook.received"
        payload = body if isinstance(body, dict) else {"data": body}
        try:
            run = engine.run_event(event_type, payload, source="webhook")
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        return {"runId": run["id"], "status": run["status"], "pendingApproval": run.get("pendingApproval")}

    @api.post("/slack/events")
    async def slack_events(request: Request):
        """Real Slack inbound adapter: verify Slack's signature, answer the
        url_verification handshake, and turn event_callbacks into agent runs."""
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
            run = base_engine.run_event(f"slack.{ev.get('type', 'message')}", ev, source="slack")
            return {"ok": True, "runId": run["id"]}
        return {"ok": True}

    @api.get("/agents/{agent_id}")
    def get_agent(agent_id: str, engine: Engine = Depends(get_engine)):
        return manifest.model_dump(mode="json")

    @api.post("/agents/{agent_id}/events")
    async def post_event(agent_id: str, request: Request, engine: Engine = Depends(get_engine),
                         authorization: Optional[str] = Header(None), x_rya_token: Optional[str] = Header(None)):
        body = await request.json()
        identity = _identity_from(authorization, x_rya_token, required=False)
        run = engine.run_event(body.get("type", "message.received"),
                               body.get("payload", {}), body.get("source", "api"), identity=identity)
        return {"runId": run["id"], "status": run["status"], "pendingApproval": run.get("pendingApproval"),
                "identity": identity.to_dict() if identity else None}

    @api.get("/agents/{agent_id}/runs")
    def list_runs(agent_id: str, engine: Engine = Depends(get_engine)):
        return {"runs": engine.store.list_runs(manifest.name)}

    @api.post("/runs/ingest")
    async def ingest_run(request: Request, engine: Engine = Depends(get_engine)):
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
            "id": engine.store.new_run_id(),
            "agent": manifest.name,
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
        engine.store.save_run(run)
        # Onward export (Langfuse/OTLP/webhook) like any finished run - best effort.
        if status in ("completed", "failed", "rejected"):
            try:
                from ..observability.export import export_run
                from ..sdk.context import load_env
                export_run(run, load_env(engine.project_root))
            except Exception:
                pass
        return {"ok": True, "runId": run["id"], "events": len(trace)}

    # ---- files (uploaded documents) -------------------------------------
    @api.post("/files")
    async def upload_file(request: Request, engine: Engine = Depends(get_engine)):
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
        meta = engine.store.save_file(name, content,
                                      content_type=request.headers.get("content-type"),
                                      tags=tags)
        out = {"ok": True, "file": meta}
        if request.query_params.get("event", "true").lower() != "false":
            run = engine.run_event("file.uploaded",
                                   {"fileId": meta["id"], "name": meta["name"],
                                    "tags": meta["tags"], "size": meta["size"],
                                    "contentType": meta["contentType"]},
                                   source="upload")
            out["runId"] = run["id"]
            out["runStatus"] = run["status"]
        return out

    @api.post("/files/presign")
    async def presign_file(request: Request, engine: Engine = Depends(get_engine)):
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
        meta = engine.store.save_file(name, b"", content_type=ctype,
                                      tags={**(body.get("tags") or {}), "_storage": "s3",
                                            "_pending": "1"})
        return {"ok": True, "fileId": meta["id"],
                "uploadUrl": files_s3.presign_put(meta["id"], ctype)}

    @api.post("/files/{file_id}/confirm")
    def confirm_file(file_id: str, engine: Engine = Depends(get_engine)):
        """After a presigned PUT: verify the object landed, fire file.uploaded."""
        from .. import files_s3
        meta = engine.store.get_file(file_id)
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
        run = engine.run_event("file.uploaded",
                               {"fileId": file_id, "name": meta["name"], "tags": tags,
                                "size": h["size"], "contentType": h.get("contentType")},
                               source="upload")
        return {"ok": True, "runId": run["id"], "runStatus": run["status"], "size": h["size"]}

    @api.get("/files")
    def list_files(engine: Engine = Depends(get_engine)):
        return {"files": engine.store.list_files()}

    @api.get("/files/{file_id}")
    def get_file(file_id: str, engine: Engine = Depends(get_engine)):
        meta = engine.store.get_file(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_NOT_FOUND", "message": f"No file '{file_id}'.",
                "hint": "List what exists with GET /files."})
        return meta

    @api.get("/runs/{run_id}")
    def get_run(run_id: str, engine: Engine = Depends(get_engine)):
        run = engine.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "E_RUN_NOT_FOUND"})
        return run

    @api.get("/runs/{run_id}/trace")
    def get_trace(run_id: str, engine: Engine = Depends(get_engine)):
        run = engine.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "E_RUN_NOT_FOUND"})
        return {"runId": run_id, "trace": run["trace"]}

    @api.get("/connections")
    def list_connections(engine: Engine = Depends(get_engine)):
        # Metadata only — the store never returns secret values.
        return {"connections": engine.store.list_connections()}

    @api.get("/knowledge")
    def knowledge(engine: Engine = Depends(get_engine)):
        km = engine.store.load_memory("knowledge")
        return {"documents": km.get("documents", []),
                "chunks": len(km.get("collections", {}).get("chunks", []))}

    @api.post("/knowledge/search")
    async def knowledge_search(request: Request, engine: Engine = Depends(get_engine)):
        from ..providers.embeddings import cosine, embed
        import re
        body = await request.json()
        query = (body or {}).get("query", "")
        km = engine.store.load_memory("knowledge")
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

    @api.get("/sessions")
    def list_sessions(engine: Engine = Depends(get_engine)):
        return {"sessions": engine.store.list_sessions(manifest.name)}

    @api.get("/sessions/find")
    def find_session(channel: str, externalId: str, engine: Engine = Depends(get_engine)):
        """Resolve the session for a (channel, externalId) identity - how a chat
        client locates its own thread after a reload without listing everything."""
        session = engine.store.find_session(manifest.name, channel, externalId)
        if session is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND",
                "message": f"No session for {channel}/{externalId}.",
                "hint": "A session is created by the first turn on that identity — send one, "
                        "then resolve it."})
        return session

    @api.get("/sessions/{session_id}")
    def get_session(session_id: str, engine: Engine = Depends(get_engine)):
        session = engine.store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND", "message": f"No session '{session_id}'.",
                "hint": "Resolve it by identity with GET /sessions/find?channel=…&externalId=…"})
        return session

    @api.get("/sessions/{session_id}/messages")
    def session_messages(session_id: str, limit: int = 200,
                         engine: Engine = Depends(get_engine)):
        """The durable transcript, oldest first - restored on client reload
        (render instantly; never replay a typewriter)."""
        if engine.store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_SESSION_NOT_FOUND", "message": f"No session '{session_id}'.",
                "hint": "An empty transcript and an unknown session are different answers; "
                        "this one is unknown."})
        msgs = engine.store.list_messages(session_id)
        return {"messages": msgs[-limit:]}

    @api.get("/approvals")
    def list_approvals(status: Optional[str] = None, engine: Engine = Depends(get_engine)):
        return {"approvals": engine.store.list_approvals(status)}

    @api.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, engine: Engine = Depends(get_engine),
                authorization: Optional[str] = Header(None),
                x_rya_token: Optional[str] = Header(None),
                x_rya_user_token: Optional[str] = Header(None)):
        # Turn-bound runs stream their post-approval continuation onto the
        # original turn's durable buffer (resolve_on_stream); plain runs approve
        # exactly as before. The resolved actor is recorded on the approval.
        actor = _actor_from(authorization, x_rya_token, x_rya_user_token)
        try:
            run = _turns.resolve_on_stream(engine, approval_id, approve=True, actor=actor)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"],
                "turnId": run.get("turnId"),
                "resolvedBy": actor}

    @api.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, engine: Engine = Depends(get_engine),
               authorization: Optional[str] = Header(None),
               x_rya_token: Optional[str] = Header(None),
               x_rya_user_token: Optional[str] = Header(None)):
        actor = _actor_from(authorization, x_rya_token, x_rya_user_token)
        try:
            run = _turns.resolve_on_stream(engine, approval_id, approve=False, actor=actor)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"],
                "turnId": run.get("turnId"),
                "resolvedBy": actor}

    # ---- queue: durable jobs for external workers (Sim et al.) -----------
    from .. import queue as q

    _Q_HTTP = {"E_JOB_NOT_FOUND": 404, "E_QUEUE_CONFLICT": 409, "E_VALIDATION": 400}

    def _q_err(e: RyaError) -> HTTPException:
        return HTTPException(status_code=_Q_HTTP.get(e.code, 400), detail=e.to_dict()["error"])

    @api.post("/queue/jobs")
    async def queue_enqueue(request: Request, engine: Engine = Depends(get_engine)):
        body = await request.json()
        try:
            job = q.enqueue(
                engine.store, body.get("type"), body.get("payload"),
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
    async def queue_enqueue_batch(request: Request, engine: Engine = Depends(get_engine)):
        body = await request.json()
        try:
            jobs = q.enqueue_batch(engine.store, body.get("type"), body.get("items") or [])
        except RyaError as e:
            raise _q_err(e)
        return {"jobs": jobs, "ids": [j["id"] for j in jobs]}

    @api.post("/queue/claim")
    async def queue_claim(request: Request, engine: Engine = Depends(get_engine)):
        """Claim due jobs for an external worker. Optional short long-poll via
        waitSeconds (capped) so idle workers don't hammer the API."""
        import asyncio
        body = await request.json()
        worker_id = body.get("workerId")
        wait = min(float(body.get("waitSeconds") or 0), 25.0)
        deadline = time.monotonic() + wait

        def _claim():
            return q.claim(engine.store, worker_id, types=body.get("types"),
                           limit=body.get("limit", 1),
                           lease_seconds=body.get("leaseSeconds", q.DEFAULT_LEASE_SECONDS))
        try:
            jobs = await asyncio.to_thread(_claim)
            while not jobs and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                jobs = await asyncio.to_thread(_claim)
        except RyaError as e:
            raise _q_err(e)
        return {"jobs": jobs}

    @api.get("/queue/jobs/{job_id}")
    def queue_get_job(job_id: str, engine: Engine = Depends(get_engine)):
        job = engine.store.queue_get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "E_JOB_NOT_FOUND"})
        return {"job": job}

    @api.get("/queue/jobs")
    def queue_list_jobs(status: Optional[str] = None, type: Optional[str] = None,
                        engine: Engine = Depends(get_engine)):
        return {"jobs": engine.store.queue_list(status, type)}

    @api.post("/queue/jobs/{job_id}/heartbeat")
    async def queue_heartbeat(job_id: str, request: Request, engine: Engine = Depends(get_engine)):
        body = await request.json()
        try:
            return q.heartbeat(engine.store, job_id, body.get("workerId"),
                               body.get("extendSeconds", q.DEFAULT_LEASE_SECONDS))
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/complete")
    async def queue_complete(job_id: str, request: Request, engine: Engine = Depends(get_engine)):
        body = await request.json()
        try:
            return {"job": q.complete(engine.store, job_id, body.get("workerId"),
                                      body.get("output"))}
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/fail")
    async def queue_fail(job_id: str, request: Request, engine: Engine = Depends(get_engine)):
        body = await request.json()
        try:
            return {"job": q.fail(engine.store, job_id, body.get("workerId"),
                                  body.get("error") or "unknown error")}
        except RyaError as e:
            raise _q_err(e)

    @api.post("/queue/jobs/{job_id}/cancel")
    async def queue_cancel(job_id: str, request: Request, engine: Engine = Depends(get_engine)):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        return q.cancel(engine.store, job_id, force=bool((body or {}).get("force")))

    @api.post("/queue/jobs/{job_id}/retry")
    def queue_retry(job_id: str, engine: Engine = Depends(get_engine)):
        try:
            return {"job": q.retry(engine.store, job_id)}
        except RyaError as e:
            raise _q_err(e)

    @api.get("/queue/stats")
    def queue_stats(engine: Engine = Depends(get_engine)):
        return q.stats(engine.store)

    def _killswitches(engine: Engine) -> dict:
        """Read the kill switches the runtime will actually honour.

        §11.2 moved these out of the `_runtime_config` memory scope — which a
        bundle could overwrite through ctx.memory.set — into privileged policy
        state. The legacy scope is still READ so a switch set before the move
        keeps working; nothing writes it any more.
        """
        from ..sdk.context import POLICY_KILLSWITCHES
        getter = getattr(engine.store, "policy_get", None)
        if getter is not None:
            switches = getter(POLICY_KILLSWITCHES)
            if switches is not None:
                return switches
        return (engine.store.load_memory("_runtime_config") or {}).get("kv") or {}

    @api.get("/tools")
    def tools(engine: Engine = Depends(get_engine)):
        # Effective permission = manifest, unless a runtime kill switch overrides.
        overrides = _killswitches(engine)
        out = []
        for t in manifest.tools:
            ov = overrides.get(f"tool:{t.id}")
            out.append({
                "id": t.id,
                "permission": t.permission.value,
                "effectivePermission": (ov or {}).get("permission") or t.permission.value,
                "override": ov,
            })
        return {"tools": out}

    @api.put("/tools/{tool_id}/permission")
    async def set_tool_permission(tool_id: str, request: Request,
                                  engine: Engine = Depends(get_engine),
                                  authorization: Optional[str] = Header(None),
                                  x_rya_token: Optional[str] = Header(None)):
        """Runtime kill switch: override a tool's permission NOW, without a
        redeploy. Body: {"permission": "...", "reason": "..."} or {"clear": true}
        to drop the override and fall back to the manifest.

        Written to privileged policy state (§11.2), so the change is versioned,
        attributed and append-only auditable — and the bundle whose tool is being
        killed cannot write it back.
        """
        from ..manifest.schema import Permission as Perm
        from ..sdk.context import POLICY_KILLSWITCHES
        from ..store import now_iso

        body = await request.json()
        decl = next((t for t in manifest.tools if t.id == tool_id), None)
        if decl is None:
            raise HTTPException(status_code=404, detail={"code": "E_TOOL_NOT_FOUND",
                                "message": f"Tool '{tool_id}' is not declared in the manifest."})
        setter = getattr(engine.store, "policy_set", None)
        if setter is None:
            raise HTTPException(status_code=501, detail={
                "code": "E_RUNTIME",
                "message": "This store backend does not support privileged policy writes."})

        switches = dict(_killswitches(engine))
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
    def tool_permission_log(limit: int = 50, engine: Engine = Depends(get_engine)):
        """Who changed which kill switch, when, and what it was before."""
        from ..sdk.context import POLICY_KILLSWITCHES
        history = getattr(engine.store, "policy_history", None)
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
                                 engine: Engine = Depends(get_engine),
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

            # `agent_id` in these paths is decorative everywhere else (each handler
            # resolves manifest.name), and a version filed under a name this
            # deployment does not serve would be listed by nothing and run by
            # nobody. So it is checked here instead of ignored.
            declared = str((rebuilt.manifest or {}).get("name") or "")
            if agent_id != manifest.name or declared != manifest.name:
                raise HTTPException(status_code=400, detail={
                    "code": "E_VALIDATION",
                    "message": f"Bundle declares agent '{declared or '?'}' and the path says "
                               f"'{agent_id}', but this deployment serves '{manifest.name}'.",
                    "hint": f"Publish to /agents/{manifest.name}/versions from a project whose "
                            f"rya.agent.yaml has `name: {manifest.name}`."})

            try:
                archive_store = bundles.resolve_bundle_store(root)
                stored = bundles.store_bundle(rebuilt, archive_store)
            except RyaError as e:
                raise _dep_err(e) from e

            try:
                version = _deployments().create_version(
                    engine.store, agent=manifest.name, bundle=rebuilt, actor=actor,
                    metadata={**meta, "publishedVia": "http"})
            except RyaError as e:
                raise _dep_err(e) from e

        gate_result = None
        if env_name and promote_it:
            try:
                gate_result = gates.check_promotion(engine.store, version=version,
                                                   environment=env_name, actor=actor)
                _deployments().promote(engine.store, environment=env_name, agent=manifest.name,
                                       version_id=version["id"], actor=actor)
            except RyaError as e:
                raise _dep_err(e) from e

        return {
            "ok": True, "agent": manifest.name, "versionId": version["id"],
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
                         engine: Engine = Depends(get_engine)):
        return {"versions": _deployments().list_versions(engine.store, agent=manifest.name,
                                                         state=state)}

    @api.get("/versions/{version_id}")
    def get_version_ep(version_id: str, engine: Engine = Depends(get_engine)):
        v = engine.store.version_get(version_id)
        if v is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        return v

    @api.get("/versions/{version_id}/pinned-runs")
    def pinned_runs_ep(version_id: str, engine: Engine = Depends(get_engine)):
        """Why a retire was refused: the runs still pinned to this version."""
        runs = _deployments().pinned_runs(engine.store, version_id)
        return {"runs": runs, "count": len(runs)}

    @api.get("/versions/{version_id}/runs")
    def version_runs_ep(version_id: str, limit: int = 50, engine: Engine = Depends(get_engine)):
        """Every run pinned to this version — terminal ones included.

        Deliberately NOT the same question as `/versions/{id}/pinned-runs`, which
        answers "what blocks a retire" and therefore excludes terminal runs by
        construction (`deployments.pinned_runs`, §6 "Version retirement"). The
        console's environment → version → runs drill-down (§11 item 12) needs the
        history too: "what ran on the hash that is on prod" is mostly finished
        runs, and `/console`'s run list is capped at 30 and carries no versionId.
        """
        if engine.store.version_get(version_id) is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        rows = [r for r in engine.store.list_runs(manifest.name)
                if r.get("versionId") == version_id]
        rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        live = {r["id"] for r in _deployments().pinned_runs(engine.store, version_id)}
        runs = [{**_run_summary(r), "createdAt": r.get("createdAt"),
                 "environment": r.get("environment"), "agentVersion": r.get("agentVersion"),
                 # "pinned" in the retention sense: still holding the version alive.
                 "pinned": r["id"] in live}
                for r in rows[:max(1, min(int(limit or 50), 500))]]
        return {"runs": runs, "count": len(rows), "pinnedCount": len(live)}

    @api.post("/versions/{version_id}/retire")
    async def retire_version_ep(version_id: str, request: Request,
                                engine: Engine = Depends(get_engine)):
        body = await request.json() if await request.body() else {}
        try:
            return _deployments().retire(engine.store, version_id,
                                         force=bool(body.get("force")))
        except RyaError as e:
            raise _dep_err(e)

    @api.get("/agents/{agent_id}/environments")
    def list_environments_ep(agent_id: str, engine: Engine = Depends(get_engine)):
        return {"environments": _deployments().list_environments(engine.store,
                                                                 agent=manifest.name)}

    @api.get("/agents/{agent_id}/environments/{env_name}")
    def describe_environment_ep(agent_id: str, env_name: str,
                                engine: Engine = Depends(get_engine)):
        try:
            return _deployments().describe_environment(engine.store, env_name, manifest.name)
        except RyaError as e:
            raise _dep_err(e)

    @api.post("/agents/{agent_id}/environments/{env_name}/promote")
    async def promote_ep(agent_id: str, env_name: str, request: Request,
                         engine: Engine = Depends(get_engine),
                         authorization: Optional[str] = Header(None),
                         x_rya_token: Optional[str] = Header(None)):
        """Flip the environment's current-version pointer (§9). Atomic: new runs
        go to the new version, in-flight runs finish on theirs."""
        body = await request.json()
        try:
            return _deployments().promote(engine.store, environment=env_name,
                                          agent=manifest.name,
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
                          engine: Engine = Depends(get_engine),
                          authorization: Optional[str] = Header(None),
                          x_rya_token: Optional[str] = Header(None)):
        body = await request.json() if await request.body() else {}
        try:
            return _deployments().rollback(engine.store, environment=env_name,
                                           agent=manifest.name,
                                           to_version_id=body.get("versionId"),
                                           actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)

    @api.get("/agents/{agent_id}/environments/{env_name}/history")
    def environment_history_ep(agent_id: str, env_name: str,
                               engine: Engine = Depends(get_engine)):
        return {"history": _deployments().history(engine.store, env_name, manifest.name)}

    # ---- promotion gates (§9) ---------------------------------------------
    # The readiness/eval gate as a server-side ADMISSION check. Config is
    # privileged platform state (D7), so these routes sit behind the same auth as
    # every other governance write and every change is audited.
    def _gates():
        from .. import gates as _g
        return _g

    @api.get("/gate")
    def get_gate_ep(env: Optional[str] = None, engine: Engine = Depends(get_engine)):
        g = _gates()
        names = [env] if env else sorted(
            {e["name"] for e in _deployments().list_environments(engine.store, agent=manifest.name)}
            | set((engine.store.policy_get(g.POLICY_KEY) or {}).get("environments") or {}))
        try:
            return {"gates": [g.resolve_gate(engine.store, n).describe() for n in names],
                    "default": g.resolve_gate(engine.store, "default").describe()}
        except RyaError as e:
            raise _dep_err(e)

    @api.put("/gate")
    async def put_gate_ep(request: Request, engine: Engine = Depends(get_engine),
                          authorization: Optional[str] = Header(None),
                          x_rya_token: Optional[str] = Header(None)):
        """Replace the promotion gate policy. Validated on write so a mistyped
        requirement is rejected by the operator who typed it."""
        body = await request.json() if await request.body() else {}
        try:
            record = _gates().set_gate(engine.store, body or None,
                                       actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)
        return {"ok": True, "version": record.get("version"), "policy": record.get("value")}

    @api.get("/gate/check")
    def check_gate_ep(env: str, version_id: Optional[str] = None,
                      engine: Engine = Depends(get_engine),
                      authorization: Optional[str] = Header(None),
                      x_rya_token: Optional[str] = Header(None)):
        """Dry-run the gate — what a promotion would refuse, before attempting it."""
        try:
            if version_id:
                version = engine.store.version_get(version_id)
                if version is None:
                    raise RyaError("E_VERSION_NOT_FOUND", f"Version '{version_id}' not found.")
            else:
                version = _deployments().current_version(engine.store, env, manifest.name)
                if version is None:
                    raise RyaError("E_ENVIRONMENT_NOT_FOUND",
                                   f"Nothing is promoted to '{env}' and no versionId was given.")
            result = _gates().check_promotion(engine.store, version=version, environment=env,
                                              actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise _dep_err(e)
        return {"versionId": version["id"], **result.to_dict()}

    @api.get("/versions/{version_id}/attestations")
    def version_attestations_ep(version_id: str, engine: Engine = Depends(get_engine)):
        """The evidence filed against a version: readiness, evals, overrides."""
        if engine.store.version_get(version_id) is None:
            raise HTTPException(status_code=404, detail={
                "code": "E_VERSION_NOT_FOUND", "message": f"Version '{version_id}' not found."})
        rows = _gates().attestations(engine.store, version_id)
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
    def get_quotas_ep(engine: Engine = Depends(get_engine)):
        """This workspace's limits and what it is currently consuming."""
        q = _quotas()
        try:
            policy = q.resolve_quota(engine.store)
            usage = q.usage_snapshot(engine.store)
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        return {"quota": policy.describe(), "usage": usage,
                "admission": q.check_admission(engine.store, kind="any",
                                               usage=usage).to_dict()["violations"]}

    @api.put("/quotas")
    async def put_quotas_ep(request: Request, engine: Engine = Depends(get_engine),
                            authorization: Optional[str] = Header(None),
                            x_rya_token: Optional[str] = Header(None)):
        _require_operator(authorization, x_rya_token)
        body = await request.json() if await request.body() else {}
        try:
            record = _quotas().set_quota(engine.store, body or None,
                                         actor=_actor(authorization, x_rya_token))
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])
        return {"ok": True, "version": record.get("version"), "quota": record.get("value")}

    @api.get("/workers")
    def list_workers_ep(status: Optional[str] = "alive", version_id: Optional[str] = None,
                        engine: Engine = Depends(get_engine)):
        """Which execution-plane processes are live, on which version (§6)."""
        listing = getattr(engine.store, "worker_list", None)
        return {"workers": listing(version_id=version_id, status=status) if listing else []}

    @api.get("/usage")
    def usage_ep(since: Optional[str] = None, until: Optional[str] = None,
                 group_by: Optional[str] = None, engine: Engine = Depends(get_engine)):
        """Billable facts from the durable meter (D10) — not from run traces."""
        from ..observability.usage import workspace_usage
        try:
            return {"usage": workspace_usage(engine.store, since=since, until=until,
                                             group_by=group_by)}
        except RyaError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()["error"])

    @api.get("/models")
    def models(engine: Engine = Depends(get_engine)):
        return {"models": [{"id": m.id, "type": m.type, "permission": m.permission.value} for m in manifest.models]}

    @api.get("/channels")
    def channels(engine: Engine = Depends(get_engine)):
        return {"channels": [c.model_dump() for c in manifest.channels]}

    @api.get("/v1/info")
    def cloud_info(request: Request):
        # Discovery: how an agent connects to this (possibly hosted) instance.
        base = str(request.base_url).rstrip("/")
        return {
            "service": "rya", "version": RYA_VERSION, "agent": manifest.name,
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
