"""Rya CLI.

Agent-friendly by design: every command takes ``--json`` for machine-readable
output and ``--non-interactive`` to forbid hidden prompts, errors carry stable
codes + a suggested next action, and exit codes are semantic (see errors.py).
"""

from __future__ import annotations

import json as jsonlib
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

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
from ..config import current_environment

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Rya — production backend/runtime for AI agents.")
console = Console()
err_console = Console(stderr=True)

# Sub-apps
agents_app = typer.Typer(no_args_is_help=True, help="Inspect agents.")
events_app = typer.Typer(no_args_is_help=True, help="Send events into the runtime.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect runs and traces.")
versions_app = typer.Typer(no_args_is_help=True, help="Immutable, content-hashed deployment versions.")
envs_app = typer.Typer(no_args_is_help=True, help="Environments and their current-version pointers.")
gate_app = typer.Typer(no_args_is_help=True, help="Promotion gates: readiness/eval admission checks per environment.")
quotas_app = typer.Typer(no_args_is_help=True, help="Per-workspace resource quotas (runs, tokens, cost, workers).")
approvals_app = typer.Typer(no_args_is_help=True, help="List/approve/reject human approvals.")
tools_app = typer.Typer(no_args_is_help=True, help="Tool registry.")
models_app = typer.Typer(no_args_is_help=True, help="Model registry.")
channels_app = typer.Typer(no_args_is_help=True, help="Channels.")
secrets_app = typer.Typer(no_args_is_help=True, help="Secrets (metadata only).")
schedules_app = typer.Typer(no_args_is_help=True, help="Cron schedules.")
jobs_app = typer.Typer(no_args_is_help=True, help="Background jobs.")
skills_app = typer.Typer(no_args_is_help=True, help="Install the Rya coding-agent skill.")
workspaces_app = typer.Typer(no_args_is_help=True, help="Manage tenant workspaces (Postgres/cloud).")
orgs_app = typer.Typer(no_args_is_help=True,
                       help="Organizations: the BILLING boundary above a workspace (D29).")
keys_app = typer.Typer(no_args_is_help=True, help="Manage per-workspace API keys.")
connections_app = typer.Typer(no_args_is_help=True, help="Scoped connected credentials for tools.")
# NOT `keys`, which is already the per-workspace API keys. Two things called "key" in
# one CLI is a support ticket waiting to happen, and the encryption ones are a ring.
keyring_app = typer.Typer(no_args_is_help=True,
                          help="Per-tenant encryption keys: rotate, re-seal, inspect (D18/#13).")
cloud_app = typer.Typer(no_args_is_help=True, help="Drive a hosted Rya (after `rya login`).")

app.add_typer(agents_app, name="agents")
app.add_typer(events_app, name="events")
app.add_typer(runs_app, name="runs")
app.add_typer(versions_app, name="versions")
app.add_typer(envs_app, name="envs")
app.add_typer(gate_app, name="gate")
app.add_typer(quotas_app, name="quotas")
app.add_typer(approvals_app, name="approvals")
app.add_typer(tools_app, name="tools")
app.add_typer(models_app, name="models")
app.add_typer(channels_app, name="channels")
app.add_typer(secrets_app, name="secrets")
app.add_typer(schedules_app, name="schedules")
app.add_typer(jobs_app, name="jobs")
app.add_typer(skills_app, name="skills")
app.add_typer(workspaces_app, name="workspaces")
app.add_typer(orgs_app, name="orgs")
app.add_typer(keys_app, name="keys")
app.add_typer(connections_app, name="connections")
app.add_typer(keyring_app, name="keyring")
app.add_typer(cloud_app, name="cloud")


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


def _admin_store(workspace: str = "") -> tuple[Path, Store]:
    """A store for a named workspace, with **no manifest required**.

    ``_store`` insists on a project because most commands operate on one. The
    tenant-lifecycle and key commands do not: under D21 a deployment serves many
    agents and has no ``rya.agent.yaml`` at all, so demanding one would make
    `rya workspaces purge` unrunnable exactly where it matters. Same resolution the
    `supervisor` command uses, and the same reason.
    """
    from ..agents import project_root as mounted_project
    from ..store import open_worker_store

    path = find_manifest()
    root = mounted_project() or (path.parent if path else Path.cwd())
    if not workspace:
        return root, open_store(root)
    return root, open_worker_store(root, workspace)


def _actor() -> str:
    """Who is running this, for the audit record. Best effort and honest about it.

    A purge and a disable are both recorded, and "unknown" is a worse answer than an
    OS username — but neither is an authenticated identity, so this must not be
    mistaken for one. The api's routes carry a real actor; the CLI carries whoever ran
    it.
    """
    import getpass
    import os

    return (os.environ.get("RYA_ACTOR")
            or (getpass.getuser() if hasattr(getpass, "getuser") else "")
            or "cli")


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
def login(url: Optional[str] = typer.Argument(None, help="Hosted Rya URL, e.g. https://rya.yourco.com. Omit for local."),
          key: Optional[str] = typer.Option(None, "--key", help="Workspace API key (rya_sk_…) or operator token."),
          json: bool = typer.Option(False, "--json")):
    """Point the CLI + your agent at a hosted Rya (or confirm local).

    `rya login https://rya.host --key rya_sk_…` verifies the connection, stores it
    (~/.rya/config.json, 0600), and prints the `.mcp.json` block to connect a
    coding agent's remote MCP to the same instance. With no URL, you're on the
    local runtime (no auth needed).
    """
    with guard(json):
        from ..cloud import RemoteClient, save_cloud_config, mcp_config_snippet
        if not url:
            emit(json, {"mode": "local", "authenticated": True,
                        "message": "Local runtime — no authentication required."},
                 lambda: console.print("[green]✓[/green] Local runtime — no authentication required."))
            return
        info = RemoteClient(url, key).info()  # verifies reachability + auth
        save_cloud_config(url, key)
        snippet = mcp_config_snippet(url)
        out = {"ok": True, "mode": "cloud", "cloudUrl": url.rstrip("/"),
               "agent": info.get("agent"), "remoteMcp": info.get("remoteMcp"),
               "mcpConfig": snippet}

        def render():
            console.print(f"[green]✓[/green] Connected to [bold]{url.rstrip('/')}[/bold] "
                          f"(agent: {info.get('agent')}, v{info.get('version','?')})")
            console.print(f"  remote MCP: [bold]{info.get('remoteMcp')}[/bold]")
            console.print("  add this to your agent's [bold].mcp.json[/bold] to drive it from your editor:")
            console.print(jsonlib.dumps(snippet, indent=2))
        emit(json, out, render)


@app.command()
def logout(json: bool = typer.Option(False, "--json")):
    """Forget the hosted connection and go back to the local runtime."""
    with guard(json):
        from ..cloud import clear_cloud_config
        cleared = clear_cloud_config()
        emit(json, {"ok": True, "cleared": cleared, "mode": "local"},
             lambda: console.print("[green]✓[/green] " + ("Signed out — using the local runtime." if cleared
                                   else "No hosted connection was set (already local).")))


@app.command()
def whoami(json: bool = typer.Option(False, "--json")):
    """Show whether the CLI is pointed at a hosted Rya or the local runtime."""
    with guard(json):
        from ..cloud import load_cloud_config
        cfg = load_cloud_config()
        if cfg:
            emit(json, {"mode": "cloud", "cloudUrl": cfg["cloudUrl"], "hasKey": bool(cfg.get("apiKey"))},
                 lambda: console.print(f"[bold]cloud[/bold] → {cfg['cloudUrl']} "
                                       f"({'key set' if cfg.get('apiKey') else 'no key'})"))
        else:
            emit(json, {"mode": "local"}, lambda: console.print("[bold]local[/bold] runtime (run `rya login <url>` to use a hosted Rya)"))


@app.command()
def create(
    name: str = typer.Argument(..., help="Project / agent name."),
    template: str = typer.Option("minimal", "--template",
                                 help="minimal (default: real seams, no mocked domain data) or demo (full showcase with mocked CRM domain)."),
    json: bool = typer.Option(False, "--json"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
):
    """Scaffold a new agent project in ./<name>."""
    with guard(json):
        target = Path.cwd() / name
        written = scaffold.write_project(target, name, overwrite=force, template=template)
        emit(json, {"name": name, "path": str(target), "template": template, "files": written,
                    "next": [f"cd {name}", "rya dev", "rya events send --type message.received --payload '{\"email\":\"ada@example.com\",\"body\":\"hello\"}'"]},
             lambda: (console.print(f"[green]✓[/green] Created project [bold]{name}[/bold] at {target} ({template} template)"),
                      console.print("  next: [bold]cd " + name + " && rya dev[/bold]")))


@app.command()
def init(json: bool = typer.Option(False, "--json"), force: bool = typer.Option(False, "--force"),
         template: str = typer.Option("minimal", "--template", help="minimal or demo.")):
    """Scaffold a project in the current directory."""
    with guard(json):
        name = Path.cwd().name
        written = scaffold.write_project(Path.cwd(), name, overwrite=force, template=template)
        emit(json, {"name": name, "template": template, "files": written},
             lambda: console.print(f"[green]✓[/green] Initialized Rya project [bold]{name}[/bold] ({len(written)} files)"))


@app.command()
def dev(check: bool = typer.Option(False, "--check",
                                   help="Validate the manifest and agent code, then exit (no processes)."),
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8787, "--port"),
        json: bool = typer.Option(False, "--json")):
    """Run the platform locally — the same two processes as production.

    `rya dev` starts an `api` process and one `worker`, with the working tree as
    the bundle: real journal, real approvals, real permission and pin
    resolution, real guard, and the real queue hand-off between them
    (PLATFORM_DESIGN §10). Two processes on a laptop is a small cost accepted
    for topology parity — the local shape is the production shape, so `rya dev`
    exercises the queue and the turn buffer rather than a simplified path that
    only works locally.

    `rya dev --check` is the instant manifest+code validation that CI and tight
    edit loops depend on; it starts nothing.
    """
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

    if check:
        with guard(json):
            def render():
                console.print(f"[green]✓[/green] [bold]{manifest.name}[/bold] v{manifest.version} ready ({manifest.runtime})")
                console.print(f"  entrypoint: {manifest.entrypoint}")
                console.print(f"  event handler: {'yes' if info['eventHandler'] else '[red]MISSING[/red]'}")
                console.print(f"  jobs: {', '.join(info['jobHandlers']) or '—'}")
                console.print(f"  tools: {', '.join(info['tools']) or '—'}")
                console.print("  send a test event: [bold]rya events send --type message.received --payload '{\"email\":\"ada@example.com\"}'[/bold]")
            emit(json, info, render)
        return

    with guard(json):
        import os as _os
        import signal
        import subprocess
        import sys as _sys

        try:
            import uvicorn
            from ..api.app import build_app
        except ImportError:
            raise RyaError("E_RUNTIME", "API extra not installed.",
                           hint="Install with: pip install 'rya[api]'")

        # The worker is a real second process, exactly as in production. The api
        # is told NOT to execute handler code (§11.7), so the hand-off under test
        # locally is the queue — not an in-process shortcut that only works here.
        env = {**_os.environ, "RYA_API_INLINE_WORKER": "0"}
        worker_proc = subprocess.Popen(
            [_sys.executable, "-m", "rya.cli.main", "worker", "--interval", "1"],
            cwd=str(root), env=env)

        if json:
            typer.echo(jsonlib.dumps({**info, "api": f"http://{host}:{port}",
                                      "workerPid": worker_proc.pid}))
        else:
            console.print(f"[green]✓[/green] [bold]{manifest.name}[/bold] v{manifest.version} "
                          f"({manifest.runtime}) — dev deployment up")
            console.print(f"  api:      http://{host}:{port}   console, /ws, /mcp")
            console.print(f"  worker:   pid {worker_proc.pid} (claims turns + jobs from the queue)")
            console.print(f"  tools:    {', '.join(info['tools']) or '—'}")
            console.print("  Ctrl-C to stop both.")

        _os.environ["RYA_API_INLINE_WORKER"] = "0"
        try:
            uvicorn.run(build_app(root), host=host, port=port, log_level="warning")
        finally:
            worker_proc.send_signal(signal.SIGINT)
            try:
                worker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - shutdown race
                worker_proc.kill()


