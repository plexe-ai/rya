"""Rya CLI.

Agent-friendly by design: every command takes ``--json`` for machine-readable
output and ``--non-interactive`` to forbid hidden prompts, errors carry stable
codes + a suggested next action, and exit codes are semantic (see errors.py).
"""

from __future__ import annotations

import json as jsonlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..errors import RyaError, EXIT_OK
from ..manifest import find_manifest, load_manifest
from ..manifest.loader import MANIFEST_NAME
from ..runtime import Engine, load_agent
from ..store import Store, open_store
from ..sdk.context import load_env
from ..tools.registry import default_registry as default_tools
from ..models.registry import default_registry as default_models
from . import scaffold

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Rya — production backend/runtime for AI agents.")
console = Console()
err_console = Console(stderr=True)

# Sub-apps
agents_app = typer.Typer(no_args_is_help=True, help="Inspect agents.")
events_app = typer.Typer(no_args_is_help=True, help="Send events into the runtime.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect runs and traces.")
approvals_app = typer.Typer(no_args_is_help=True, help="List/approve/reject human approvals.")
tools_app = typer.Typer(no_args_is_help=True, help="Tool registry.")
models_app = typer.Typer(no_args_is_help=True, help="Model registry.")
channels_app = typer.Typer(no_args_is_help=True, help="Channels.")
secrets_app = typer.Typer(no_args_is_help=True, help="Secrets (metadata only).")
schedules_app = typer.Typer(no_args_is_help=True, help="Cron schedules.")
jobs_app = typer.Typer(no_args_is_help=True, help="Background jobs.")
skills_app = typer.Typer(no_args_is_help=True, help="Install the Rya coding-agent skill.")
workspaces_app = typer.Typer(no_args_is_help=True, help="Manage tenant workspaces (Postgres/cloud).")
keys_app = typer.Typer(no_args_is_help=True, help="Manage per-workspace API keys.")
connections_app = typer.Typer(no_args_is_help=True, help="Scoped connected credentials for tools.")

app.add_typer(agents_app, name="agents")
app.add_typer(events_app, name="events")
app.add_typer(runs_app, name="runs")
app.add_typer(approvals_app, name="approvals")
app.add_typer(tools_app, name="tools")
app.add_typer(models_app, name="models")
app.add_typer(channels_app, name="channels")
app.add_typer(secrets_app, name="secrets")
app.add_typer(schedules_app, name="schedules")
app.add_typer(jobs_app, name="jobs")
app.add_typer(skills_app, name="skills")
app.add_typer(workspaces_app, name="workspaces")
app.add_typer(keys_app, name="keys")
app.add_typer(connections_app, name="connections")


def _version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(False, "--version", "-V", callback=_version_callback,
                                       is_eager=True, help="Show the Rya version and exit.")):
    """Rya — production backend/runtime for AI agents."""


# --------------------------------------------------------------------------
# Output + error helpers
# --------------------------------------------------------------------------
def emit(json_mode: bool, payload: dict, render=None) -> None:
    if json_mode:
        typer.echo(jsonlib.dumps({"ok": True, **payload}, default=str))
    elif render is not None:
        render()


@contextmanager
def guard(json_mode: bool):
    try:
        yield
    except RyaError as e:
        if json_mode:
            typer.echo(jsonlib.dumps(e.to_dict(), default=str))
        else:
            err_console.print(f"[red]✗[/red] [{e.code}] {e.message}")
            if e.hint:
                err_console.print(f"  [dim]next:[/dim] {e.hint}")
        raise typer.Exit(e.exit_code)


def _project() -> tuple[Path, "load_manifest"]:
    path = find_manifest()
    if path is None:
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            f"No {MANIFEST_NAME} found here or in any parent directory.",
            hint="Run `rya create <name>` or cd into a Rya project.",
        )
    manifest = load_manifest(path)
    return path.parent, manifest


def _store() -> tuple[Path, "load_manifest", Store]:
    root, manifest = _project()
    store = open_store(root)
    return root, manifest, store


def _engine() -> Engine:
    root, manifest = _project()
    agent = load_agent(manifest, root)
    return Engine(manifest, agent, open_store(root), root)


def _parse_payload(payload: Optional[str], payload_file: Optional[Path]) -> dict:
    if payload_file is not None:
        try:
            return jsonlib.loads(Path(payload_file).read_text())
        except (OSError, jsonlib.JSONDecodeError) as e:
            raise RyaError("E_VALIDATION", f"Could not read payload file: {e}",
                           hint="Pass a path to a valid JSON file.")
    if payload:
        try:
            return jsonlib.loads(payload)
        except jsonlib.JSONDecodeError as e:
            raise RyaError("E_VALIDATION", f"--payload is not valid JSON: {e}",
                           hint='Pass JSON, e.g. --payload \'{"email":"a@b.com"}\'')
    return {}


def _write_manifest_raw(root: Path, data: dict) -> None:
    (root / MANIFEST_NAME).write_text(yaml.safe_dump(data, sort_keys=False))


def _load_manifest_raw(root: Path) -> dict:
    return yaml.safe_load((root / MANIFEST_NAME).read_text()) or {}


# --------------------------------------------------------------------------
# Top-level commands
# --------------------------------------------------------------------------
@app.command()
def login(json: bool = typer.Option(False, "--json")):
    """Authenticate. The local runtime needs no auth; hosted login lands in a later milestone."""
    with guard(json):
        emit(json, {"mode": "local", "authenticated": True,
                    "message": "Local runtime — no authentication required."},
             lambda: console.print("[green]✓[/green] Local runtime — no authentication required."))


