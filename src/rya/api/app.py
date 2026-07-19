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

import hashlib
import hmac
import json
import os
import platform
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
        "environment": manifest.environment,
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

    # ---- global turn-reclaim sweeper (the built-in cron) ------------------
    # A crashed chat turn is reclaimable, but reclaim only happens when someone
    # runs it. This background loop IS that someone: every RYA_TURN_SWEEP_SECONDS
    # (default 30; 0 disables) it reclaims + runs crashed/pending turns - across
    # ALL workspaces in multi-tenant mode. Interrupted turns therefore always
    # finish without any external cron.
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

    from contextlib import AsyncExitStack, asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):
        import asyncio
        sweep_seconds = float(os.environ.get("RYA_TURN_SWEEP_SECONDS", "30") or 0)
        task = asyncio.create_task(_sweeper_loop(sweep_seconds)) if sweep_seconds > 0 else None
        async with AsyncExitStack() as stack:
            if _mcp_sm is not None:
                await stack.enter_async_context(_mcp_sm.run())
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()

    api = FastAPI(title="Rya Control Plane", version=manifest.version, lifespan=_lifespan)

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
        viewer = {"workspace": ws_id,
                  "mode": "multi-tenant" if mt else "single-tenant",
                  "user": (manifest.owner if not mt else None)}
        return {**build_console(manifest, store, agent, root),
                "infra": build_infra(manifest, store), "viewer": viewer}

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

                # Stream every trace event the instant it happens. The callback
                # fires on the worker thread; marshal each send onto the loop.
                futs = []

                def on_trace(ev):
                    futs.append(asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "trace", "event": ev}), loop))

                # Token-level LLM streaming: each text chunk lands as a
                # {"type":"token"} frame the moment the model emits it.
                def on_token(chunk):
                    futs.append(asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "token", "text": chunk}), loop))

                try:
                    run = await asyncio.to_thread(
                        engine.run_event, event_type, payload, "websocket", None, on_trace, on_token)
                except RyaError as e:
                    await websocket.send_json({"type": "error", **e.to_dict()["error"]})
                    continue
                for f in futs:  # ensure all trace frames land before replies/summary
                    try:
                        await asyncio.wrap_future(f)
                    except Exception:
                        pass

                # Conversational reply: surface assistant messages this run wrote,
                # BEFORE the terminal summary so `run` is always the last frame a
                # client must wait for (no ambiguous "is more coming?" blocking).
                if mtype == "message" and hasattr(engine.store, "find_session"):
                    sess = engine.store.find_session(manifest.name, payload["channel"],
                                                     payload["externalId"])
                    if sess:
                        for m in engine.store.list_messages(sess["id"]):
                            if m.get("runId") == run["id"] and m.get("role") in ("assistant", "agent"):
                                await websocket.send_json({"type": "message", "message": m})

                await websocket.send_json({"type": "run", "run": _run_summary(run)})
                continue

            await websocket.send_json({"type": "error", "message": f"unknown type '{mtype}'"})

    @api.post("/agents/{agent_id}/events/stream")
    async def post_event_stream(agent_id: str, request: Request,
                                engine: Engine = Depends(get_engine),
                                authorization: Optional[str] = Header(None),
                                x_rya_token: Optional[str] = Header(None)):
        """Trigger a run and stream it back as Server-Sent Events.

        The default client transport (plain HTTP - works through ALBs, proxies,
        and `fetch` with no connection upgrade). Frames, in order of arrival:

          event: token    {"text": ...}          - each streamed LLM chunk
          event: trace    {trace event}          - each journaled step
          event: message  {assistant message}    - session replies (chat agents)
          event: run      {run summary}          - ALWAYS the terminal frame
          event: error    {code, message}        - fatal error (then closes)

        Clients wait for `run` and never have to guess whether more is coming.
        """
        import asyncio
        import queue as _q

        from fastapi.responses import StreamingResponse

        body = await request.json()
        event_type = body.get("type", "message.received")
        payload = body.get("payload", {})
        identity = _identity_from(authorization, x_rya_token, required=False)

        frames: _q.Queue = _q.Queue()
        _DONE = object()

        def emit(kind: str, data: dict) -> None:
            frames.put((kind, data))

        def produce() -> None:
            try:
                run = engine.run_event(event_type, payload, "sse", identity=identity,
                                       on_trace=lambda ev: emit("trace", ev),
                                       on_token=lambda chunk: emit("token", {"text": chunk}))
                # Chat agents: surface assistant messages this run wrote, BEFORE
                # the terminal summary (same contract as the WebSocket).
                if hasattr(engine.store, "find_session") and isinstance(payload, dict) \
                        and payload.get("channel") and payload.get("externalId"):
                    sess = engine.store.find_session(manifest.name, payload["channel"],
                                                     payload["externalId"])
                    if sess:
                        for m in engine.store.list_messages(sess["id"]):
                            if m.get("runId") == run["id"] and m.get("role") in ("assistant", "agent"):
                                emit("message", m)
                emit("run", _run_summary(run))
            except RyaError as e:
                emit("error", e.to_dict()["error"])
            except Exception as e:  # never leave the stream hanging
                emit("error", {"code": "E_RUNTIME", "message": str(e)})
            finally:
                frames.put(_DONE)

        async def sse():
            producer = asyncio.get_running_loop().run_in_executor(None, produce)
            try:
                while True:
                    try:
                        item = await asyncio.to_thread(frames.get, True, 15.0)
                    except _q.Empty:
                        yield ": keep-alive\n\n"  # comment frame; defeats idle timeouts
                        continue
                    if item is _DONE:
                        break
                    kind, data = item
                    yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
            finally:
                await producer

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---- durable chat turns ---------------------------------------------
    # Unlike /events/stream (synchronous - a mid-turn crash strands the run), a
    # TURN is a durable, leased, reclaimable job whose frames land in a durable
    # buffer; the stream endpoint just tails it and resumes on reconnect. See
    # rya.turns.
    from .. import turns as _turns

    def _fresh_engine_for(authorization, x_rya_token) -> Engine:
        """A standalone engine for background turn execution - never shares the
        request engine's connection across the response boundary."""
        if mt:
            return engine_for(authorize(authorization, x_rya_token))
        return base_engine

    @api.post("/agents/{agent_id}/turns")
    async def create_turn_ep(agent_id: str, request: Request, background: BackgroundTasks,
                             engine: Engine = Depends(get_engine),
                             authorization: Optional[str] = Header(None),
                             x_rya_token: Optional[str] = Header(None)):
        """Start a DURABLE chat turn. Returns ``{turnId}`` immediately; the turn
        runs on a worker (kicked inline here, reclaimed on crash) and streams via
        GET /agents/{id}/turns/{turnId}/stream."""
        body = await request.json()
        res = _turns.create_turn(engine, body.get("type", "message.received"),
                                 body.get("payload", {}))

        def _kick():
            try:
                _turns.execute_pending(_fresh_engine_for(authorization, x_rya_token),
                                       worker_id="inline", limit=1)
            except Exception:  # reclaim will re-drive it; never fail the request
                import logging
                logging.getLogger("rya.turns").warning("inline turn execution failed", exc_info=True)

        background.add_task(_kick)
        return res

    @api.get("/agents/{agent_id}/turns/{turn_id}/stream")
    async def turn_stream_ep(agent_id: str, turn_id: str, request: Request,
                             after: int = -1, engine: Engine = Depends(get_engine)):
        """Tail a turn's durable stream as SSE. Resumable: reconnect with
        ``?after=<lastSeq>`` (or the browser's Last-Event-ID header) to continue
        exactly where the dropped connection left off. Ends on the terminal
        run/error frame."""
        import asyncio

        from fastapi.responses import StreamingResponse

        last_id = request.headers.get("last-event-id")
        start = int(last_id) if (last_id and last_id.lstrip("-").isdigit()) else after
        idle_limit = max(1, int(float(os.environ.get("RYA_TURN_STREAM_IDLE_SECONDS", "60")) / 0.3))

        def _is_terminal(f: dict) -> bool:
            # A run frame with waiting_approval is a PAUSE marker - the approval
            # resolution appends the continuation + the real terminal frame to
            # this same buffer, so the tail must not end there.
            from ..turns import TERMINAL_RUN_STATUSES
            return f["kind"] == "error" or (
                f["kind"] == "run" and (f.get("data") or {}).get("status") in TERMINAL_RUN_STATUSES)

        async def sse():
            cursor = start
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                frames = await asyncio.to_thread(engine.store.stream_read, turn_id, cursor)
                if frames:
                    idle = 0
                    for f in frames:
                        cursor = f["seq"]
                        yield (f"id: {f['seq']}\nevent: {f['kind']}\n"
                               f"data: {json.dumps(f['data'], default=str)}\n\n")
                        if _is_terminal(f):
                            return
                else:
                    idle += 1
                    if idle >= idle_limit:  # no new frame; client reconnects w/ Last-Event-ID
                        yield ": idle-timeout\n\n"
                        return
                    if idle % 15 == 0:
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.3)

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
        from ..guard import load_policy, run_tests, GUARD_FILE
        p = root / GUARD_FILE
        policy = load_policy(str(p)) or {"ssrf": True, "default": "deny", "fail": "closed",
                                         "policy": "", "rules": []}
        return {"policy": policy, "tests": run_tests(policy), "exists": p.is_file()}

    @api.put("/guard")
    async def put_guard(request: Request, engine: Engine = Depends(get_engine)):
        from ..guard import save_policy, run_tests, GUARD_FILE
        body = await request.json()
        policy = body.get("policy", body)
        save_policy(policy, str(root / GUARD_FILE))
        return {"ok": True, "tests": run_tests(policy)}

    @api.post("/guard/test")
    def test_guard(engine: Engine = Depends(get_engine)):
        from ..guard import load_policy, run_tests, GUARD_FILE
        policy = load_policy(str(root / GUARD_FILE)) or {}
        return run_tests(policy)

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
        if status not in ("completed", "failed", "running", "waiting_approval", "rejected"):
            raise HTTPException(status_code=400, detail={
                "code": "E_VALIDATION",
                "message": "status must be one of completed|failed|running|waiting_approval|rejected."})
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
                export_run(run)
            except Exception:
                pass
        return {"ok": True, "runId": run["id"], "events": len(trace)}

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

    @api.get("/sessions/{session_id}")
    def get_session(session_id: str, engine: Engine = Depends(get_engine)):
        session = engine.store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={"code": "E_SESSION_NOT_FOUND"})
        return session

    @api.get("/approvals")
    def list_approvals(status: Optional[str] = None, engine: Engine = Depends(get_engine)):
        return {"approvals": engine.store.list_approvals(status)}

    @api.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, engine: Engine = Depends(get_engine)):
        # Turn-bound runs stream their post-approval continuation onto the
        # original turn's durable buffer (resolve_on_stream); plain runs approve
        # exactly as before.
        try:
            run = _turns.resolve_on_stream(engine, approval_id, approve=True)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"],
                "turnId": run.get("turnId")}

    @api.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, engine: Engine = Depends(get_engine)):
        try:
            run = _turns.resolve_on_stream(engine, approval_id, approve=False)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"],
                "turnId": run.get("turnId")}

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

    @api.get("/tools")
    def tools(engine: Engine = Depends(get_engine)):
        # Effective permission = manifest, unless a runtime kill switch overrides.
        rc = engine.store.load_memory("_runtime_config")
        overrides = rc.get("kv") or {}
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
    async def set_tool_permission(tool_id: str, request: Request, engine: Engine = Depends(get_engine)):
        """Runtime kill switch: override a tool's permission NOW, without a
        redeploy. Versioned + append-only history. Body:
        {"permission": "...", "reason": "..."} or {"clear": true} to drop the
        override and fall back to the manifest."""
        from ..manifest.schema import Permission as Perm
        body = await request.json()
        decl = next((t for t in manifest.tools if t.id == tool_id), None)
        if decl is None:
            raise HTTPException(status_code=404, detail={"code": "E_TOOL_NOT_FOUND",
                                "message": f"Tool '{tool_id}' is not declared in the manifest."})
        rc = engine.store.load_memory("_runtime_config")
        kv = rc.setdefault("kv", {})
        hist = rc.setdefault("collections", {}).setdefault("history", [])
        prev = (kv.get(f"tool:{tool_id}") or {}).get("permission") or decl.permission.value
        if body.get("clear"):
            kv.pop(f"tool:{tool_id}", None)
            new = decl.permission.value
        else:
            perm = body.get("permission")
            if perm not in {p.value for p in Perm}:
                raise HTTPException(status_code=400, detail={
                    "code": "E_VALIDATION",
                    "message": f"permission must be one of {sorted(p.value for p in Perm)}."})
            new = perm
        from ..store import now_iso
        version = len(hist) + 1
        ts = now_iso()
        entry = {"version": version, "tool": tool_id, "permission": new, "previous": prev,
                 "cleared": bool(body.get("clear")), "reason": body.get("reason"), "ts": ts}
        if not body.get("clear"):
            kv[f"tool:{tool_id}"] = {"permission": new, "version": version, "ts": ts}
        hist.append(entry)
        engine.store.save_memory("_runtime_config", rc)
        return {"ok": True, **entry}

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
        key = tenancy.create_api_key(ws_id, label=(body or {}).get("label") or s["email"])
        ws = next((w for w in tenancy.list_user_workspaces(s["sub"]) if w["id"] == ws_id), {"id": ws_id})
        return {"ok": True, "workspace": ws, "apiKey": key["key"]}

    # Mount remote MCP last so its catch-all under /mcp doesn't shadow API routes.
    if mcp_asgi is not None:
        api.mount("/mcp", mcp_asgi)

    return api