@app.command()
def deploy(action: Optional[str] = typer.Argument(None, help="aws | status | destroy (omit for readiness/artifacts)"),
           target: str = typer.Option("check", "--target", help="check | docker | fly | render"),
           env: Optional[str] = typer.Option(None, "--env",
                                             help="Deploy a bundle to this environment (dev | staging | prod)."),
           region: str = typer.Option("us-east-1", "--region"),
           stack: Optional[str] = typer.Option(None, "--stack", help="Stack name (default {agent}-live)."),
           count: int = typer.Option(2, "--count", help="Fargate task count."),
           ha: bool = typer.Option(True, "--ha/--no-ha", help="Multi-AZ RDS."),
           skip_build: bool = typer.Option(False, "--skip-build", help="Reuse the last pushed image tag."),
           langfuse: bool = typer.Option(False, "--langfuse", help="Provision in-VPC Langfuse and wire trace export."),
           yes: bool = typer.Option(False, "--yes", help="Skip the destroy confirmation."),
           check: bool = typer.Option(False, "--check", help="Only run the production-readiness check, then exit."),
           force: bool = typer.Option(False, "--force", help="Deploy even if readiness blocks remain."),
           write: bool = typer.Option(True, "--write/--no-write", help="Write deploy artifacts into the project."),
           promote_it: bool = typer.Option(True, "--promote/--no-promote",
                                           help="With --env: also flip the environment's current-version pointer."),
           actor: Optional[str] = typer.Option(None, "--actor", help="Who is deploying (recorded on the version)."),
           metadata: Optional[str] = typer.Option(None, "--metadata",
                                                  help="Provenance as k=v,k=v — e.g. gitSha=abc,ci=run/42."),
           json: bool = typer.Option(False, "--json"),
           non_interactive: bool = typer.Option(False, "--non-interactive")):
    """Ship the agent. Three modes: an action, `--env`, or neither.

    **`rya deploy --env <name>`** is the deployment pipeline (PLATFORM_DESIGN §9):
    validate, bundle the source into an immutable content-hashed version, record
    it, and flip the environment's current-version pointer. Rollback is the same
    flip backwards (`rya rollback`). Readiness is a hard gate here.

    **`rya deploy aws | status | destroy`** stands the platform up in a real AWS
    account (and reports on or tears down that stack).

    **`rya deploy --target docker|fly|render`** is the original meaning: generate
    the artifacts that stand a Rya deployment up in the first place. These are
    different verbs — one ships an agent onto a platform, the others stand the
    platform up — so they stayed one command rather than pretending to be one
    thing.

    `rya deploy --check` runs the readiness checklist and exits non-zero if any
    blocker remains (exit 7) — a coding agent makes this all-green to ship safely.
    """
    if env is not None:
        # Standing infrastructure up and promoting a bundle onto it are separate
        # verbs; combining them would silently do one and drop the other.
        if action is not None:
            with guard(json):
                raise RyaError("E_VALIDATION",
                               f"`rya deploy {action}` and `--env {env}` are different verbs.",
                               hint=f"Stand the stack up with `rya deploy {action}`, then "
                                    f"`rya deploy --env {env}` to promote a bundle onto it.")
        return _deploy_bundle(env=env, promote_it=promote_it, actor=actor,
                              metadata=metadata, force=force, json=json)
    with guard(json):
        if action in ("aws", "status", "destroy"):
            return _deploy_aws_action(action, region, stack, count, ha, skip_build, langfuse, yes, json)
        if action is not None:
            raise RyaError("E_VALIDATION", f"Unknown deploy action '{action}'.",
                           hint="Use: rya deploy aws | status | destroy")
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


def _kv(spec: Optional[str]) -> dict:
    """Parse `k=v,k=v` into a dict — the --metadata provenance slot."""
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _deploy_bundle(*, env: str, promote_it: bool, actor: Optional[str],
                   metadata: Optional[str], force: bool, json: bool):
    """`rya deploy --env <name>` — the §9 pipeline.

        validate manifest + readiness gate locally
        bundle: source + lockfile + manifest + SDK version
        record an immutable, content-hashed version
        promote: set the environment's current version

    Deploys are atomic per environment: the pointer flips once, new runs go to
    the new version, in-flight runs finish on theirs.
    """
    with guard(json):
        from .. import bundles, deployments, gates
        from ..readiness import check_readiness

        root, manifest, store = _store()
        agent = load_agent(manifest, root)  # validates the agent code imports

        # §9: "the readiness gate becomes a server-side admission check rather
        # than a client-side courtesy". Locally it is a hard gate on the way in.
        rep = check_readiness(manifest, store, agent, root)
        if not rep["ready"] and not force:
            raise RyaError(
                "E_NOT_PRODUCTION_READY",
                f"{rep['summary']['blocks']} readiness blocker(s) before deploying to '{env}': "
                + "; ".join(b.get("title", str(b)) for b in rep["blocks"][:5]),
                hint="Fix them, or pass --force to deploy anyway (recorded on the version).",
            )

        bundle = bundles.build_bundle(root)
        # Honour a declared object store: a deployment with RYA_BUNDLES_S3_BUCKET
        # set must upload there, or the worker that later resolves the same store
        # would look in a bucket nothing was ever written to. Falls back to the
        # local content-addressed directory when nothing is declared.
        # D20: into this store's tenant namespace. On the local FileStore that is
        # empty and addressing stays flat, which is what keeps `rya dev` and a
        # single-tenant self-host on the pre-D20 layout.
        archive_store = bundles.resolve_bundle_store(
            root, workspace=bundles.workspace_of(store))
        archive = bundles.store_bundle(bundle, archive_store)

        # Record, attest, THEN promote — in that order, and not via
        # create_version(environment=...). An attestation is filed against a
        # version id (§9, gates.py), so the version must exist before the
        # readiness result can be bound to it, and the attestation must exist
        # before a gate that requires readiness can pass.
        version = deployments.create_version(
            store, agent=manifest.name, bundle=bundle, actor=actor,
            metadata={**_kv(metadata), "readiness": rep["summary"],
                      "forced": bool(force and not rep["ready"])})
        gates.attest_readiness(store, version, rep, actor=actor)

        gate_result = None
        if promote_it:
            gate_result = gates.check_promotion(store, version=version, environment=env,
                                                actor=actor)
            deployments.promote(store, environment=env, agent=manifest.name,
                                version_id=version["id"], actor=actor, force=force)

        data = {"ok": True, "environment": env, "agent": manifest.name,
                "versionId": version["id"], "bundleHash": bundle.hash,
                "fileCount": bundle.fileCount, "sizeBytes": bundle.sizeBytes,
                "sdkVersion": bundle.sdkVersion, "lockfile": bundle.lockfile,
                "archive": str(archive), "promoted": promote_it,
                **({"gate": gate_result.to_dict()} if gate_result else {})}

        def render():
            console.print(f"[green]✓[/green] {manifest.name} → [bold]{env}[/bold]")
            console.print(f"  version: {version['id']}  ({bundle.hash[:12]}…)")
            console.print(f"  bundle:  {bundle.fileCount} files, {bundle.sizeBytes} bytes, "
                          f"sdk {bundle.sdkVersion}")
            if promote_it:
                console.print(f"  promoted — new runs in {env} use this version; "
                              "in-flight runs finish on theirs")
            else:
                console.print(f"  recorded but NOT promoted — `rya promote --env {env} "
                              f"--version {version['id']}` when ready")
        emit(json, data, render)


# Commands DEFINED in the client CLI, re-registered here so they do not disappear
# from `rya-server`.
#
# Both distributions own the `rya` console script, and the recommended local dev
# loop is an editable install of this one — which replaces the thin SDK and
# repoints the script at this module. A command that lived only in
# `cli/client.py` would vanish the moment a developer dev-linked their agent repo
# against a local checkout, which is precisely when they are running it most.
#
#   publish  the §9 pipeline over HTTP, for a repo with no database or bucket
#            access. `deploy --env` is the same pipeline run locally.
#   check    manifest + handler set. `dev --check` is the operator equivalent, but
#            it starts a server; this one starts nothing and is what CI runs.
#
# main.py -> client.py is platform -> SDK, the direction the boundary test allows.
from .client import check as _check_cmd  # noqa: E402
from .client import publish as _publish_cmd  # noqa: E402

app.command(name="publish")(_publish_cmd)
app.command(name="check")(_check_cmd)