@app.command()
def create(
    name: str = typer.Argument(..., help="Project / agent name."),
    json: bool = typer.Option(False, "--json"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
):
    """Scaffold a new agent project in ./<name>."""
    with guard(json):
        target = Path.cwd() / name
        written = scaffold.write_project(target, name, overwrite=force)
        emit(json, {"name": name, "path": str(target), "files": written,
                    "next": [f"cd {name}", "rya dev", "rya events send --type message.received --payload '{\"email\":\"ada@example.com\"}'"]},
             lambda: (console.print(f"[green]✓[/green] Created project [bold]{name}[/bold] at {target}"),
                      console.print("  next: [bold]cd " + name + " && rya dev[/bold]")))


@app.command()
def init(json: bool = typer.Option(False, "--json"), force: bool = typer.Option(False, "--force")):
    """Scaffold a project in the current directory."""
    with guard(json):
        name = Path.cwd().name
        written = scaffold.write_project(Path.cwd(), name, overwrite=force)
        emit(json, {"name": name, "files": written},
             lambda: console.print(f"[green]✓[/green] Initialized Rya project [bold]{name}[/bold] ({len(written)} files)"))


@app.command()
def dev(json: bool = typer.Option(False, "--json")):
    """Load + validate the manifest and the agent code; report what's wired up."""
    with guard(json):
        root, manifest = _project()
        agent = load_agent(manifest, root)
        info = {
            "agent": manifest.name,
            "version": manifest.version,
            "runtime": manifest.runtime,
            "entrypoint": manifest.entrypoint,
            "eventHandler": agent.event_handler() is not None,
            "jobHandlers": list(agent._job_handlers.keys()),
            "cronHandlers": list(agent._cron_handlers.keys()),
            "tools": [t.id for t in manifest.tools],
            "models": [m.id for m in manifest.models],
            "triggers": [t.id for t in manifest.triggers],
            "ready": agent.event_handler() is not None,
        }
        def render():
            console.print(f"[green]✓[/green] [bold]{manifest.name}[/bold] v{manifest.version} ready ({manifest.runtime})")
            console.print(f"  entrypoint: {manifest.entrypoint}")
            console.print(f"  event handler: {'yes' if info['eventHandler'] else '[red]MISSING[/red]'}")
            console.print(f"  jobs: {', '.join(info['jobHandlers']) or '—'}")
            console.print(f"  tools: {', '.join(info['tools']) or '—'}")
            console.print("  send a test event: [bold]rya events send --type message.received --payload '{\"email\":\"ada@example.com\"}'[/bold]")
        emit(json, info, render)


@app.command()
def deploy(target: str = typer.Option("check", "--target", help="check | docker | fly | render"),
           check: bool = typer.Option(False, "--check", help="Only run the production-readiness check, then exit."),
           force: bool = typer.Option(False, "--force", help="Deploy even if readiness blocks remain."),
           write: bool = typer.Option(True, "--write/--no-write", help="Write deploy artifacts into the project."),
           json: bool = typer.Option(False, "--json"),
           non_interactive: bool = typer.Option(False, "--non-interactive")):
    """Check production-readiness, then generate deploy artifacts + plan.

    `rya deploy --check` runs the readiness checklist and exits non-zero if any
    blocker remains (exit 7) — a coding agent makes this all-green to ship safely.
    A plain `rya deploy` runs the check as a GATE first (override with --force).
    """
    with guard(json):
        from ..readiness import check_readiness
        from .deploy_templates import write_artifacts, deploy_plan
        root, manifest = _project()
        agent = load_agent(manifest, root)  # validates the agent code imports
        store = open_store(root)
        rep = check_readiness(manifest, store, agent, root)

        def render_check():
            if rep["ready"]:
                console.print(f"[green]✓[/green] {manifest.name} is production-ready ({rep['summary']['warnings']} warning(s))")
            else:
                console.print(f"[red]✗[/red] {rep['summary']['blocks']} blocker(s) before production:")
            for b in rep["blocks"]:
                console.print(f"  [red]•[/red] [{b['code']}] {b['message']}")
                console.print(f"      [dim]fix:[/dim] {b['fix']}")
            for w in rep["warnings"]:
                console.print(f"  [yellow]•[/yellow] [{w['code']}] {w['message']}")
                console.print(f"      [dim]fix:[/dim] {w['fix']}")

        # --check: readiness only, semantic exit code.
        if check:
            emit(json, {"ready": rep["ready"], **rep}, render_check)
            raise typer.Exit(EXIT_OK if rep["ready"] else 7)

        if target not in ("check", "docker", "fly", "render"):
            raise RyaError("E_VALIDATION", f"unknown --target '{target}'.",
                           hint="Use one of: check, docker, fly, render.")

        # Gate the deploy on readiness unless overridden.
        if not rep["ready"] and not force:
            raise RyaError("E_NOT_PRODUCTION_READY",
                           f"{rep['summary']['blocks']} readiness blocker(s) must be fixed before deploy.",
                           hint="Run `rya deploy --check --json` for the checklist, or pass --force to override.")

        written = write_artifacts(root) if write else []
        plan = deploy_plan(target, manifest, root)
        out = {"validated": True, "ready": rep["ready"], "warnings": rep["warnings"],
               "agent": manifest.name, "version": manifest.version,
               "target": target, "artifacts": written, **plan}
        def render():
            console.print(f"[green]✓[/green] {manifest.name} v{manifest.version} ready for [bold]{target}[/bold]"
                          + (f" ([yellow]{rep['summary']['warnings']} warning(s)[/yellow])" if rep["warnings"] else ""))
            if written:
                console.print(f"  wrote: {', '.join(written)}")
            console.print(f"  deploy: [bold]{plan['command']}[/bold]")
        emit(json, out, render)


@app.command(name="eval")
def eval_cmd(
    id: Optional[str] = typer.Option(None, "--id", help="Run only this eval case."),
    json: bool = typer.Option(False, "--json"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
):
    """Run the declarative eval suite (rya.evals.yaml) against the runtime.

    Each case fires a real event and scores the run against expectations. Exits
    non-zero (5) if any case fails — gate a deploy on it like `rya deploy --check`.
    """
    with guard(json):
        from ..evals import run_evals
        root, manifest = _project()
        agent = load_agent(manifest, root)
        store = open_store(root)
        rep = run_evals(manifest, agent, store, root, only=id)

        def render():
            if not rep["hasEvals"]:
                console.print("[yellow]no evals[/yellow] — create rya.evals.yaml (rya create scaffolds one).")
                return
            head = "[green]✓[/green]" if rep["ok"] else "[red]✗[/red]"
            console.print(f"{head} evals: {rep['passed']}/{rep['total']} passed "
                          f"(score {rep['score']})")
            for r in rep["results"]:
                g = "[green]✓[/green]" if r["pass"] else "[red]✗[/red]"
                console.print(f"  {g} [bold]{r['id']}[/bold]  [dim]{r['status']} · {r['runId']}[/dim]")
                for c in r["checks"]:
                    if not c["pass"]:
                        console.print(f"      [red]✗[/red] {c['check']}: {c['detail']}")
                if r.get("error"):
                    console.print(f"      [red]error:[/red] {r['error']}")

        emit(json, rep, render)
        if rep["hasEvals"] and not rep["ok"]:
            raise typer.Exit(5)


@app.command()
def connect(
    provider: str = typer.Argument(..., help="Provider name the tool binds to, e.g. github, slack, stripe."),
    scopes: str = typer.Option("", "--scopes", help="Comma-separated scopes this credential grants."),
    token: Optional[str] = typer.Option(None, "--token", help="The secret/credential to vault."),
    user: Optional[str] = typer.Option(None, "--user", help="Bind to a user (owner); omit for workspace-shared."),
    label: Optional[str] = typer.Option(None, "--label"),
    json: bool = typer.Option(False, "--json"),
):
    """Create a scoped, vaulted connection a tool uses to act on a provider.

    The secret is stored vaulted and injected into tool calls at runtime — the
    agent/model never sees it. Tools bind to it via `provider:` + `scopes:` in the
    manifest; at call time the runtime enforces required ⊆ (connection ∩ user).
    """
    with guard(json):
        root, _, store = _store()
        scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
        conn = store.create_connection(provider, scope_list, secret=token, owner=user, label=label)
        from ..seal import key_source
        ks = key_source(root)
        if not conn["secretSet"]:
            secret_state = "NO secret"
        elif conn.get("encrypted"):
            secret_state = f"secret encrypted at rest · key: {ks}"
        else:
            secret_state = "secret stored UNENCRYPTED (install cryptography or set RYA_SECRET_KEY)"
        emit(json, {"ok": True, "connection": conn},
             lambda: (console.print(f"[green]✓[/green] connected [bold]{provider}[/bold] "
                                    f"({secret_state}) scopes: {', '.join(conn['scopes']) or '—'}"),
                      console.print(f"  id: {conn['id']}"
                                    + (f"  owner: {conn['owner']}" if conn.get('owner') else "  (workspace-shared)"))))


@connections_app.command("list")
def connections_list(json: bool = typer.Option(False, "--json")):
    """List connections (provider, scopes, owner, status) — never the secret."""
    with guard(json):
        _, _, store = _store()
        conns = store.list_connections()
        emit(json, {"connections": conns, "count": len(conns)},
             lambda: [console.print(
                 f"  {'[green]●[/green]' if c['status']=='active' else '[dim]○[/dim]'} "
                 f"[bold]{c['provider']}[/bold]  scopes: {', '.join(c['scopes']) or '—'}  "
                 f"{'· '+c['owner'] if c.get('owner') else '· shared'}  [dim]{c['id']}[/dim]")
                 for c in conns] or [console.print("  [dim]no connections[/dim]")])


@connections_app.command("reseal")
def connections_reseal(json: bool = typer.Option(False, "--json")):
    """Encrypt any legacy plaintext connection secrets at rest (one-time migration).

    Connections created before encryption-at-rest existed keep their secret in
    plaintext until rewritten. This re-seals them in place. Idempotent.
    """
    with guard(json):
        from ..seal import available, key_source
        root, _, store = _store()
        if not available():
            raise RyaError("E_CRYPTO_UNAVAILABLE",
                           "cryptography is not installed — cannot encrypt secrets.",
                           hint="pip install cryptography (or install rya with its base deps).")
        res = store.reseal_connections()
        emit(json, {"ok": True, "keySource": key_source(root), **res},
             lambda: console.print(
                 f"[green]✓[/green] resealed [bold]{res['resealed']}[/bold] secret(s) · "
                 f"{res['alreadyEncrypted']} already encrypted, {res['noSecret']} without a secret "
                 f"({res['scanned']} scanned)"))


@connections_app.command("revoke")
def connections_revoke(connection_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Revoke a connection and destroy its vaulted credential."""
    with guard(json):
        _, _, store = _store()
        ok = store.revoke_connection(connection_id)
        if not ok:
            raise RyaError("E_NOT_FOUND", f"No connection '{connection_id}'.", hint="rya connections list")
        emit(json, {"ok": True, "revoked": connection_id},
             lambda: console.print(f"[green]✓[/green] revoked {connection_id} (credential destroyed)"))


@app.command()
def provision(
    target: str = typer.Option("auto", "--target", help="auto | local | postgres | docker"),
    apply: bool = typer.Option(True, "--apply/--dry-run", help="Actually provision, or just inspect."),
    force: bool = typer.Option(False, "--force", help="Exit 0 even if infra is incomplete."),
    json: bool = typer.Option(False, "--json"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
):
    """Stand up the full base infrastructure for a production-grade agent.

    Assembles and verifies every primitive a production agent needs — durable
    database, memory, conversation sessions, authentication, guardrails, the
    real-time WebSocket channel, jobs with retry + dead-letter, horizontal
    scale, observability, and secrets — then prints the exact commands to serve
    it. Idempotent: re-running converges to the same state.
    """
    with guard(json):
        from ..provision import provision as run_provision
        root, manifest = _project()
        agent = load_agent(manifest, root)
        store = open_store(root)
        rep = run_provision(manifest, store, agent, root, target=target, force=force, apply=apply)

        _GLYPH = {"ready": "[green]✓[/green]", "provisioned": "[green]✦[/green]",
                  "warn": "[yellow]●[/yellow]", "missing": "[red]✗[/red]", "blocked": "[red]✗[/red]"}

        def render():
            s = rep["summary"]
            head = "provisioned" if rep["provisioned"] else "[red]incomplete[/red]"
            console.print(f"[bold]{manifest.name}[/bold] · base infra [{rep['target']}] — {head} "
                          f"({s['byStatus'].get('ready',0)} ready, "
                          f"{s['byStatus'].get('provisioned',0)} provisioned, "
                          f"{s['byStatus'].get('warn',0)} warn, "
                          f"{s['byStatus'].get('missing',0)+s['byStatus'].get('blocked',0)} missing)")
            for c in rep["components"]:
                line = f"  {_GLYPH.get(c['status'],'•')} [bold]{c['name']}[/bold] — {c['detail']}"
                console.print(line)
                if c.get("fix") and c["status"] in ("missing", "warn", "blocked"):
                    console.print(f"      [dim]fix:[/dim] {c['fix']}")
            if rep["actions"]:
                console.print("  [dim]actions taken:[/dim]")
                for a in rep["actions"]:
                    console.print(f"    • {a}")
            con = rep["connection"]
            console.print(f"  [dim]serve:[/dim] [bold]{con['serve']}[/bold]   "
                          f"[dim]worker:[/dim] [bold]{con['worker']}[/bold]")
            console.print(f"  [dim]websocket:[/dim] {con['websocket']}   [dim]console:[/dim] {con['console']}")
            if con.get("operatorToken"):
                console.print(f"  [dim]operator token:[/dim] {con['operatorToken']}")
            if con.get("apiKey"):
                console.print(f"  [dim]workspace API key:[/dim] {con['apiKey']}")

        emit(json, rep, render)
        if not force and not (rep["provisioned"] and rep["ready"]):
            raise typer.Exit(7)


@app.command()
def context(json: bool = typer.Option(False, "--json"),
            recent: int = typer.Option(5, "--recent", help="How many recent runs to include.")):
    """One-shot machine-readable snapshot of the whole agent backend (for coding agents).

    Hands you the manifest, tools+permissions, models, channels, handlers, recent
    runs, pending approvals/jobs, active store/LLM backend, the rules to respect,
    and suggested next actions — so you don't burn tokens discovering state.
    """
    with guard(json):
        from ..snapshot import build_snapshot
        root, manifest = _project()
        agent = load_agent(manifest, root)
        store = open_store(root)
        snap = build_snapshot(manifest, store, agent, recent, project_root=root)
        def render():
            a = snap["agent"]
            console.print(f"[bold]{a['name']}[/bold] v{a['version']} ({a['runtime']}/{a['environment']})")
            console.print(f"  store: {snap['runtime']['store']['backend']}  llm: {snap['runtime']['llmProvider']}  multiTenant: {snap['runtime']['multiTenant']}")
            console.print(f"  tools: {', '.join(t['id']+'('+t['permission']+')' for t in snap['tools']) or '—'}")
            console.print(f"  handlers: event={snap['handlers']['event']} jobs={snap['handlers']['jobs']}")
            console.print(f"  runs: {snap['runs']['total']} {snap['runs']['byStatus']}  approvals pending: {snap['approvals']['pendingCount']}")
            if snap["next"]:
                console.print("  next:")
                for n in snap["next"]:
                    console.print(f"    • {n}")
        emit(json, snap, render)


@app.command()
def status(json: bool = typer.Option(False, "--json")):
    """Show overall runtime status: agent, runs, pending approvals, jobs."""
    with guard(json):
        root, manifest, store = _store()
        runs = store.list_runs()
        counts: dict = {}
        for r in runs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        from ..providers import resolve_provider
        data = {
            "agent": manifest.name,
            "version": manifest.version,
            "environment": manifest.environment,
            "store": store.describe(),
            "llmProvider": resolve_provider(manifest.model.provider),
            "runs": {"total": len(runs), "byStatus": counts},
            "approvalsPending": len(store.list_approvals("pending")),
            "jobsPending": len(store.list_jobs("pending")),
            "tools": len(manifest.tools),
            "models": len(manifest.models),
            "channels": len(manifest.channels),
        }
        def render():
            console.print(f"[bold]{manifest.name}[/bold] v{manifest.version} ({manifest.environment})")
            console.print(f"  store: {data['store']['backend']}  |  llm: {data['llmProvider']}")
            console.print(f"  runs: {len(runs)} {counts}")
            console.print(f"  approvals pending: {data['approvalsPending']}")
            console.print(f"  jobs pending: {data['jobsPending']}")
        emit(json, data, render)


@app.command()
def logs(run: str = typer.Option(..., "--run", help="Run id."),
         json: bool = typer.Option(False, "--json")):
    """Show structured logs (log + trace entries) for a run."""
    with guard(json):
        _, _, store = _store()
        r = store.get_run(run)
        if r is None:
            raise RyaError("E_RUN_NOT_FOUND", f"Run '{run}' not found.", hint="List runs with `rya runs list`.")
        entries = [t for t in r["trace"] if t["kind"] in ("log", "run.started", "run.completed", "run.failed", "run.rejected")]
        emit(json, {"runId": run, "logs": entries},
             lambda: [console.print(f"{e['ts']} [{e['kind']}] {e['label']} {e['data']}") for e in entries] and None)


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------
@agents_app.command("list")
def agents_list(json: bool = typer.Option(False, "--json")):
    with guard(json):
        root, manifest = _project()
        data = {"agents": [{"name": manifest.name, "version": manifest.version,
                            "environment": manifest.environment, "runtime": manifest.runtime}]}
        emit(json, data, lambda: console.print(f"{manifest.name}  v{manifest.version}  ({manifest.environment})"))


@agents_app.command("inspect")
def agents_inspect(name: Optional[str] = typer.Argument(None), json: bool = typer.Option(False, "--json")):
    with guard(json):
        root, manifest = _project()
        data = manifest.model_dump(mode="json")
        emit(json, {"agent": data}, lambda: console.print_json(data=data))


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
@events_app.command("send")
def events_send(
    type: str = typer.Option("message.received", "--type", help="Event type."),
    payload: Optional[str] = typer.Option(None, "--payload", help="Inline JSON payload."),
    payload_file: Optional[Path] = typer.Option(None, "--payload-file", help="Path to JSON payload."),
    source: str = typer.Option("manual", "--source"),
    json: bool = typer.Option(False, "--json"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
):
    """Trigger a run by sending a test event into the runtime."""
    with guard(json):
        data = _parse_payload(payload, payload_file)
        engine = _engine()
        run = engine.run_event(type, data, source)
        out = {"runId": run["id"], "status": run["status"], "pendingApproval": run.get("pendingApproval"),
               "traceLength": len(run["trace"])}
        def render():
            console.print(f"[green]✓[/green] run [bold]{run['id']}[/bold] → [bold]{run['status']}[/bold]")
            if run.get("pendingApproval"):
                console.print(f"  awaiting approval: [yellow]{run['pendingApproval']}[/yellow]")
                console.print(f"  approve it: [bold]rya approvals approve {run['pendingApproval']}[/bold]")
            console.print(f"  trace: [bold]rya runs trace {run['id']}[/bold]")
        emit(json, out, render)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------
@runs_app.command("list")
def runs_list(agent: Optional[str] = typer.Option(None, "--agent"), json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, _, store = _store()
        runs = store.list_runs(agent)
        summary = [{"id": r["id"], "status": r["status"], "trigger": r["trigger"],
                    "createdAt": r["createdAt"], "pendingApproval": r.get("pendingApproval")} for r in runs]
        def render():
            t = Table("run", "status", "trigger", "created")
            for r in summary:
                t.add_row(r["id"], r["status"], r["trigger"], r["createdAt"])
            console.print(t)
        emit(json, {"runs": summary, "count": len(summary)}, render)


@runs_app.command("trace")
def runs_trace(run_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, _, store = _store()
        r = store.get_run(run_id)
        if r is None:
            raise RyaError("E_RUN_NOT_FOUND", f"Run '{run_id}' not found.", hint="List runs with `rya runs list`.")
        from ..observability.usage import run_usage
        usage = run_usage(r)
        def render():
            console.print(f"[bold]{r['id']}[/bold]  status=[bold]{r['status']}[/bold]  agent={r['agent']} v{r['agentVersion']}")
            console.print(f"  usage: {usage['inputTokens']}in/{usage['outputTokens']}out tokens"
                          + (f"  cost ${usage['costUsd']}" if usage['costUsd'] is not None else ""))
            t = Table("seq", "kind", "label")
            for e in r["trace"]:
                t.add_row(str(e["seq"]), e["kind"], str(e["label"]))
            console.print(t)
            if r.get("error"):
                console.print(f"[red]error:[/red] {r['error']}")
        emit(json, {"run": r, "usage": usage}, render)


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------
@approvals_app.command("list")
def approvals_list(status: Optional[str] = typer.Option(None, "--status", help="Filter: pending|approved|rejected"),
                   json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, _, store = _store()
        items = store.list_approvals(status)
        summary = [{"id": a["id"], "status": a["status"], "title": a["title"],
                    "runId": a["runId"], "createdAt": a["createdAt"]} for a in items]
        def render():
            t = Table("approval", "status", "title", "run")
            for a in summary:
                t.add_row(a["id"], a["status"], a["title"], a["runId"])
            console.print(t)
        emit(json, {"approvals": summary, "count": len(summary)}, render)


@approvals_app.command("approve")
def approvals_approve(approval_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json"),
                      non_interactive: bool = typer.Option(False, "--non-interactive")):
    """Approve a pending action; the run resumes and the action executes."""
    with guard(json):
        engine = _engine()
        run = engine.approve(approval_id)
        emit(json, {"approvalId": approval_id, "runId": run["id"], "runStatus": run["status"]},
             lambda: console.print(f"[green]✓[/green] approved {approval_id} → run {run['id']} is now [bold]{run['status']}[/bold]"))


@approvals_app.command("reject")
def approvals_reject(approval_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json"),
                     non_interactive: bool = typer.Option(False, "--non-interactive")):
    """Reject a pending action; the run terminates as rejected."""
    with guard(json):
        engine = _engine()
        run = engine.reject(approval_id)
        emit(json, {"approvalId": approval_id, "runId": run["id"], "runStatus": run["status"]},
             lambda: console.print(f"[yellow]✗[/yellow] rejected {approval_id} → run {run['id']} is now [bold]{run['status']}[/bold]"))


# --------------------------------------------------------------------------
# tools / models / channels
# --------------------------------------------------------------------------
@tools_app.command("list")
def tools_list(json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, manifest = _project()
        reg = default_tools()
        items = []
        for t in manifest.tools:
            spec = reg.get(t.id)
            items.append({"id": t.id, "permission": t.permission.value,
                          "registered": spec is not None,
                          "externalSideEffects": spec.external_side_effects if spec else None})
        def render():
            tbl = Table("tool", "permission", "registered")
            for i in items:
                tbl.add_row(i["id"], i["permission"], "yes" if i["registered"] else "no")
            console.print(tbl)
        emit(json, {"tools": items}, render)


@tools_app.command("register")
def tools_register(id: str = typer.Argument(...),
                   permission: str = typer.Option("allowed", "--permission"),
                   json: bool = typer.Option(False, "--json")):
    """Declare a tool in the manifest."""
    with guard(json):
        root, _ = _project()
        raw = _load_manifest_raw(root)
        tools = raw.setdefault("tools", [])
        if any(t.get("id") == id for t in tools):
            raise RyaError("E_VALIDATION", f"Tool '{id}' already declared.", hint="Edit it directly in the manifest.")
        tools.append({"id": id, "permission": permission})
        _write_manifest_raw(root, raw)
        emit(json, {"registered": id, "permission": permission},
             lambda: console.print(f"[green]✓[/green] registered tool {id} ({permission})"))


@models_app.command("list")
def models_list(json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, manifest = _project()
        reg = default_models()
        items = []
        for m in manifest.models:
            spec = reg.get(m.id)
            items.append({"id": m.id, "type": m.type, "permission": m.permission.value,
                          "version": spec.version if spec else None, "registered": spec is not None})
        def render():
            tbl = Table("model", "type", "permission", "version")
            for i in items:
                tbl.add_row(i["id"], i["type"], i["permission"], str(i["version"]))
            console.print(tbl)
        emit(json, {"models": items}, render)


@models_app.command("register")
def models_register(id: str = typer.Argument(...), type: str = typer.Option("external", "--type"),
                    permission: str = typer.Option("allowed", "--permission"),
                    json: bool = typer.Option(False, "--json")):
    """Declare a model in the manifest."""
    with guard(json):
        root, _ = _project()
        raw = _load_manifest_raw(root)
        models = raw.setdefault("models", [])
        if any(m.get("id") == id for m in models):
            raise RyaError("E_VALIDATION", f"Model '{id}' already declared.")
        models.append({"id": id, "type": type, "permission": permission})
        _write_manifest_raw(root, raw)
        emit(json, {"registered": id}, lambda: console.print(f"[green]✓[/green] registered model {id}"))


@channels_app.command("list")
def channels_list(json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, manifest = _project()
        items = [{"type": c.type, "path": c.path, "enabled": c.enabled} for c in manifest.channels]
        def render():
            tbl = Table("channel", "path", "enabled")
            for c in items:
                tbl.add_row(c["type"], c["path"] or "—", str(c["enabled"]))
            console.print(tbl)
        emit(json, {"channels": items}, render)


@channels_app.command("connect")
def channels_connect(type: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Enable a channel in the manifest."""
    with guard(json):
        root, _ = _project()
        raw = _load_manifest_raw(root)
        channels = raw.setdefault("channels", [])
        found = next((c for c in channels if c.get("type") == type), None)
        if found is None:
            found = {"type": type, "enabled": True}
            channels.append(found)
        else:
            found["enabled"] = True
        _write_manifest_raw(root, raw)
        emit(json, {"connected": type}, lambda: console.print(f"[green]✓[/green] channel {type} enabled"))


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
@secrets_app.command("set")
def secrets_set(key: str = typer.Argument(...), value: str = typer.Argument(...),
                json: bool = typer.Option(False, "--json")):
    """Set a secret in .env (value never echoed back)."""
    with guard(json):
        root, _ = _project()
        env_path = root / ".env"
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        out, replaced = [], False
        for line in lines:
            if line.split("=", 1)[0].strip() == key:
                out.append(f"{key}={value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{key}={value}")
        env_path.write_text("\n".join(out) + "\n")
        emit(json, {"set": key}, lambda: console.print(f"[green]✓[/green] set secret {key}"))


@secrets_app.command("list")
def secrets_list(json: bool = typer.Option(False, "--json")):
    """List secret names (never values)."""
    with guard(json):
        root, _ = _project()
        names = sorted(load_env(root).keys())
        # Only surface names that look like project secrets (defined in .env).
        env_path = root / ".env"
        defined = []
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    defined.append(line.split("=", 1)[0].strip())
        emit(json, {"secrets": defined}, lambda: console.print("\n".join(defined) or "(none)"))


# --------------------------------------------------------------------------
# schedules / jobs
# --------------------------------------------------------------------------
@schedules_app.command("list")
def schedules_list(json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, manifest = _project()
        items = [t.model_dump() for t in manifest.triggers if t.type == "cron"]
        def render():
            tbl = Table("id", "schedule", "handler")
            for s in items:
                tbl.add_row(s["id"], s.get("schedule") or "—", s["handler"])
            console.print(tbl)
        emit(json, {"schedules": items}, render)


@schedules_app.command("create")
def schedules_create(id: str = typer.Argument(...), schedule: str = typer.Option(..., "--schedule"),
                     handler: str = typer.Option(..., "--handler"), json: bool = typer.Option(False, "--json")):
    """Add a cron trigger to the manifest."""
    with guard(json):
        root, _ = _project()
        raw = _load_manifest_raw(root)
        triggers = raw.setdefault("triggers", [])
        if any(t.get("id") == id for t in triggers):
            raise RyaError("E_VALIDATION", f"Trigger '{id}' already exists.")
        triggers.append({"id": id, "type": "cron", "schedule": schedule, "handler": handler})
        _write_manifest_raw(root, raw)
        emit(json, {"created": id, "schedule": schedule}, lambda: console.print(f"[green]✓[/green] schedule {id} ({schedule})"))


@schedules_app.command("run")
def schedules_run(id: str = typer.Argument(..., help="Trigger id."), json: bool = typer.Option(False, "--json")):
    """Run a cron trigger once now (simulates the scheduler firing)."""
    with guard(json):
        engine = _engine()
        run = engine.run_cron(id)
        emit(json, {"runId": run["id"], "status": run["status"]},
             lambda: console.print(f"[green]✓[/green] cron {id} → run {run['id']} ({run['status']})"))


@jobs_app.command("list")
def jobs_list(status: Optional[str] = typer.Option(None, "--status"), json: bool = typer.Option(False, "--json")):
    with guard(json):
        _, _, store = _store()
        items = store.list_jobs(status)
        summary = [{"id": j["id"], "handler": j["handler"], "status": j["status"],
                    "runAt": j["runAt"], "attempts": j.get("attempts", 0),
                    "maxAttempts": j.get("maxAttempts", 3), "lastError": j.get("lastError"),
                    "resultRunId": j.get("resultRunId")} for j in items]
        def render():
            tbl = Table("job", "handler", "status", "attempts", "runAt")
            for j in summary:
                tbl.add_row(j["id"], j["handler"], j["status"],
                            f"{j['attempts']}/{j['maxAttempts']}", j["runAt"])
            console.print(tbl)
        emit(json, {"jobs": summary, "count": len(summary)}, render)


@jobs_app.command("run")
def jobs_run(job_id: Optional[str] = typer.Argument(None),
             all: bool = typer.Option(False, "--all", help="Run all pending jobs, ignoring runAt."),
             due: bool = typer.Option(False, "--due", help="Run only pending jobs whose runAt has passed."),
             json: bool = typer.Option(False, "--json")):
    """Run queued jobs. Failed jobs retry with backoff up to maxAttempts; `--due`
    respects the backoff schedule, `--all` forces every pending job."""
    with guard(json):
        engine = _engine()
        if all or due:
            pending = engine.due_jobs() if due else engine.store.list_jobs("pending")
            results = []
            for j in pending:
                r = engine.run_job(j["id"])
                results.append({"jobId": j["id"], "runId": r["id"], "status": r["status"]})
            emit(json, {"ran": results, "count": len(results)},
                 lambda: console.print(f"[green]✓[/green] ran {len(results)} job(s)"))
        elif job_id:
            run = engine.run_job(job_id)
            emit(json, {"jobId": job_id, "runId": run["id"], "status": run["status"]},
                 lambda: console.print(f"[green]✓[/green] job {job_id} → run {run['id']} ({run['status']})"))
        else:
            raise RyaError("E_VALIDATION", "Provide a job id, --due, or --all.", hint="See `rya jobs list`.")


@app.command()
def mcp(json: bool = typer.Option(False, "--json")):
    """Run the Rya MCP server (stdio) so MCP-native coding agents can drive Rya."""
    with guard(json):
        try:
            from ..mcp.server import run as run_server
        except ImportError:
            raise RyaError("E_RUNTIME", "MCP extra not installed.",
                           hint="Install with: pip install 'rya[mcp]'")
        # stdio transport blocks; logs/banner go to stderr to keep stdout clean.
        err_console.print("[green]✓[/green] Rya MCP server starting (stdio)…")
        run_server()


@skills_app.command("install")
def skills_install(
    target_global: bool = typer.Option(False, "--global", help="Install to ~/.claude/skills instead of ./.claude/skills."),
    json: bool = typer.Option(False, "--json"),
):
    """Install the Rya skills (rya = authoring, rya-ops = operating) for coding agents."""
    with guard(json):
        from ..skills import SKILLS
        root = (Path.home() if target_global else Path.cwd()) / ".claude" / "skills"
        installed = []
        for name, content in SKILLS.items():
            base = root / name
            base.mkdir(parents=True, exist_ok=True)
            dest = base / "SKILL.md"
            dest.write_text(content)
            installed.append(str(dest))
        emit(json, {"installed": installed, "scope": "global" if target_global else "project"},
             lambda: [console.print(f"[green]✓[/green] installed skill → {p}") for p in installed] and None)


@skills_app.command("path")
def skills_path(json: bool = typer.Option(False, "--json")):
    """Print where the skill would be installed (project + global)."""
    with guard(json):
        proj = Path.cwd() / ".claude" / "skills" / "rya" / "SKILL.md"
        glob = Path.home() / ".claude" / "skills" / "rya" / "SKILL.md"
        emit(json, {"project": str(proj), "global": str(glob)},
             lambda: console.print(f"project: {proj}\nglobal:  {glob}"))


def _tenancy():
    import os
    dsn = os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RyaError("E_VALIDATION", "Multi-tenancy requires Postgres.",
                       hint="Set RYA_DATABASE_URL to a Postgres connection string.")
    from ..tenancy import Tenancy
    t = Tenancy(dsn)
    t.setup()  # idempotent: tables + rya_app role + RLS policies
    return t


@workspaces_app.command("create")
def workspaces_create(name: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Create a tenant workspace."""
    with guard(json):
        ws = _tenancy().create_workspace(name)
        emit(json, {"workspace": ws}, lambda: console.print(f"[green]✓[/green] workspace [bold]{ws['id']}[/bold] ({name})"))


@workspaces_app.command("list")
def workspaces_list(json: bool = typer.Option(False, "--json")):
    """List tenant workspaces."""
    with guard(json):
        items = _tenancy().list_workspaces()
        def render():
            t = Table("id", "name", "created")
            for w in items:
                t.add_row(w["id"], w["name"], w["createdAt"])
            console.print(t)
        emit(json, {"workspaces": items}, render)


@keys_app.command("create")
def keys_create(workspace: str = typer.Option(..., "--workspace", help="Workspace id."),
                label: str = typer.Option("", "--label"), json: bool = typer.Option(False, "--json")):
    """Create an API key for a workspace. The plaintext key is shown ONCE."""
    with guard(json):
        rec = _tenancy().create_api_key(workspace, label)
        emit(json, {"id": rec["id"], "workspaceId": rec["workspaceId"], "key": rec["key"],
                    "note": "Store this key now — it is not retrievable later."},
             lambda: (console.print(f"[green]✓[/green] key for {workspace}:"),
                      console.print(f"  [bold]{rec['key']}[/bold]"),
                      console.print("  [dim]store it now — not retrievable later[/dim]")))


@jobs_app.command("dlq")
def jobs_dlq(json: bool = typer.Option(False, "--json")):
    """List dead-lettered jobs (exhausted all retries)."""
    with guard(json):
        engine = _engine()
        items = [{"id": j["id"], "handler": j["handler"], "attempts": j.get("attempts"),
                  "lastError": j.get("lastError")} for j in engine.dead_letter()]
        def render():
            tbl = Table("job", "handler", "attempts", "lastError")
            for j in items:
                tbl.add_row(j["id"], j["handler"], str(j["attempts"]), str(j["lastError"])[:60])
            console.print(tbl)
        emit(json, {"deadLetter": items, "count": len(items)}, render)


@jobs_app.command("retry")
def jobs_retry(job_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Requeue a dead-lettered job (reset attempts, make it due now)."""
    with guard(json):
        engine = _engine()
        job = engine.retry_job(job_id)
        emit(json, {"jobId": job_id, "status": job["status"], "attempts": job["attempts"]},
             lambda: console.print(f"[green]✓[/green] requeued {job_id} (run it with `rya jobs run --due`)"))


@app.command()
def worker(once: bool = typer.Option(False, "--once", help="Drain due jobs once and exit."),
           interval: int = typer.Option(2, "--interval", help="Poll seconds between drains."),
           max_iterations: Optional[int] = typer.Option(None, "--max-iterations", help="Stop after N polls."),
           json: bool = typer.Option(False, "--json")):
    """Run a background worker that claims and executes due jobs. Run several
    concurrently for horizontal throughput — claims are atomic on Postgres."""
    with guard(json):
        import time as _time
        engine = _engine()
        if once:
            ran = engine.work_once()
            emit(json, {"ran": ran, "count": len(ran)},
                 lambda: console.print(f"[green]✓[/green] drained {len(ran)} job(s)"))
            return
        if not json:
            console.print(f"[green]✓[/green] worker polling every {interval}s (Ctrl-C to stop)")
        i = 0
        while True:
            ran = engine.work_once()
            if ran and not json:
                console.print(f"  ran {len(ran)} job(s)")
            i += 1
            if max_iterations and i >= max_iterations:
                break
            _time.sleep(interval)


@app.command()
def serve(host: str = typer.Option("127.0.0.1", "--host"), port: int = typer.Option(8787, "--port"),
          json: bool = typer.Option(False, "--json")):
    """Run the control-plane API (requires the [api] extra)."""
    with guard(json):
        try:
            import uvicorn  # noqa: F401
            from ..api.app import build_app
        except ImportError:
            raise RyaError("E_RUNTIME", "API extra not installed.", hint="Install with: pip install 'rya[api]'")
        import os
        root, manifest = _project()
        auth_on = bool(os.environ.get("RYA_TOKEN"))
        sig_on = bool(os.environ.get("RYA_WEBHOOK_SECRET"))
        info = {"ok": True, "serving": f"http://{host}:{port}", "agent": manifest.name,
                "authEnabled": auth_on, "webhookSignature": sig_on,
                "webhook": f"http://{host}:{port}/inbound"}
        info["console"] = f"http://{host}:{port}/"
        info["websocket"] = f"ws://{host}:{port}/ws"
        if json:
            typer.echo(jsonlib.dumps(info))
        else:
            console.print(f"[green]✓[/green] serving control plane on http://{host}:{port}")
            console.print(f"  console: [bold]http://{host}:{port}/[/bold]")
            console.print(f"  websocket: [bold]ws://{host}:{port}/ws[/bold] (real-time agent channel)")
            console.print(f"  webhook: POST http://{host}:{port}/inbound")
            console.print(f"  auth: {'[green]token required[/green]' if auth_on else '[yellow]OPEN (set RYA_TOKEN to require a token)[/yellow]'}")
            if sig_on:
                console.print("  webhook signature: required (RYA_WEBHOOK_SECRET)")
        import uvicorn
        uvicorn.run(build_app(root), host=host, port=port, log_level="info")


@app.command()
def token(json: bool = typer.Option(False, "--json")):
    """Generate a random operator token. Export it as RYA_TOKEN to enable API auth."""
    with guard(json):
        import secrets as _secrets
        tok = "rya_" + _secrets.token_urlsafe(32)
        emit(json, {"token": tok, "usage": "export RYA_TOKEN=<token>  # then `rya serve`"},
             lambda: (console.print(tok),
                      console.print("[dim]export RYA_TOKEN to require it on the API[/dim]")))


if __name__ == "__main__":  # pragma: no cover
    app()
