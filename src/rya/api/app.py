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

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    mcp_lifespan = None
    try:
        from ..mcp.server import mounted_app
        from contextlib import asynccontextmanager
        mcp_asgi, _mcp_sm = mounted_app()

        @asynccontextmanager
        async def mcp_lifespan(_app):
            async with _mcp_sm.run():
                yield
    except Exception:  # pragma: no cover - mcp extra absent / import issue
        mcp_asgi = None
        mcp_lifespan = None

    api = FastAPI(title="Rya Control Plane", version=manifest.version, lifespan=mcp_lifespan)

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

                try:
                    run = await asyncio.to_thread(
                        engine.run_event, event_type, payload, "websocket", None, on_trace)
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
        try:
            run = engine.approve(approval_id)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"]}

    @api.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, engine: Engine = Depends(get_engine)):
        try:
            run = engine.reject(approval_id)
        except RyaError as e:
            raise HTTPException(status_code=409, detail=e.to_dict()["error"])
        return {"approvalId": approval_id, "runStatus": run["status"]}

    @api.get("/tools")
    def tools(engine: Engine = Depends(get_engine)):
        return {"tools": [{"id": t.id, "permission": t.permission.value} for t in manifest.tools]}

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

    # Mount remote MCP last so its catch-all under /mcp doesn't shadow API routes.
    if mcp_asgi is not None:
        api.mount("/mcp", mcp_asgi)

    return api