@app.command()
def promote(env: str = typer.Option(..., "--env", help="Environment to point at this version."),
            version: str = typer.Option(..., "--version", help="Version id to promote."),
            actor: Optional[str] = typer.Option(None, "--actor"),
            force: bool = typer.Option(False, "--force",
                                       help="Override the promotion gate (recorded against the version)."),
            json: bool = typer.Option(False, "--json")):
    """Flip an environment's current-version pointer (§9). Atomic per
    environment: in-flight runs finish on the version they were pinned to.

    Refused with E_PROMOTION_BLOCKED if the environment's promotion gate is not
    satisfied — see `rya gate show --env <name>`."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rec = deployments.promote(store, environment=env, agent=manifest.name,
                                  version_id=version, actor=actor, force=force)
        emit(json, rec, lambda: console.print(
            f"[green]✓[/green] {env} → {rec['currentVersionId']}"))


@app.command()
def rollback(env: str = typer.Option(..., "--env"),
             version: Optional[str] = typer.Option(None, "--version",
                                                   help="Land on a specific version instead of the previous one."),
             actor: Optional[str] = typer.Option(None, "--actor"),
             json: bool = typer.Option(False, "--json")):
    """Roll an environment back. §9: "Rollback is a pointer flip." """
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rec = deployments.rollback(store, environment=env, agent=manifest.name,
                                   actor=actor, to_version_id=version)
        emit(json, rec, lambda: console.print(
            f"[green]✓[/green] {env} rolled back to {rec['currentVersionId']}"))


@versions_app.command("list")
def versions_list(state: Optional[str] = typer.Option(None, "--state", help="active | retired"),
                  json: bool = typer.Option(False, "--json")):
    """Immutable, content-hashed versions of this agent, newest first."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rows = deployments.list_versions(store, agent=manifest.name, state=state)

        def render():
            if not rows:
                console.print("[dim]no versions yet — `rya deploy --env dev`[/dim]")
            for v in rows:
                console.print(f"  {v['id']}  {v['bundleHash'][:12]}…  {v['state']:8}  "
                              f"{v.get('createdAt', '')}  {v.get('manifestVersion') or ''}")
        emit(json, {"versions": rows, "count": len(rows)}, render)


@versions_app.command("retire")
def versions_retire(version_id: str = typer.Argument(...),
                    force: bool = typer.Option(False, "--force",
                                               help="Retire even with runs pinned to it (their replay may fail closed)."),
                    json: bool = typer.Option(False, "--json")):
    """Retire a version. Fails closed while any run is still pinned to it (D12):
    replay is only sound against the code that wrote the journal."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rec = deployments.retire(store, version_id, force=force)
        emit(json, rec, lambda: console.print(f"[green]✓[/green] retired {version_id}"))


@versions_app.command("pinned")
def versions_pinned(version_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Runs still pinned to a version — the reason a retire was refused."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        runs = deployments.pinned_runs(store, version_id)
        emit(json, {"runs": runs, "count": len(runs)},
             lambda: console.print(f"{len(runs)} run(s) pinned to {version_id}"))


@envs_app.command("list")
def envs_list(json: bool = typer.Option(False, "--json")):
    """Environments and the version each currently points at."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rows = deployments.list_environments(store, agent=manifest.name)

        def render():
            if not rows:
                console.print("[dim]nothing deployed yet — `rya deploy --env dev`[/dim]")
            for e in rows:
                console.print(f"  {e['name']:10} → {e.get('currentVersionId') or '(none)'}"
                              f"   updated {e.get('updatedAt', '')}")
        emit(json, {"environments": rows}, render)


