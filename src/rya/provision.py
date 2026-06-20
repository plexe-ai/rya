"""`rya provision` — stand up the full base infrastructure for a production agent.

One command a coding agent runs to get the *right* infra ready for its agent:
durable database, memory, conversation sessions, authentication, guardrails, the
real-time WebSocket channel, background jobs with retry + dead-letter, horizontal
scale, observability, and secrets — assembled, verified, and reported as a single
inventory with the exact commands to serve it.

This is deliberately idempotent and substrate-aware: re-running it converges to
the same state. It provisions what it safely can (writes a default guard policy,
runs the Postgres tenancy/RLS setup, mints a workspace API key, generates an
operator token) and reports what still needs a human (missing secrets, unfixed
readiness blocks). It never deploys to a cloud or charges money — those stay an
explicit, separate step.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from .readiness import check_readiness
from .sdk.context import load_env

# Component status vocabulary (stable, machine-readable):
#   ready       — exists and verified working
#   provisioned — created/initialized by THIS run
#   warn        — works but below the production bar (e.g. file store)
#   missing     — required for the chosen target and absent (needs a human)
PRODUCTION_TARGETS = ("postgres", "production", "docker")


def _effective_target(target: str, env: dict) -> str:
    if target in (None, "", "auto"):
        return "postgres" if (env.get("RYA_DATABASE_URL") or env.get("DATABASE_URL")) else "local"
    return target


def provision(manifest, store, agent, project_root, target: str = "auto",
              *, force: bool = False, apply: bool = True) -> dict:
    root = Path(project_root)
    env = load_env(root)
    eff = _effective_target(target, env)
    is_prod = eff in PRODUCTION_TARGETS

    components: list[dict] = []
    actions: list[str] = []

    def comp(key, name, status, detail, **extra):
        components.append({"key": key, "name": name, "status": status, "detail": detail, **extra})

    backend = store.describe().get("backend")

    # 1. Compute / runtime --------------------------------------------------
    has_handler = agent.event_handler() is not None
    comp("runtime", "Compute & runtime", "ready" if has_handler else "missing",
         "Durable execution — pause/resume, retries, per-run timeout.",
         handler=has_handler, timeoutSeconds=manifest.timeout_seconds,
         serve="rya serve")

    # 2. Data substrate -----------------------------------------------------
    db_url = env.get("RYA_DATABASE_URL") or env.get("DATABASE_URL")
    if backend == "postgres":
        ran_setup = False
        key_minted = None
        if apply and os.environ.get("RYA_MULTITENANT") == "1" and db_url:
            try:
                from .tenancy import Tenancy
                ten = Tenancy(db_url)
                ten.setup()  # idempotent: schema + RLS + rya_app role
                ran_setup = True
                wss = ten.list_workspaces()
                if not wss:
                    ten.create_workspace("default")
                key_minted = ten.create_api_key(
                    (ten.list_workspaces()[0])["id"], label="provision").get("key")
                actions.append("ran Postgres tenancy + RLS setup (schema, rya_app role, policies)")
                actions.append("minted a workspace API key")
            except Exception as e:  # real DB not reachable / driver missing
                comp("database", "Data substrate", "warn",
                     f"Postgres configured but setup could not run: {e}", backend="postgres",
                     durable=True)
                db_done = True
            else:
                db_done = False
        else:
            db_done = False
        if not db_done:
            comp("database", "Data substrate",
                 "provisioned" if ran_setup else "ready",
                 "Postgres — durable runs, memory, sessions, approvals, jobs; RLS isolation.",
                 backend="postgres", durable=True, rls=ran_setup,
                 apiKey=key_minted)
    else:
        comp("database", "Data substrate", "warn" if is_prod else "ready",
             "File store — zero-config and durable across restarts, but single-node. "
             + ("Set RYA_DATABASE_URL to Postgres for production." if is_prod else "Fine for local dev."),
             backend="file", durable=False,
             fix="Set RYA_DATABASE_URL to a Postgres connection string." if is_prod else None)

    # 3. Memory & 4. Conversations -----------------------------------------
    agent_mem = store.load_memory("agent") if hasattr(store, "load_memory") else {}
    comp("memory", "Memory", "ready",
         "Core memory blocks (always in context, self-editable) + consolidated "
         "long-term facts with vector recall + budget-bounded context assembly — "
         "scoped per user by RLS.",
         vector=True, blocks=len(agent_mem.get("blocks", {})),
         facts=len(agent_mem.get("collections", {}).get("facts", [])))
    comp("sessions", "Conversations", "ready",
         "Durable sessions + messages, threaded per channel for context retrieval.",
         sessions=len(store.list_sessions()) if hasattr(store, "list_sessions") else 0)

    # 5. Authentication -----------------------------------------------------
    if os.environ.get("RYA_MULTITENANT") == "1" and backend == "postgres":
        comp("auth", "Authentication", "ready",
             "Multi-tenant — per-workspace API keys + Postgres row-level security.",
             mode="api-keys+rls")
    elif env.get("RYA_JWT_SECRET") or env.get("RYA_JWKS_URL"):
        comp("auth", "Authentication", "ready", "Per-user identity via verified JWT.", mode="jwt")
    elif os.environ.get("RYA_TOKEN") or env.get("RYA_TOKEN"):
        comp("auth", "Authentication", "ready", "Operator token gates the control plane.", mode="token")
    elif is_prod:
        token = "rya_" + _secrets.token_urlsafe(32)
        actions.append("generated an operator token — export RYA_TOKEN to enforce it")
        comp("auth", "Authentication", "provisioned",
             "Operator token generated. Export RYA_TOKEN=<token> before serving.",
             mode="token", token=token, fix="export RYA_TOKEN=<token>")
    else:
        comp("auth", "Authentication", "warn",
             "Open (local dev) — no auth. Set RYA_TOKEN or RYA_MULTITENANT for production.",
             mode="open")

    # 5b. Scoped connected credentials -------------------------------------
    from .seal import available as _seal_available, key_source
    conns = store.list_connections() if hasattr(store, "list_connections") else []
    scoped_tools = [t for t in manifest.tools if getattr(t, "provider", None)]
    ks = key_source(root)
    enc_ok = _seal_available()
    plaintext = sum(1 for c in conns if c.get("secretSet") and not c.get("encrypted"))
    if not enc_ok:
        conn_status, conn_fix = "warn", "pip install cryptography to encrypt secrets at rest."
    elif plaintext:
        conn_status, conn_fix = "warn", f"{plaintext} connection secret(s) still plaintext — run `rya connections reseal`."
    else:
        conn_status, conn_fix = "ready", None
    comp("connections", "Connected credentials", conn_status,
         "Scoped credentials per provider — encrypted at rest, injected into tool "
         "calls, enforced by the intersection rule (required ⊆ connection ∩ user); "
         "the model never sees a secret.",
         connections=len(conns), scopedTools=len(scoped_tools),
         providers=sorted({getattr(t, "provider") for t in scoped_tools}),
         encryptionAtRest=enc_ok, keySource=ks, plaintextSecrets=plaintext, fix=conn_fix)

    # 6. Guardrails (Action Guard) -----------------------------------------
    from .guard import GUARD_FILE, load_policy, run_tests, save_policy
    gpath = root / GUARD_FILE
    provisioned_guard = False
    if not gpath.exists() and apply:
        from .cli.scaffold import GUARD_TEMPLATE
        gpath.write_text(GUARD_TEMPLATE)
        provisioned_guard = True
        actions.append(f"wrote a default egress policy ({GUARD_FILE})")
    policy = load_policy(str(gpath)) or {"ssrf": True, "default": "deny", "rules": []}
    gtests = run_tests(policy)
    comp("guardrails", "Guardrails (Action Guard)",
         "provisioned" if provisioned_guard else "ready",
         "Network egress firewall — every outbound call checked before it leaves the process.",
         default=policy.get("default"), rules=len(policy.get("rules", [])),
         testsPassed=gtests["passed"], testsTotal=gtests["total"])

    # 7. Real-time WebSocket -----------------------------------------------
    try:
        import fastapi  # noqa: F401
        ws_ready = True
    except Exception:
        ws_ready = False
    comp("realtime", "Real-time channel", "ready" if ws_ready else "missing",
         "Bidirectional WebSocket — drive the agent and stream its run live.",
         endpoint="WS /ws", protocol="json: event|message|replay|ping",
         fix=None if ws_ready else "pip install 'rya[api]'")

    # 8. Jobs: retry + dead-letter -----------------------------------------
    cron = [t for t in manifest.triggers if getattr(t, "type", None) == "cron"]
    comp("jobs", "Background jobs", "ready",
         "Scheduled + delayed work with retry/backoff and a dead-letter queue.",
         retryMaxAttempts=3, backoff="exponential", deadLetter=True,
         cron=len(cron), worker="rya worker")

    # 9. Horizontal scale ---------------------------------------------------
    if backend == "postgres":
        comp("scale", "Horizontal scale", "ready",
             "Run N `rya worker` processes — jobs claimed with FOR UPDATE SKIP LOCKED, "
             "so no two workers ever grab the same job.",
             horizontal=True, jobClaim="FOR UPDATE SKIP LOCKED")
    else:
        comp("scale", "Horizontal scale", "warn" if is_prod else "ready",
             "Single-node on the file store. Move to Postgres to run multiple workers.",
             horizontal=False, fix="Set RYA_DATABASE_URL to a Postgres connection string.")

    # 10. Observability -----------------------------------------------------
    export = "Langfuse" if env.get("LANGFUSE_HOST") else (
        "webhook" if env.get("RYA_TRACE_WEBHOOK") else "local trace store")
    comp("observability", "Observability", "ready" if export != "local trace store" else "warn",
         "Every run is a forensic trace with token + cost accounting.",
         traces=manifest.observability.traces, export=export,
         fix=None if export != "local trace store" else "Set LANGFUSE_HOST or RYA_TRACE_WEBHOOK.")

    # 11. Secrets -----------------------------------------------------------
    from .tools.registry import default_registry as _tools
    treg = _tools()
    required, missing = set(), []
    for t in manifest.tools:
        for s in (getattr(treg.get(t.id), "required_secrets", []) or []):
            required.add(s)
            if not env.get(s) and s not in missing:
                missing.append(s)
    comp("secrets", "Secrets", "missing" if missing else "ready",
         "Names only, never values — resolved in the runtime, never seen by the agent.",
         required=sorted(required), missing=missing,
         fix=(f"rya secrets set {missing[0]}=…" if missing else None))

    # 11b. Evals ------------------------------------------------------------
    from .evals import load_evals
    eval_cases = load_evals(root)
    comp("evals", "Evals", "ready" if eval_cases else "warn",
         "Behavioural eval suite — each case fires a real event and scores the run; "
         "gate a deploy on it with `rya eval`.",
         cases=len(eval_cases),
         fix=None if eval_cases else "Add rya.evals.yaml cases (run `rya eval`).")

    # 12. Readiness gate ----------------------------------------------------
    rep = check_readiness(manifest, store, agent, root)
    comp("readiness", "Production-readiness gate", "ready" if rep["ready"] else "blocked",
         "Machine-checkable production bar — must be green to deploy.",
         blocks=rep["summary"]["blocks"], warnings=rep["summary"]["warnings"])

    # ---- roll up ----------------------------------------------------------
    provisioned_ok = not any(c["status"] in ("missing", "blocked") for c in components)
    # A production target additionally requires durable infra (no warns that
    # matter): we surface them but only block deploy on readiness + missing.
    counts: dict = {}
    for c in components:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    api_key = next((c.get("apiKey") for c in components if c["key"] == "database" and c.get("apiKey")), None)
    op_token = next((c.get("token") for c in components if c["key"] == "auth" and c.get("token")), None)

    return {
        "target": eff,
        "provisioned": provisioned_ok,
        "ready": rep["ready"],
        "components": components,
        "actions": actions,
        "blocks": rep["blocks"],
        "warnings": rep["warnings"],
        "connection": {
            "store": backend,
            "serve": "rya serve --port 8787",
            "worker": "rya worker",
            "console": "http://127.0.0.1:8787/",
            "websocket": "ws://127.0.0.1:8787/ws",
            "webhook": "POST http://127.0.0.1:8787/inbound",
            "apiKey": api_key,
            "operatorToken": op_token,
        },
        "summary": {
            "total": len(components), "byStatus": counts,
            "blocks": rep["summary"]["blocks"], "warnings": rep["summary"]["warnings"],
        },
    }