@envs_app.command("show")
def envs_show(env: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """One environment in full: current version, history, and which older
    versions are retained because runs are still pinned to them."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        data = deployments.describe_environment(store, env, manifest.name)
        emit(json, data, lambda: console.print(data))


@envs_app.command("history")
def envs_history(env: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """The promote/rollback audit trail for an environment, newest first."""
    with guard(json):
        from .. import deployments
        root, manifest, store = _store()
        rows = deployments.history(store, env, manifest.name)
        emit(json, {"history": rows},
             lambda: [console.print(f"  {h.get('replacedAt') or h.get('updatedAt', '')}  "
                                    f"{h.get('versionId')}  {h.get('actor') or ''}") for h in rows])


@gate_app.command("show")
def gate_show(env: Optional[str] = typer.Option(None, "--env", help="Show the gate for one environment."),
              json: bool = typer.Option(False, "--json")):
    """The promotion gate: what an environment requires before it accepts a version.

    §9's admission check. With no --env, shows every environment that has a gate
    plus the default."""
    with guard(json):
        from .. import deployments, gates
        root, manifest, store = _store()
        names = [env] if env else sorted(
            {e["name"] for e in deployments.list_environments(store, agent=manifest.name)}
            | set((gates.gate_policy(store, manifest.name) or {}).get("environments") or {}))
        rows = [gates.resolve_gate(store, name, agent=manifest.name).describe()
                for name in names]
        data = {"gates": rows,
                "default": gates.resolve_gate(store, "default", agent=manifest.name).describe()}

        def render():
            if not rows:
                console.print("[dim]no gates configured — every environment accepts any version.\n"
                              "  `rya gate set --env prod --require-readiness --require-evals`[/dim]")
            for g in rows:
                marks = [k for k in ("requireReadiness", "requireEvals", "requireActor") if g[k]]
                if g["requireProvenance"]:
                    marks.append("provenance=" + ",".join(g["requireProvenance"]))
                console.print(f"  {g['environment']:10} "
                              + ("[green]" + " ".join(marks) + "[/green]" if marks
                                 else "[dim]unenforced[/dim]"))
        emit(json, data, render)


@gate_app.command("set")
def gate_set(env: Optional[str] = typer.Option(None, "--env",
                                               help="Environment to gate. Omit to set the default for all."),
             require_readiness: Optional[bool] = typer.Option(None, "--require-readiness/--no-require-readiness"),
             require_evals: Optional[bool] = typer.Option(None, "--require-evals/--no-require-evals"),
             min_eval_score: Optional[float] = typer.Option(None, "--min-eval-score",
                                                            help="Minimum eval pass rate, 0..1."),
             allow_warnings: Optional[bool] = typer.Option(None, "--allow-warnings/--no-allow-warnings"),
             require_actor: Optional[bool] = typer.Option(None, "--require-actor/--no-require-actor"),
             provenance: Optional[str] = typer.Option(None, "--require-provenance",
                                                      help="Comma-separated metadata keys, e.g. gitSha,ciRunUrl."),
             actor: Optional[str] = typer.Option(None, "--actor", help="Who is changing the gate."),
             json: bool = typer.Option(False, "--json")):
    """Configure what an environment requires before it will accept a promotion.

    Merges into the existing policy rather than replacing it, so tightening one
    requirement does not silently drop the others. Every change lands in the
    append-only policy log (§12 risk 7: "who reviewed this" is a feature)."""
    with guard(json):
        from .. import gates
        root, manifest, store = _store()
        # D28: read through the fallback so `gate set` on a workspace that
        # predates agent-qualified keys TIGHTENS the inherited policy instead of
        # starting from empty and silently dropping the other requirements.
        policy = dict(gates.gate_policy(store, manifest.name) or {})
        target = "default" if env is None else env
        if env is None:
            spec = dict(policy.get("default") or {})
        else:
            environments = dict(policy.get("environments") or {})
            spec = dict(environments.get(env) or {})

        for wire, value in (("requireReadiness", require_readiness),
                            ("requireEvals", require_evals),
                            ("minEvalScore", min_eval_score),
                            ("allowWarnings", allow_warnings),
                            ("requireActor", require_actor)):
            if value is not None:
                spec[wire] = value
        if provenance is not None:
            keys = [k.strip() for k in provenance.split(",") if k.strip()]
            spec["requireProvenance"] = keys

        if env is None:
            policy["default"] = spec
        else:
            environments = dict(policy.get("environments") or {})
            environments[env] = spec
            policy["environments"] = environments

        gates.set_gate(store, policy, actor=actor, agent=manifest.name)
        resolved = gates.resolve_gate(store, target, agent=manifest.name).describe()
        emit(json, {"ok": True, "gate": resolved}, lambda: console.print(
            f"[green]✓[/green] gate for [bold]{target}[/bold]: "
            + (", ".join(k for k in ("requireReadiness", "requireEvals", "requireActor")
                         if resolved[k]) or "unenforced")))


@gate_app.command("clear")
def gate_clear(actor: Optional[str] = typer.Option(None, "--actor"),
               json: bool = typer.Option(False, "--json")):
    """Remove all promotion gates. Recorded in the policy log."""
    with guard(json):
        from .. import gates
        root, manifest, store = _store()
        gates.set_gate(store, None, actor=actor, agent=manifest.name)
        emit(json, {"ok": True, "cleared": True},
             lambda: console.print("[yellow]gates cleared[/yellow] — every environment now "
                                   "accepts any version."))


@gate_app.command("check")
def gate_check(env: str = typer.Option(..., "--env"),
               version: Optional[str] = typer.Option(None, "--version",
                                                     help="Defaults to the environment's current version."),
               actor: Optional[str] = typer.Option(None, "--actor"),
               json: bool = typer.Option(False, "--json")):
    """Dry-run the gate: would this version be admitted, and if not, what is missing?

    Exits 7 when blocked, so CI can gate on it without parsing prose."""
    with guard(json):
        from .. import deployments, gates
        root, manifest, store = _store()
        if version:
            rec = store.version_get(version)
            if rec is None:
                raise RyaError("E_VERSION_NOT_FOUND", f"No deployment version '{version}'.",
                               hint="`rya versions list --json`")
        else:
            rec = deployments.current_version(store, env, manifest.name)
            if rec is None:
                raise RyaError("E_ENVIRONMENT_NOT_FOUND",
                               f"Nothing is promoted to '{env}' yet, and no --version was given.",
                               hint=f"Pass --version <id>, or `rya deploy --env {env}`.")
        result = gates.check_promotion(store, version=rec, environment=env, actor=actor)

        def render():
            head = "[green]✓ admitted[/green]" if result.allowed else "[red]✗ blocked[/red]"
            console.print(f"{head}  {rec['id']} → {env}")
            for c in result.checks:
                mark = "[green]✓[/green]" if c["ok"] else "[red]•[/red]"
                console.print(f"  {mark} {c['check']}: {c['detail']}")
                if not c["ok"] and c["fix"]:
                    console.print(f"      [dim]fix:[/dim] {c['fix']}")
        emit(json, {"versionId": rec["id"], **result.to_dict()}, render)
        raise typer.Exit(EXIT_OK if result.allowed else 7)


@quotas_app.command("show")
def quotas_show(json: bool = typer.Option(False, "--json")):
    """This workspace's limits and what it is consuming right now (§11.12)."""
    with guard(json):
        from .. import quotas
        root, manifest, store = _store()
        policy = quotas.resolve_quota(store)
        usage = quotas.usage_snapshot(store)
        verdict = quotas.check_admission(store, kind="any", usage=usage)
        data = {"quota": policy.describe(), "usage": usage,
                "violations": verdict.violations}

        def render():
            if not policy.enforced:
                console.print("[dim]no quota configured — this workspace is unlimited.\n"
                              "  `rya quotas set --max-concurrent-runs 10 "
                              "--max-cost-usd-per-day 25`[/dim]")
            rows = [("concurrent runs", usage.get("concurrentRuns"), policy.max_concurrent_runs),
                    ("runs today", usage.get("runsToday"), policy.max_runs_per_day),
                    ("queue depth", usage.get("queueDepth"), policy.max_queue_depth),
                    ("tokens today", usage.get("tokensToday"), policy.max_tokens_per_day),
                    ("USD today", usage.get("costUsdToday"), policy.max_cost_usd_per_day),
                    ("workers", usage.get("workers"), policy.max_workers)]
            for label, current, limit in rows:
                cap = "∞" if limit is None else str(limit)
                over = limit is not None and (current or 0) >= limit
                colour = "red" if over else "green" if limit is not None else "dim"
                console.print(f"  [{colour}]{label:18} {current}/{cap}[/{colour}]")
        emit(json, data, render)


@quotas_app.command("set")
def quotas_set(max_concurrent_runs: Optional[int] = typer.Option(None, "--max-concurrent-runs"),
               max_runs_per_day: Optional[int] = typer.Option(None, "--max-runs-per-day"),
               max_queue_depth: Optional[int] = typer.Option(None, "--max-queue-depth"),
               max_tokens_per_day: Optional[int] = typer.Option(None, "--max-tokens-per-day"),
               max_cost_usd_per_day: Optional[float] = typer.Option(None, "--max-cost-usd-per-day"),
               max_workers: Optional[int] = typer.Option(None, "--max-workers"),
               actor: Optional[str] = typer.Option(None, "--actor"),
               json: bool = typer.Option(False, "--json")):
    """Set this workspace's limits. Merges into the existing quota.

    Note this is the OPERATOR's command. Over the API the same write requires the
    admin token in multi-tenant mode — a tenant that can raise its own quota does
    not have one."""
    with guard(json):
        from .. import quotas
        root, manifest, store = _store()
        policy = dict(store.policy_get(quotas.POLICY_KEY) or {})
        for wire, value in (("maxConcurrentRuns", max_concurrent_runs),
                            ("maxRunsPerDay", max_runs_per_day),
                            ("maxQueueDepth", max_queue_depth),
                            ("maxTokensPerDay", max_tokens_per_day),
                            ("maxCostUsdPerDay", max_cost_usd_per_day),
                            ("maxWorkers", max_workers)):
            if value is not None:
                policy[wire] = value
        quotas.set_quota(store, policy, actor=actor)
        resolved = quotas.resolve_quota(store).describe()
        emit(json, {"ok": True, "quota": resolved},
             lambda: console.print(f"[green]✓[/green] quota updated: "
                                   + ", ".join(f"{k}={v}" for k, v in resolved.items()
                                               if v is not None and k not in ("enforced", "source"))))


@quotas_app.command("clear")
def quotas_clear(actor: Optional[str] = typer.Option(None, "--actor"),
                 json: bool = typer.Option(False, "--json")):
    """Remove all limits for this workspace. Recorded in the policy log."""
    with guard(json):
        from .. import quotas
        root, manifest, store = _store()
        quotas.set_quota(store, None, actor=actor)
        emit(json, {"ok": True, "cleared": True},
             lambda: console.print("[yellow]quota cleared[/yellow] — this workspace is unlimited."))


@app.command()
def bundle(json: bool = typer.Option(False, "--json")):
    """Build the bundle and print its content hash without recording anything.

    The CI diffing primitive: identical source always produces an identical
    hash, so "has anything actually changed" is one command (D12)."""
    with guard(json):
        from .. import bundles
        root, manifest = _project()
        b = bundles.build_bundle(root)
        emit(json, b.to_dict(), lambda: console.print(
            f"{b.hash}  {b.fileCount} files  {b.sizeBytes} bytes  sdk {b.sdkVersion}"))


def _deploy_aws_action(action, region, stack, count, ha, skip_build, langfuse, yes, json_mode):
    from .. import deploy_aws as dx
    root, manifest = _project()
    stack = stack or f"{manifest.name}-live"
    log = (lambda m: None) if json_mode else (lambda m: console.print(f"  [dim]{m}[/dim]"))

    if action == "status":
        state = dx.load_state(root)
        if not state:
            emit(json_mode, {"deployed": False},
                 lambda: console.print("[yellow]no deployment recorded[/yellow] - run `rya deploy aws`."))
            return
        emit(json_mode, {"deployed": True, **state},
             lambda: console.print(f"[green]{state['stack']}[/green] ({state['region']}) - {state.get('url')}"))
        return

    if action == "destroy":
        state = dx.load_state(root) or {}
        lf_stack = (state.get("langfuse") or {}).get("stack")
        what = f"stacks {stack} + {lf_stack}" if lf_stack else f"stack {stack}"
        if not yes and not typer.confirm(f"Delete {what} and ALL their data?"):
            raise typer.Exit(0)
        if lf_stack:
            dx.destroy(lf_stack, region, log)
        dx.destroy(stack, region, log)
        (root / dx.STATE_FILE).unlink(missing_ok=True)
        emit(json_mode, {"destroyed": stack},
             lambda: console.print(f"[green]destroyed[/green] {stack}"))
        return

    # ---- rya deploy aws ----
    console.print(f"[bold]rya deploy aws[/bold] - {manifest.name} -> {stack} ({region})")
    pf = dx.preflight(root, manifest, region, log)
    image = dx.build_and_push(root, manifest.name, pf["account"], region, log,
                              skip_build=skip_build)
    net = dx.discover_network(region, log)
    prior = dx.load_state(root) or {}
    lf_info = None
    extra = None
    if langfuse or prior.get("langfuse"):
        lf_stack = (prior.get("langfuse") or {}).get("stack") or f"{stack}-langfuse"
        lf_info = dx.deploy_langfuse(lf_stack, region, net, log,
                                     prior=prior.get("langfuse"),
                                     persist=lambda inf: dx.save_state(root, dict(prior, langfuse=inf)))
        lf_info["stack"] = lf_stack
        if langfuse:  # (re)wire the app stack to it explicitly
            extra = {"LangfuseHost": lf_info["url"],
                     "LangfusePublicKey": lf_info["public_key"],
                     "LangfuseSecretKey": lf_info["secret_key"]}
    outputs = dx.deploy_stack(stack, region, image, net, log, count=count, multi_az=ha,
                              extra_params=extra)
    url = outputs.get("AlbUrl", "")
    if url:
        dx.smoke(url, log)
    state = {"stack": stack, "region": region, "url": url, "image": image}
    if lf_info:
        state["langfuse"] = lf_info
    dx.save_state(root, state)
    emit(json_mode, state, lambda: (
        console.print(f"\n[green]LIVE[/green] {url}"),
        console.print(f"  app:     {url}/app/"),
        console.print(f"  console: {url}/console"),
        console.print(f"  langfuse: {state['langfuse']['url']} "
                      f"(login {state['langfuse']['admin_email']}, "
                      f"password in .rya/deploy.json)") if lf_info else None,
        console.print("  first user: open the app and sign up - the first account owns the workspace"),
        console.print("  [dim]note: HTTP - front with CloudFront+ACM before real users[/dim]")))


@app.command(name="doctor")
def doctor_cmd(json: bool = typer.Option(False, "--json")):
    """Static durable-execution checks: flags raw IO inside replayed handlers."""
    with guard(json):
        from ..doctor import lint_replay
        root, manifest = _project()
        findings = lint_replay(root / manifest.entrypoint)
        rep = {"ok": not findings, "findings": findings}

        def render():
            if not findings:
                console.print("[green]OK[/green] no replay-discipline issues found in handlers.")
                return
            console.print(f"[yellow]{len(findings)} replay-discipline issue(s):[/yellow]")
            for f in findings:
                console.print(f"  [red]line {f['line']}[/red] in [bold]{f['handler']}[/bold]: "
                              f"{f['call']} - {f['hint']}")

        emit(json, rep, render)
        if findings:
            raise typer.Exit(6)


@app.command(name="eval")
def eval_cmd(
    id: Optional[str] = typer.Option(None, "--id", help="Run only this eval case."),
    langfuse_dataset: Optional[str] = typer.Option(
        None, "--langfuse-dataset",
        help="Pull this Langfuse dataset and run the agent over every item, "
             "recording the results as a Langfuse dataset run."),
    run_name: Optional[str] = typer.Option(
        None, "--run-name", help="Name for the Langfuse dataset run (default rya-<agent>-<ts>)."),
    payload_defaults: Optional[str] = typer.Option(
        None, "--payload-defaults", help="JSON merged under each item's payload, "
        'e.g. \'{"email":"counsellor@csa.test"}\'.'),
    trigger_type: str = typer.Option(
        "message.received", "--trigger-type", help="Event type to fire per dataset item."),
    attest: bool = typer.Option(False, "--attest",
                                help="Record the result against a deployment version, so an "
                                     "eval-gated environment will accept it (§9)."),
    version: Optional[str] = typer.Option(None, "--version",
                                          help="Version to attest against. Defaults to the version "
                                               "whose bundle hash matches the working tree."),
    actor: Optional[str] = typer.Option(None, "--actor", help="Who ran the evals."),
    json: bool = typer.Option(False, "--json"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
):
    """Run the declarative eval suite (rya.evals.yaml) against the runtime.

    Each case fires a real event and scores the run against expectations. Exits
    non-zero (5) if any case fails — gate a deploy on it like `rya deploy --check`.

    With --langfuse-dataset, instead pull that dataset's items from Langfuse, run
    the agent over each, and link every run's trace to its dataset item as a
    Langfuse dataset run (Datasets → runs). An item may carry a Rya `expect` block
    under its metadata to be scored the same way local eval cases are.
    """
    with guard(json):
        root, manifest = _project()
        agent = load_agent(manifest, root)
        store = open_store(root)

        if langfuse_dataset:
            from ..evals import run_langfuse_dataset
            defaults = jsonlib.loads(payload_defaults) if payload_defaults else None
            rep = run_langfuse_dataset(manifest, agent, store, root, langfuse_dataset,
                                       run_name=run_name, trigger_type=trigger_type,
                                       payload_defaults=defaults)

            def render_ds():
                if not rep["hasItems"]:
                    console.print(f"[yellow]no items[/yellow] — dataset '{langfuse_dataset}' "
                                  "is empty or not found in Langfuse.")
                    return
                head = "[green]✓[/green]" if rep["ok"] else "[red]✗[/red]"
                console.print(f"{head} dataset [bold]{rep['dataset']}[/bold]: "
                              f"{rep['passed']}/{rep['total']} passed (score {rep['score']})")
                console.print(f"  [dim]run '{rep['runName']}' — traces linked in Langfuse "
                              "(Datasets → runs)[/dim]")
                for r in rep["results"]:
                    g = "[green]✓[/green]" if r["pass"] else "[red]✗[/red]"
                    console.print(f"  {g} [bold]{r['itemId']}[/bold]  [dim]{r['status']} · {r['runId']}[/dim]")
                    for c in r["checks"]:
                        if not c["pass"]:
                            console.print(f"      [red]✗[/red] {c['check']}: {c['detail']}")
                    if r.get("error"):
                        console.print(f"      [red]error:[/red] {r['error']}")

            emit(json, rep, render_ds)
            if rep["hasItems"] and not rep["ok"]:
                raise typer.Exit(5)
            return

        from ..evals import run_evals
        rep = run_evals(manifest, agent, store, root, only=id)

        def render():
            if not rep["hasEvals"]:
                console.print("[yellow]no evals[/yellow] — create rya.evals.yaml (rya create scaffolds one).")
                return
            head = "[green]✓[/green]" if rep["ok"] else "[red]✗[/red]"
            console.print(f"{head} evals: {rep['passed']}/{rep['total']} passed "
                          f"(score {rep['score']})")
            if rep.get("langfuse"):
                console.print("  [dim]traces + scores exported to Langfuse[/dim]")
            for r in rep["results"]:
                g = "[green]✓[/green]" if r["pass"] else "[red]✗[/red]"
                console.print(f"  {g} [bold]{r['id']}[/bold]  [dim]{r['status']} · {r['runId']}[/dim]")
                for c in r["checks"]:
                    if not c["pass"]:
                        console.print(f"      [red]✗[/red] {c['check']}: {c['detail']}")
                if r.get("error"):
                    console.print(f"      [red]error:[/red] {r['error']}")

        # §9: "evals can gate promotion between staging and prod". The result is
        # filed against a VERSION, because a gate satisfied by an eval run against
        # some other tree is not a gate (see gates.py).
        attested = None
        if attest:
            from .. import bundles, gates
            target = version
            if target is None:
                # Default to the version matching the working tree's content, so
                # `rya deploy` then `rya eval --attest` needs no id copied by hand.
                b = bundles.build_bundle(root)
                rec = store.version_by_hash(manifest.name, b.hash)
                if rec is None:
                    raise RyaError(
                        "E_VERSION_NOT_FOUND",
                        f"No recorded version matches this working tree ({b.hash[:12]}…).",
                        hint="Record it first with `rya deploy --env <env> --no-promote`, or pass "
                        "--version <id> to attest against a specific version.")
            else:
                rec = store.version_get(target)
                if rec is None:
                    raise RyaError("E_VERSION_NOT_FOUND", f"No deployment version '{target}'.",
                                   hint="`rya versions list --json`")
            attested = gates.attest_evals(store, rec, rep, actor=actor)

        emit(json, {**rep, **({"attestation": attested} if attested else {})}, render)
        if rep["hasEvals"] and not rep["ok"]:
            raise typer.Exit(5)


def _remote():
    from ..cloud import RemoteClient, load_cloud_config
    cfg = load_cloud_config()
    if not cfg:
        raise RyaError("E_NOT_LOGGED_IN", "Not connected to a hosted Rya.",
                       hint="Run `rya login <url> --key …` first (or set RYA_REMOTE_URL).")
    return RemoteClient(cfg["cloudUrl"], cfg.get("apiKey")), cfg["cloudUrl"]


@cloud_app.command("info")
def cloud_info(json: bool = typer.Option(False, "--json")):
    """Show the hosted instance you're connected to (endpoints, mode)."""
    with guard(json):
        client, url = _remote()
        info = client.info()
        emit(json, info, lambda: (console.print(f"[bold]{url}[/bold] — {info.get('agent')} v{info.get('version','?')}"),
                                  console.print(f"  remote MCP: {info.get('remoteMcp')}   multiTenant: {info.get('multiTenant')}")))


@cloud_app.command("send")
def cloud_send(type: str = typer.Option("message.received", "--type"),
               payload: Optional[str] = typer.Option(None, "--payload"),
               payload_file: Optional[Path] = typer.Option(None, "--payload-file"),
               json: bool = typer.Option(False, "--json")):
    """Trigger a run on the hosted agent."""
    with guard(json):
        client, _ = _remote()
        res = client.send_event(type, _parse_payload(payload, payload_file))
        emit(json, res, lambda: console.print(f"[green]✓[/green] run {res.get('runId')} → {res.get('status')}"
                                              + (f" (approval {res.get('pendingApproval')})" if res.get('pendingApproval') else "")))


@cloud_app.command("runs")
def cloud_runs(json: bool = typer.Option(False, "--json")):
    """List runs on the hosted agent."""
    with guard(json):
        client, _ = _remote()
        res = client.list_runs()
        runs = res.get("runs", res) if isinstance(res, dict) else res
        emit(json, {"runs": runs}, lambda: [console.print(f"  {r.get('id')}  {r.get('status')}") for r in (runs or [])]
             or [console.print("  [dim]no runs[/dim]")])


@cloud_app.command("approvals")
def cloud_approvals(json: bool = typer.Option(False, "--json")):
    """List pending approvals on the hosted agent."""
    with guard(json):
        client, _ = _remote()
        res = client.list_approvals("pending")
        apprs = res.get("approvals", []) if isinstance(res, dict) else res
        emit(json, {"approvals": apprs},
             lambda: [console.print(f"  {a.get('id')}  {a.get('title','')}") for a in (apprs or [])]
             or [console.print("  [dim]no pending approvals[/dim]")])


@cloud_app.command("approve")
def cloud_approve(approval_id: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    """Approve a pending approval on the hosted agent (resumes the real run)."""
    with guard(json):
        client, _ = _remote()
        res = client.approve(approval_id)
        emit(json, res, lambda: console.print(f"[green]✓[/green] approved → run {res.get('runStatus', res.get('status'))}"))


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
            "environment": current_environment(),
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
            console.print(f"[bold]{manifest.name}[/bold] v{manifest.version} ({current_environment()})")
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
                            "environment": current_environment(), "runtime": manifest.runtime}]}
        emit(json, data, lambda: console.print(f"{manifest.name}  v{manifest.version}  ({current_environment()})"))


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
def mcp(http: bool = typer.Option(False, "--http", help="Serve remote MCP over HTTP instead of stdio."),
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8765, "--port"),
        json: bool = typer.Option(False, "--json")):
    """Run the Rya MCP server so MCP-native coding agents can drive Rya.

    Default is stdio (a local agent spawns the process). `--http` serves *remote*
    MCP — agents in any editor connect to `http://host:port/mcp` over the network,
    no local install. `rya serve` also mounts this at `/mcp` on the control plane.
    """
    with guard(json):
        try:
            from ..mcp.server import run as run_server, run_http
        except ImportError:
            raise RyaError("E_RUNTIME", "MCP extra not installed.",
                           hint="Install with: pip install 'rya[mcp]'")
        if http:
            err_console.print(f"[green]✓[/green] Rya remote MCP on [bold]http://{host}:{port}/mcp[/bold]")
            run_http(host, port)
        else:
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


@orgs_app.command("create")
def orgs_create(name: str = typer.Argument(..., help="Display name."),
                json: bool = typer.Option(False, "--json")):
    """Create an organization — the billing entity that owns workspaces (D29).

    Creating one does not move any isolation boundary: `workspace_id` stays the
    thing RLS pins to, and no policy anywhere references `org_id`. What an org
    owns is a bill.
    """
    with guard(json):
        org = _tenancy().create_organization(name)
        emit(json, {"org": org},
             lambda: console.print(f"[green]✓[/green] organization "
                                   f"[bold]{org['id']}[/bold] ({name})"))


@orgs_app.command("list")
def orgs_list(json: bool = typer.Option(False, "--json")):
    """List organizations, their workspace counts and their budgets."""
    with guard(json):
        items = _tenancy().list_organizations()

        def render():
            t = Table("id", "name", "workspaces", "budget")
            for o in items:
                budget = o.get("budget") or {}
                t.add_row(o["id"], o["name"], str(o["workspaces"]),
                          ", ".join(f"{k}={v}" for k, v in sorted(budget.items())) or "-")
            console.print(t)
        emit(json, {"orgs": items}, render)


@orgs_app.command("assign")
def orgs_assign(workspace: str = typer.Argument(..., help="Workspace id."),
                org: str = typer.Option(..., "--org", help="Organization id."),
                json: bool = typer.Option(False, "--json")):
    """Move a workspace's BILLING to another org. Its isolation is unaffected."""
    with guard(json):
        rec = _tenancy().assign_workspace_to_org(workspace, org)
        emit(json, rec,
             lambda: console.print(f"[green]✓[/green] {workspace} now bills to {org}"))


@orgs_app.command("budget")
def orgs_budget(org: str = typer.Argument(..., help="Organization id."),
                tokens_per_day: Optional[int] = typer.Option(None, "--tokens-per-day"),
                usd_per_day: Optional[float] = typer.Option(None, "--usd-per-day"),
                usd_per_month: Optional[float] = typer.Option(None, "--usd-per-month"),
                clear: bool = typer.Option(False, "--clear", help="Remove the budget."),
                json: bool = typer.Option(False, "--json")):
    """Set an organization's shared budget.

    Money limits only. An org is the billing boundary, so a concurrency cap there
    would be a scheduling limit at a boundary that does no scheduling — those stay
    per workspace, with `rya quotas set`.

    Writing a budget does not enforce it on its own: `rya orgs reconcile` computes
    the rollup and pushes the verdict to each member workspace, which is what the
    admission path reads. Run it from a cron.
    """
    with guard(json):
        spec = None if clear else {k: v for k, v in (
            ("maxTokensPerDay", tokens_per_day),
            ("maxCostUsdPerDay", usd_per_day),
            ("maxCostUsdPerMonth", usd_per_month)) if v is not None}
        if spec is not None and not spec:
            raise RyaError("E_VALIDATION", "Name at least one limit, or pass --clear.",
                           hint="e.g. `rya orgs budget org_x --usd-per-month 500`.")
        rec = _tenancy().set_org_budget(org, spec)
        emit(json, rec, lambda: console.print(
            f"[green]✓[/green] budget for {org}: "
            + (", ".join(f"{k}={v}" for k, v in sorted((spec or {}).items())) or "cleared")))


@orgs_app.command("show")
def orgs_show(org: str = typer.Argument(..., help="Organization id."),
              json: bool = typer.Option(False, "--json")):
    """One org's budget, members, and current aggregate usage.

    `byWorkspace` is the field to read first when an org has stopped: the total says
    the budget is gone and this says which tenant spent it.

    `freshness` is the field to read when an org has *not* stopped and should have.
    The numbers above it are computed live by this command; the numbers the platform
    *enforces* are the derived verdict a reconciler last wrote, and if nothing is
    running one, those are two different things.
    """
    with guard(json):
        import os as _os

        from .. import orgs as O
        from ..store import open_worker_store

        record = _tenancy().get_organization(org)
        if record is None:
            raise RyaError("E_NOT_FOUND", f"No organization '{org}'.")
        dsn = _os.environ.get("RYA_DATABASE_URL") or _os.environ.get("DATABASE_URL") or ""
        usage = O.org_usage(dsn, org)
        budget = O.coerce_budget(record.get("budget") or {})
        breaches = O.violations_for(budget, usage)
        # Asked per member workspace, because the verdict is per workspace: a
        # reconcile that failed halfway leaves some members current and some stale,
        # and reporting only the first would hide it.
        fresh = {}
        for ws in record.get("workspaces") or []:
            member = open_worker_store(Path.cwd(), ws)
            try:
                fresh[ws] = O.freshness(member)
            finally:
                closer = getattr(member, "close", None)
                if closer is not None:
                    closer()
        out = {"org": record, "budget": budget.describe(), "usage": usage,
               "violations": breaches, "freshness": fresh}

        def render():
            console.print(f"[bold]{record['name']}[/bold] ({org}) — "
                          f"{len(record['workspaces'])} workspace(s)")
            t = Table("workspace", "tokens today", "USD today", "USD month")
            for ws, row in sorted(usage["byWorkspace"].items()):
                t.add_row(ws, str(row["tokensToday"]), f"{row['costUsdToday']:.4f}",
                          f"{row['costUsdMonth']:.4f}")
            t.add_row("[bold]total[/bold]", str(usage["tokensToday"]),
                      f"{usage['costUsdToday']:.4f}", f"{usage['costUsdMonth']:.4f}")
            console.print(t)
            for v in breaches:
                console.print(f"  [red]✗[/red] {v['label']} {v['current']}/{v['max']}")
            if not breaches:
                console.print("  [green]✓[/green] within budget")
            # The gap §9 named: a budget nothing reconciles caps nothing. Said out
            # loud here rather than left for someone to infer from a `computedAt`.
            behind = sorted(w for w, f in fresh.items() if f["stale"])
            if behind and budget.enforced:
                console.print(
                    f"  [yellow]![/yellow] {len(behind)} workspace(s) have a stale or "
                    f"missing org verdict ({', '.join(behind[:4])}"
                    f"{'…' if len(behind) > 4 else ''}) — this budget is not being "
                    "enforced. Run `rya supervisor --all-workspaces`, or "
                    "`rya orgs reconcile` from a cron.")
        emit(json, out, render)


@orgs_app.command("reconcile")
def orgs_reconcile(org: Optional[str] = typer.Option(None, "--org",
                                                     help="One org; default every org."),
                   dry_run: bool = typer.Option(False, "--dry-run",
                                                help="Compute and report; write nothing."),
                   json: bool = typer.Option(False, "--json")):
    """Recompute every org's rollup and push its verdict to member workspaces.

    The enforcement half of an org budget, and it is a separate step on purpose.
    Summing an org's meter needs a connection that spans tenants; putting one on
    every tenant's admission path would hand the hot path a credential that can read
    every other tenant — which is what Phase 4 spent itself removing from a far less
    privileged process. So this computes the aggregate out here and writes only the
    *verdict* into each workspace's own policy row, where an ordinary tenant-scoped
    read finds it.

    Idempotent. Run it from a cron; the staleness between ticks is the same trade
    §11.12 already made for token limits, with a wider bound.
    """
    with guard(json):
        import os as _os

        from .. import orgs as O

        dsn = _os.environ.get("RYA_DATABASE_URL") or _os.environ.get("DATABASE_URL")
        if not dsn:
            raise RyaError("E_VALIDATION", "Org budgets require Postgres.",
                           hint="Organizations are a multi-tenant feature; set "
                                "RYA_DATABASE_URL.")
        _tenancy()  # idempotent setup, so `budget` and the org tables exist
        results = O.reconcile(dsn, org_id=org, dry_run=dry_run)
        over = [r for r in results if r.get("exhausted")]

        def render():
            for r in results:
                if not r.get("ok"):
                    console.print(f"  [red]✗[/red] {r['orgId']}: {r.get('error')}")
                    continue
                mark = "[red]✗[/red]" if r["exhausted"] else "[green]✓[/green]"
                console.print(f"  {mark} {r['name']} ({r['orgId']}) — "
                              f"{len(r['workspaces'])} workspace(s), "
                              f"${r['usage']['costUsdMonth']:.4f} this month"
                              + (" — over budget" if r["exhausted"] else ""))
            console.print(f"[green]✓[/green] reconciled {len(results)} org(s), "
                          f"{len(over)} over budget"
                          + (" (dry run — nothing written)" if dry_run else ""))
        emit(json, {"orgs": results, "exhausted": len(over), "dryRun": dry_run}, render)


@workspaces_app.command("disable")
def workspaces_disable(workspace: str = typer.Argument(..., help="Workspace id."),
                       reason: str = typer.Option("", "--reason",
                                                  help="Recorded on the audit stub."),
                       retention_days: int = typer.Option(
                           None, "--retention-days",
                           help="Days before a purge is allowed. Default 30; 0 for none."),
                       json: bool = typer.Option(False, "--json")):
    """Phase one of D31: stop scheduling, refuse claims, revoke keys. Reversible.

    The step a billing failure or an abuse report should trigger. Nothing is destroyed,
    queued work is refused rather than dropped, and `rya workspaces enable` undoes it.
    """
    with guard(json):
        from .. import purge as P

        _, store = _admin_store(workspace)
        kwargs = {} if retention_days is None else {"retention_days": retention_days}
        # Key revocation needs the tenancy tables, which only exist on Postgres. A
        # single-tenant self-host has no per-workspace API keys to revoke, and refusing
        # to disable it for want of a table it does not need would make the local arm
        # unable to exercise the lifecycle at all.
        try:
            tenancy = _tenancy()
        except RyaError:
            tenancy = None
        out = P.disable(store, reason=reason, actor=_actor(), tenancy=tenancy,
                        workspace=workspace, **kwargs)
        emit(json, {"ok": True, **out},
             lambda: console.print(
                 f"[yellow]●[/yellow] workspace [bold]{workspace}[/bold] disabled"
                 + (f" ({reason})" if reason else "")
                 + f" · {out['keysRevoked']} key(s) revoked · purgeable after "
                 f"{out['purgeableAt'] or 'immediately'}"))


@workspaces_app.command("enable")
def workspaces_enable(workspace: str = typer.Argument(..., help="Workspace id."),
                      json: bool = typer.Option(False, "--json")):
    """Undo a disable. Refuses on a purged workspace, which cannot be undone."""
    with guard(json):
        from .. import purge as P

        _, store = _admin_store(workspace)
        out = P.enable(store, actor=_actor())
        emit(json, {"ok": True, **out},
             lambda: console.print(f"[green]✓[/green] workspace [bold]{workspace}[/bold] "
                                   "re-enabled · queued work resumes"))


@workspaces_app.command("purge")
def workspaces_purge(workspace: str = typer.Argument(..., help="Workspace id."),
                     force: bool = typer.Option(
                         False, "--force",
                         help="Skip the disabled-state and retention-window checks."),
                     dry_run: bool = typer.Option(
                         False, "--dry-run",
                         help="Report what would be destroyed, and destroy nothing."),
                     json: bool = typer.Option(False, "--json")):
    """Phase two of D31: crypto-shred the key, delete objects and rows. IRREVERSIBLE.

    Prints an attestation — one sentence written by the code that did the work,
    distinguishing "unreadable by construction" from "rows deleted". The difference
    matters when answering a deletion request, and it depends on whether this
    deployment uses a per-tenant key provider (`rya keyring show`).
    """
    with guard(json):
        import os

        from .. import purge as P
        from ..bundles import resolve_bundle_store

        root, store = _admin_store(workspace)
        keyring = None
        try:
            from ..keys import resolve_keyring

            keyring = resolve_keyring(root=root)
        except RyaError:
            pass    # reported in the attestation, not fatal
        try:
            bundle_store = resolve_bundle_store(root, workspace=workspace)
        except RyaError:
            bundle_store = None
        report = P.purge(store, workspace=workspace, keyring=keyring,
                         bundle_store=bundle_store,
                         admin_dsn=(os.environ.get("RYA_DATABASE_URL")
                                    or os.environ.get("DATABASE_URL") or ""),
                         force=force, dry_run=dry_run, actor=_actor())
        emit(json, report.describe(),
             lambda: (console.print(("[dim]dry run — nothing destroyed[/dim]"
                                     if dry_run else
                                     "[red]●[/red] purged" if report.ok else
                                     "[red]![/red] purge INCOMPLETE")),
                      console.print(f"  {report.attestation()}"),
                      *[console.print(f"  [red]✗[/red] {e}") for e in report.errors]))


@keyring_app.command("show")
def keyring_show(json: bool = typer.Option(False, "--json")):
    """Which key provider this deployment uses, and what it can therefore promise.

    The field to read is `shreddable`: only a provider that stores a random per-tenant
    key can make a purge cryptographic (D31). The default `deployment` provider cannot,
    and that is not a bug — it is the right default for a single-tenant self-host.
    """
    with guard(json):
        from ..keys import resolve_keyring

        root, _store_ = _admin_store()
        ring = resolve_keyring(root=root)
        info = ring.describe()
        emit(json, info,
             lambda: console.print(
                 f"provider: [bold]{info['provider']}[/bold]  "
                 f"per-tenant: {'yes' if info['perTenant'] else 'no'}  "
                 f"shreddable: {'[green]yes[/green]' if info['shreddable'] else '[yellow]no[/yellow]'}"
                 + (f"  wrapper: {info['wrapper']}" if info.get("wrapper") else "")))


@keyring_app.command("rotate")
def keyring_rotate(workspace: str = typer.Option("", "--workspace",
                                                 help="Workspace to rotate. Empty = this store's."),
                   reseal: bool = typer.Option(False, "--reseal",
                                               help="Re-seal existing secrets under the new key."),
                   json: bool = typer.Option(False, "--json")):
    """Mint the next key generation. Old ciphertext keeps opening until re-sealed.

    Rotation and re-sealing are separate because minting is instant and a re-seal walks
    every sealed row — so an operator can rotate now and re-seal on a schedule. Pass
    `--reseal` to do both.
    """
    with guard(json):
        from ..keys import reseal as reseal_all
        from ..keys import resolve_keyring

        root, store = _admin_store(workspace)
        ring = resolve_keyring(root=root)
        ws = workspace or str(getattr(store, "workspace_id", "") or "")
        key = ring.rotate(ws)
        out = {"ok": True, "key": key.describe()}
        if reseal:
            out["reseal"] = reseal_all(store, keyring=ring, workspace=ws)
        emit(json, out,
             lambda: console.print(
                 f"[green]✓[/green] new key generation [bold]{key.generation}[/bold] "
                 f"for {ws or '(this store)'}"
                 + (f" · re-sealed {out['reseal']['resealed']} secret(s)" if reseal
                    else " · run `rya keyring reseal` to re-protect existing secrets")))


@keyring_app.command("reseal")
def keyring_reseal(workspace: str = typer.Option("", "--workspace"),
                   json: bool = typer.Option(False, "--json")):
    """Re-seal every connection secret under the workspace's current key.

    Wider than `rya connections reseal`, which only converts plaintext: this also moves
    a v1 envelope to v2 and a superseded generation to the current one. Reports per-row
    failures rather than stopping, because one unreadable secret must not prevent the
    other ninety-nine being re-protected.
    """
    with guard(json):
        from ..keys import reseal as reseal_all
        from ..keys import resolve_keyring

        root, store = _admin_store(workspace)
        ring = resolve_keyring(root=root)
        ws = workspace or str(getattr(store, "workspace_id", "") or "")
        res = reseal_all(store, keyring=ring, workspace=ws)
        emit(json, {"ok": not res["failed"], **res},
             lambda: (console.print(
                 f"[green]✓[/green] re-sealed [bold]{res['resealed']}[/bold] · "
                 f"{res['current']} already current, {res['empty']} without a secret "
                 f"({res['scanned']} scanned)"),
                 *[console.print(f"  [red]✗[/red] {e['provider']}: {e['error']}")
                   for e in res["errors"]]))


@app.command()
def posture(verify: bool = typer.Option(
                False, "--verify",
                help="Probe the substrate for real. Costs a container/pod start."),
            json: bool = typer.Option(False, "--json")):
    """The launch gate: does this deployment meet the untrusted-tenant posture?

    Reports all three conditions at once — a sandbox that contains a kernel escape
    (D23), a tenant process holding no credentials (D18), and egress enforced by the
    network (D24) — because any one of them missing makes the other two insufficient.

    `--verify` asks the substrate what kernel it is actually running rather than
    trusting the flag that was passed. That check is the residual MULTITENANT §9 risk 8
    named, and it is off by default only because it costs a container start.
    """
    with guard(json):
        from ..broker.inventory import take_inventory
        from ..execution.drivers import check_untrusted_posture, resolve_driver

        driver = resolve_driver()
        report = check_untrusted_posture(driver, verify=verify)
        inventory = take_inventory()
        payload = {**report.describe(), "driver": driver.describe(),
                   "credentials": inventory.describe()}

        def render():
            # A trusted deployment with none of the three met is CORRECT, and marking
            # it with a red cross would train an operator to ignore the mark on the one
            # deployment where it means something.
            satisfied = report.ok or not report.untrusted
            mark = "[green]✓[/green]" if satisfied else "[red]✗[/red]"
            state = ("untrusted tenancy" if report.untrusted
                     else "trusted tenancy (untrusted not declared)")
            console.print(f"{mark} {state} · driver [bold]{driver.name}[/bold] "
                          f"({driver.isolation})")
            for name, ok, detail in (("isolation (D23)", report.isolation_ok,
                                      report.isolation_detail),
                                     ("mediation (D18)", report.broker_ok,
                                      report.broker_detail),
                                     ("egress (D24)", report.egress_ok,
                                      report.egress_detail)):
                tick = "[green]✓[/green]" if ok else "[yellow]○[/yellow]"
                console.print(f"  {tick} {name}: {detail}")
            console.print(
                f"  {'[green]✓[/green]' if inventory.clean else '[yellow]○[/yellow]'} "
                f"this process holds "
                + ("no platform credentials" if inventory.clean else
                   ", ".join(sorted({f.group or f.name for f in inventory.violations}))))
            if not report.untrusted:
                console.print("[dim]  RYA_UNTRUSTED_TENANTS is not set, so none of the "
                              "above is enforced. The trusted posture is supported — "
                              "just do not advertise hostile-tenant isolation.[/dim]")

        emit(json, payload, render)


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
def worker(once: bool = typer.Option(False, "--once", help="Drain due work once and exit."),
           interval: float = typer.Option(2, "--interval", help="Poll seconds between drains."),
           max_iterations: Optional[int] = typer.Option(None, "--max-iterations", help="Stop after N polls."),
           version: Optional[str] = typer.Option(None, "--version", help="Serve a pinned deployment version."),
           env: Optional[str] = typer.Option(None, "--env", help="Serve whichever version this environment points at."),
           workspace: str = typer.Option("default", "--workspace"),
           agent: Optional[str] = typer.Option(
               None, "--agent",
               help="Which agent to serve. Defaults to the mounted manifest's, which "
                    "is only unambiguous while a deployment serves one (D21)."),
           concurrency: int = typer.Option(1, "--concurrency", help="Parallel job execution."),
           idle_exit: float = typer.Option(0, "--idle-exit",
                                           help="Exit after N idle seconds with an empty queue (scale to zero)."),
           fork: bool = typer.Option(False, "--fork",
                                     help="D27: run each item in a fork of a warm interpreter; "
                                          "this process imports no agent code."),
           run_timeout: float = typer.Option(0, "--run-timeout",
                                             help="With --fork, kill a run that exceeds N seconds "
                                                  "(0 = no limit, the in-process behaviour)."),
           scope: Optional[str] = typer.Option(
               None, "--scope",
               help="Claimer scope (D27/#19-8b): `version` (default) serves one "
                    "(workspace, agent, version); `tenant` serves the whole workspace, "
                    "forking whichever version each item is pinned to. Defaults to "
                    "RYA_CLAIMER_SCOPE."),
           prewarm: Optional[List[str]] = typer.Option(
               None, "--prewarm",
               help="With --scope tenant, warm the current version of this environment "
                    "before claiming. Repeatable."),
           json: bool = typer.Option(False, "--json")):
    """Run an execution-plane worker: claim and execute due turns and jobs.

    This is the `worker` half of the platform (PLATFORM_DESIGN §5.2). Run
    several concurrently for horizontal throughput — claims are atomic on
    Postgres.

    With `--version` or `--env` it serves ONE pinned, content-hashed deployment:
    it loads that bundle, verifies its hash, advertises its handler set, refuses
    to start on a mismatch, and claims only work pinned to it (D3, D12). Without
    either it runs the working tree, which is `rya dev` and single-tenant serve.

    `--fork` selects D27's execution mode: this process claims but never imports
    the bundle, and each item runs in a fork of a warm interpreter keyed by the
    bundle's content hash. Same scope, same preflight, same durability — what
    changes is that the long-lived process holds no tenant code.

    `--scope tenant` is the widening that mode was built for (#19-8b). One claimer
    serves the whole workspace: it peeks at the queue, warms the version the next
    item is pinned to, and forks a child to claim it — so five agents with two live
    versions each occupy one sandbox rather than ten, a promotion costs no extra
    sandbox, and an approval resuming on a retired version is a fork rather than a
    deployment. It implies `--fork`, takes no `--agent` and no `--version`, and
    needs no mounted project.
    """
    with guard(json):
        from ..worker import start_worker

        from ..store import open_worker_store

        from ..execution.scope import SCOPE_TENANT, resolve_scope

        if resolve_scope(scope) == SCOPE_TENANT:
            # Deliberately NOT `_project()`, for the same reason `rya supervisor`
            # is not: a tenant claimer runs where there is no `rya.agent.yaml` at
            # all. It learns its agents from published versions (D21) and
            # materialises each bundle from the object store, so requiring a
            # mounted manifest would make the wide scope only usable in exactly
            # the deployment shape it exists to replace.
            from ..agents import project_root as mounted_project

            found = find_manifest()
            root = mounted_project() or (found.parent if found else Path.cwd())
            manifest = None
        else:
            root, manifest = _project()
        # NOT `_store()`: the execution plane needs a store scoped to
        # `--workspace` and connected as the weaker `rya_worker` role. Using the
        # control plane's builder here is what made `--workspace` decorative —
        # the key said `acme`, the store read everything (#5 / D19).
        store = open_worker_store(root, workspace)
        # `--agent` overrides the mounted manifest, which stopped being a sufficient
        # answer at D21: a deployment serving two agents has one manifest on disk, so
        # `--env prod` resolved the pointer for whichever agent that file happened to
        # name. With `--version` the version record names the agent and it did not
        # matter; with `--env` it decided which agent got served, silently.
        w = start_worker(project_root=root, store=store, workspace=workspace,
                         version_id=version, environment=env,
                         agent_name=agent or (manifest.name if manifest else None),
                         concurrency=concurrency,
                         idle_exit_seconds=idle_exit, poll_seconds=interval,
                         # `or None`, so an absent --fork reaches `start_worker` as
                         # "the scope decides" rather than as an explicit refusal. A
                         # typer bool flag cannot distinguish the two, and `--scope
                         # tenant` needs it to: the help below says it implies --fork.
                         fork=fork or None, run_timeout_seconds=run_timeout,
                         scope=scope, prewarm=tuple(prewarm or ()))
        if once:
            w.preflight()
            w.register()
            try:
                tick = w.drain_once()
            finally:
                w.deregister("once")
            out = {"workerId": w.id, "ran": tick["jobs"], "turns": tick["turns"],
                   "count": tick["count"], **w.key.describe()}
            emit(json, out,
                 lambda: console.print(f"[green]✓[/green] drained {tick['count']} item(s)"))
            return
        if not json:
            if w.key.tenant_scoped:
                warm = ", ".join(f"{p['agent']}@{p['versionId'][:12]}" for p in w.prewarmed)
                console.print(f"[green]✓[/green] claimer {w.id} serving all of "
                              f"[bold]{w.key.workspace}[/bold] — "
                              f"{('warm: ' + warm) if warm else 'nothing pre-warmed'} — "
                              f"polling every {interval}s (Ctrl-C to stop)")
            else:
                pin = w.key.version_id or "working tree"
                console.print(f"[green]✓[/green] worker {w.id} serving [bold]{w.key.agent}[/bold] "
                              f"({pin}) — polling every {interval}s (Ctrl-C to stop)")
        result = w.run(max_iterations=max_iterations,
                       on_tick=None if json else lambda t: (
                           console.print(f"  ran {t['count']} item(s)") if t["count"] else None))
        if json:
            emit(json, result, lambda: None)


@app.command("template-host")
def template_host(
    socket: Optional[str] = typer.Option(None, "--socket",
                                         help="Where to listen. Defaults to RYA_TEMPLATE_HOST."),
    max_entries: Optional[int] = typer.Option(None, "--max-entries",
                                              help="Warm interpreters to hold. Defaults to the "
                                                   "tenant-scope pool size."),
    run_timeout: float = typer.Option(0, "--run-timeout",
                                      help="Kill a forked run that exceeds N seconds "
                                           "(0 = no limit)."),
    status: bool = typer.Option(False, "--status",
                                help="Ask a running host what it is holding, and exit."),
    json: bool = typer.Option(False, "--json"),
):
    """Serve warm interpreters over a socket — the sandbox half of the D32 pair.

    This process holds **no credentials** and claims nothing. It imports tenant
    bundles on request and forks one child per dispatch, which is exactly what the
    claimer's own warm pool did before — the difference is that it can now be in a
    different container, so the claimer (which holds the database credential, the seal
    key and the pooled provider key) no longer has to be the tenant process's parent.

    Run it as the sandbox container's entrypoint, beside a `rya worker --fork` that
    has RYA_TEMPLATE_HOST pointing at the same socket on a shared volume. Both
    containers must be the same build (D5). The `docker` and `kubernetes` drivers
    render that pair for you; this command is for running one by hand, and for
    `--status` against one that is already up.
    """
    with guard(json):
        from ..execution.host import (HOST_SOCKET_ENV, HostedTemplateProbe,
                                      TemplateHost, host_socket, host_token)
        from ..execution.pool import default_pool_size
        from ..execution.scope import SCOPE_TENANT

        path = (socket or host_socket()).strip()
        if not path:
            raise RyaError(
                "E_VALIDATION",
                "A template host needs a socket path and none was given.",
                hint=f"Pass --socket, or set {HOST_SOCKET_ENV} to the shared path the "
                     "claimer beside it will connect to. In Kubernetes that is a file "
                     "on the `broker` emptyDir; with docker it is the bind mount both "
                     "halves of the pair are given.")
        if status:
            emit(json, HostedTemplateProbe(path, host_token()).status(),
                 lambda: console.print(f"[green]✓[/green] template host on {path}"))
            return
        host = TemplateHost(
            socket_path=Path(path), token=host_token(),
            max_entries=max_entries or default_pool_size(SCOPE_TENANT),
            run_timeout_seconds=run_timeout)
        if not json:
            console.print(f"[green]✓[/green] template host on [bold]{path}[/bold] — "
                          f"holding up to {host.pool.max_entries} warm interpreter(s), "
                          "no credentials (Ctrl-C to stop)")
        host.serve_forever()


@app.command()
def supervisor(
    once: bool = typer.Option(False, "--once", help="One observe/plan/apply tick, then exit."),
    plan_only: bool = typer.Option(False, "--plan", help="Decide and print; launch nothing."),
    interval: float = typer.Option(5, "--interval", help="Seconds between ticks."),
    max_ticks: Optional[int] = typer.Option(None, "--max-ticks", help="Stop after N ticks."),
    workspace: str = typer.Option("default", "--workspace"),
    env: Optional[str] = typer.Option(None, "--env", help="The environment its workers serve."),
    all_workspaces: bool = typer.Option(False, "--all-workspaces",
                                        help="Fan out over every workspace (needs the admin DSN)."),
    prewarm: Optional[str] = typer.Option(None, "--prewarm",
                                          help="Comma-separated environments to keep one warm worker for."),
    max_replicas: int = typer.Option(4, "--max-replicas", help="Cap on workers per key."),
    backlog: int = typer.Option(5, "--backlog-per-worker", help="Queued items one worker is assumed to absorb."),
    idle_exit: float = typer.Option(60, "--idle-exit", help="Idle seconds before a launched worker exits."),
    scope: Optional[str] = typer.Option(
        None, "--scope",
        help="Claimer scope to schedule (D27/#19-8b). Must match what the claimers "
             "actually run: `version` plans one worker per (agent, version), `tenant` "
             "plans one per workspace. Defaults to RYA_CLAIMER_SCOPE."),
    lease: bool = typer.Option(
        True, "--lease/--no-lease",
        help="Hold a per-workspace lease before applying a plan, so a second "
             "supervisor stands by instead of doubling the fleet."),
    json: bool = typer.Option(False, "--json"),
):
    """Start, scale and reap workers on demand (D25) — the scheduling half of §6.

    Nothing else in the platform starts a worker, which is why scale-to-zero was
    one-way: a key could idle out and then stay unserved. This watches claimable
    queue depth and the worker registry, and launches through the configured
    execution driver (`RYA_EXECUTION_DRIVER`, default `local`).

    `--plan` is the honest way to see what it would do. The decision is a pure
    function of (registry, depth, quota), so printing it is not a dry-run
    approximation of the real thing — it *is* the real decision, without the
    effects.
    """
    with guard(json):
        from ..agents import project_root as mounted_project
        from ..config import current_environment
        from ..execution.drivers import require_untrusted_posture, resolve_driver
        from ..execution.supervisor import Supervisor, SupervisorPolicy, supervise_workspaces
        from ..store import open_worker_store

        # Deliberately NOT `_project()`. A supervisor must run where there is no
        # `rya.agent.yaml` at all — that is the D21 deployment, which learns its
        # agents from published versions. `RYA_PROJECT` names a mounted tree when
        # one exists, and only unpinned (working-tree) keys need it.
        manifest_path = find_manifest()
        root = mounted_project() or (manifest_path.parent if manifest_path else Path.cwd())
        policy = SupervisorPolicy(
            backlog_per_worker=backlog, max_replicas_per_key=max_replicas,
            idle_exit_seconds=idle_exit,
            prewarm_environments=tuple(e.strip() for e in (prewarm or "").split(",") if e.strip()),
            require_lease=lease,
        )
        # The launch gate, all three conditions, and with `verify=True` — this is the
        # one place worth paying a container start for. `start_worker` runs the same
        # check on every worker but only against the *declaration*, because probing
        # per scale-up would cost a sandbox start per replica. So the split is: the
        # supervisor proves the substrate once at boot, and every worker re-checks the
        # cheap half so a hand-started one cannot slip past.
        driver = require_untrusted_posture(resolve_driver(), verify=True)

        if all_workspaces:
            import os as _os

            dsn = _os.environ.get("RYA_DATABASE_URL") or _os.environ.get("DATABASE_URL") or ""
            if not dsn:
                raise RyaError(
                    "E_VALIDATION", "--all-workspaces needs a Postgres DSN.",
                    hint="Set RYA_DATABASE_URL. Workspaces are a Postgres feature; "
                         "the file store has exactly one.")
            out = supervise_workspaces(admin_dsn=dsn, driver=driver, project_root=root,
                                       environment=env or current_environment(),
                                       policy=policy, scope=scope)
            emit(json, {"workspaces": out},
                 lambda: console.print(f"[green]✓[/green] ticked {len(out)} workspace(s)"))
            return

        # Defaulted, not left None: an unpinned item is scheduled onto whatever
        # this environment promoted, and Phase 2 showed what an api and its workers
        # disagreeing about the environment costs — every turn sits unclaimed.
        environment = env or current_environment()
        store = open_worker_store(root, workspace)
        sup = Supervisor(store, driver, workspace=workspace, environment=environment,
                         project_root=root, policy=policy, scope=scope)
        if plan_only:
            actions = [a.describe() for a in sup.plan()]
            emit(json, {"driver": driver.describe(), "actions": actions},
                 lambda: console.print(f"[green]✓[/green] {len(actions)} action(s) planned"
                                       + ("" if actions else " — the fleet matches the work")))
            return
        if once:
            emit(json, sup.tick(),
                 lambda: console.print("[green]✓[/green] one tick applied"))
            return
        if not json:
            console.print(f"[green]✓[/green] supervisor up on the [bold]{driver.name}[/bold] "
                          f"driver (isolation: {driver.isolation}, scope: {sup.scope}) "
                          f"— every {interval}s")
        result = sup.run(max_ticks=max_ticks, tick_seconds=interval,
                         on_tick=None if json else lambda t: (
                             console.print(f"  {len(t['actions'])} action(s), "
                                           f"{t['alive']} alive") if t["actions"] else None))
        emit(json, result, lambda: None)


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
        info["remoteMcp"] = f"http://{host}:{port}/mcp"
        if json:
            typer.echo(jsonlib.dumps(info))
        else:
            console.print(f"[green]✓[/green] serving control plane on http://{host}:{port}")
            console.print(f"  console: [bold]http://{host}:{port}/[/bold]")
            console.print(f"  remote MCP: [bold]http://{host}:{port}/mcp[/bold] (connect any editor's agent)")
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
