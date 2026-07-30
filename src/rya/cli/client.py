"""The client CLI — the `rya` console script as the thin SDK ships it (D16, §14).

§14 splits `cli/` in two: an **operator subset** that goes out in `rya-server`
(`cli/main.py`: serve, worker, runs, approvals, tenancy, secrets, provision — all
of which need the runtime, the store and the providers) and a **client subset**
that goes out in `rya`. This module is that client subset, and it exists as a
separate entry point for one reason: `cli/main.py` imports `..runtime`,
`..store`, `..sdk.context`, `..config` and `..models.registry` at module scope,
so it cannot be the entry point of a wheel that ships none of them.

D16's requirement is that "`uvx rya create` survives verbatim", so the console
script keeps the name `rya` in both distributions; only the module behind it
differs (`rya.cli.client:app` in the SDK, `rya.cli.main:app` in `rya-server`,
which is a strict superset). Every command here is one an agent *author* runs
from their own repo, and the module's import closure is SDK-only — enforced by
`tests/test_sdk_surface.py`, not by convention.
"""

from __future__ import annotations

import json as jsonlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .. import __version__
from ..errors import RyaError
from ..manifest import find_manifest, load_manifest
from ..manifest.loader import MANIFEST_NAME
from . import scaffold

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Rya — the agent SDK. Author, validate and package an agent.\n\n"
         "This is the client CLI. Operating a deployment (serve, worker, runs, "
         "approvals, workspaces, secrets) is the platform's job and lives in the "
         "`rya-server` distribution — see docs/PACKAGING.md.",
)
console = Console()
err_console = Console(stderr=True)

skills_app = typer.Typer(no_args_is_help=True, help="Install the Rya coding-agent skills.")
app.add_typer(skills_app, name="skills")


# --------------------------------------------------------------------------
# Output + error helpers (the operator CLI's `emit`/`guard`, cli/main.py:97-124;
# duplicated rather than imported because importing main.py is exactly what this
# module exists to avoid).
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


def _project():
    path = find_manifest()
    if path is None:
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            f"No {MANIFEST_NAME} found here or in any parent directory.",
            hint="Run `rya create <name>` or cd into a Rya project.",
        )
    return path.parent, load_manifest(path)


def _load_agent(manifest, project_root: Path):
    """Import the entrypoint and return the `Agent` it defined.

    The platform's twin is `runtime/engine.py:load_agent`, which additionally
    mutates `sys.path` for the worker process and is bound to the engine's
    imports. The mechanism is the same and deliberately so: `define_agent()`
    appends to `sdk.agent._DEFINED_AGENTS` (`sdk/agent.py:121-124`) and the
    loader takes the last one, so a client-side `rya check` reports exactly the
    handler set the platform will find.
    """
    import importlib.util
    import sys
    import uuid

    from ..sdk.agent import _DEFINED_AGENTS

    entry = (project_root / manifest.entrypoint).resolve()
    if not entry.is_file():
        raise RyaError(
            "E_ENTRYPOINT_NOT_FOUND",
            f"Entrypoint '{manifest.entrypoint}' not found at {entry}.",
            hint="Fix `entrypoint:` in rya.agent.yaml or create the file.",
        )
    for p in (str(project_root), str(entry.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    _DEFINED_AGENTS.clear()
    mod_name = f"rya_user_agent_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, entry)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RyaError("E_ENTRYPOINT_NOT_FOUND", f"Could not load entrypoint {entry}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RyaError(
            "E_RUNTIME",
            f"Failed to import entrypoint {manifest.entrypoint}: {exc}",
            hint="Fix the import/syntax error in the agent module.",
        )
    if not _DEFINED_AGENTS:
        raise RyaError(
            "E_NO_AGENT",
            f"{manifest.entrypoint} defines no agent.",
            hint="Add `agent = define_agent()` at module scope.",
        )
    return _DEFINED_AGENTS[-1]


# --------------------------------------------------------------------------
# Authoring
# --------------------------------------------------------------------------
def _version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(False, "--version", "-V", callback=_version_callback,
                                       is_eager=True, help="Show the Rya SDK version and exit.")):
    """Rya — the agent SDK."""


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
                    "next": [f"cd {name}", "rya check"]},
             lambda: (console.print(f"[green]✓[/green] Created project [bold]{name}[/bold] at {target} ({template} template)"),
                      console.print("  next: [bold]cd " + name + " && rya check[/bold]")))


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
def check(json: bool = typer.Option(False, "--json")):
    """Validate the manifest and the handler set, then exit (starts nothing).

    §10: "`rya dev --check` preserves today's instant manifest validation, which
    CI and tight edit loops depend on." That check is client-side by nature — it
    reads the working tree — so it is the one gate the SDK keeps. The *production
    readiness* gate (`readiness.py`) is not here: §9 makes it a server-side
    admission check, and it needs a store and a provider route to run at all.
    """
    with guard(json):
        root, manifest = _project()
        agent = _load_agent(manifest, root)
        info = {
            "agent": manifest.name,
            "version": manifest.version,
            "runtime": manifest.runtime,
            "entrypoint": manifest.entrypoint,
            "eventHandler": agent.event_handler() is not None,
            "jobHandlers": sorted(agent._job_handlers),
            "cronHandlers": sorted(agent._cron_handlers),
            "toolHandlers": sorted(agent._tool_handlers),
            "tools": [t.id for t in manifest.tools],
            "models": [m.id for m in manifest.models],
            "triggers": [t.id for t in manifest.triggers],
            "ready": agent.event_handler() is not None,
        }

        def render():
            console.print(f"[green]✓[/green] [bold]{manifest.name}[/bold] v{manifest.version} ({manifest.runtime})")
            console.print(f"  entrypoint: {manifest.entrypoint}")
            console.print(f"  event handler: {'yes' if info['eventHandler'] else '[red]MISSING[/red]'}")
            console.print(f"  jobs: {', '.join(info['jobHandlers']) or '—'}")
            console.print(f"  tools: {', '.join(info['tools']) or '—'}")
        emit(json, info, render)


@app.command()
def bundle(
    out: Optional[Path] = typer.Option(None, "--out", help="Write the packed .tar.gz here."),
    json: bool = typer.Option(False, "--json"),
):
    """Content-hash the project — the deployment artifact `rya deploy` uploads (D12).

    The hash is computed by the same code the platform verifies with
    (`bundles.py`), which is why that module ships in both distributions: a
    client-computed digest that the server could not reproduce would make
    "immutable, content-hashed, pinned per run" unverifiable.
    """
    with guard(json):
        from .. import bundles

        root, _ = _project()
        b = bundles.build_bundle(root)
        payload = b.to_dict()
        if out is not None:
            payload["archive"] = str(bundles.pack(b, Path(out)))
        emit(json, payload,
             lambda: (console.print(f"[green]✓[/green] bundle [bold]{b.hash[:12]}[/bold] "
                                    f"({b.fileCount} files, {b.sizeBytes} bytes, sdk {b.sdkVersion})"),
                      console.print(f"  archive: {payload['archive']}" if out is not None else
                                    "  pass --out to pack the archive")))


# --------------------------------------------------------------------------
# Pointing at a deployment
# --------------------------------------------------------------------------
@app.command()
def login(url: Optional[str] = typer.Argument(None, help="Hosted Rya URL, e.g. https://rya.yourco.com."),
          key: Optional[str] = typer.Option(None, "--key", help="Workspace API key (rya_sk_…)."),
          json: bool = typer.Option(False, "--json")):
    """Point the CLI + your coding agent at a deployment."""
    with guard(json):
        from ..cloud import RemoteClient, mcp_config_snippet, save_cloud_config

        if not url:
            emit(json, {"mode": "local", "authenticated": True,
                        "message": "No URL given — nothing to authenticate against."},
                 lambda: console.print("[green]✓[/green] No hosted URL given; nothing to authenticate."))
            return
        info = RemoteClient(url, key).info()  # verifies reachability + auth
        save_cloud_config(url, key)
        snippet = mcp_config_snippet(url)
        out = {"ok": True, "mode": "cloud", "cloudUrl": url.rstrip("/"),
               "agent": info.get("agent"), "remoteMcp": info.get("remoteMcp"), "mcpConfig": snippet}

        def render():
            console.print(f"[green]✓[/green] Connected to [bold]{url.rstrip('/')}[/bold] "
                          f"(agent: {info.get('agent')}, v{info.get('version', '?')})")
            console.print("  add this to your agent's [bold].mcp.json[/bold]:")
            console.print(jsonlib.dumps(snippet, indent=2))
        emit(json, out, render)


@app.command()
def logout(json: bool = typer.Option(False, "--json")):
    """Forget the stored deployment connection."""
    with guard(json):
        from ..cloud import clear_cloud_config

        cleared = clear_cloud_config()
        emit(json, {"ok": True, "cleared": cleared, "mode": "local"},
             lambda: console.print("[green]✓[/green] " + ("Signed out." if cleared
                                   else "No hosted connection was set.")))


@app.command()
def whoami(json: bool = typer.Option(False, "--json")):
    """Show which deployment the CLI is pointed at."""
    with guard(json):
        from ..cloud import load_cloud_config

        cfg = load_cloud_config()
        if cfg:
            emit(json, {"mode": "cloud", "cloudUrl": cfg["cloudUrl"], "hasKey": bool(cfg.get("apiKey"))},
                 lambda: console.print(f"[bold]cloud[/bold] → {cfg['cloudUrl']} "
                                       f"({'key set' if cfg.get('apiKey') else 'no key'})"))
        else:
            emit(json, {"mode": "local"},
                 lambda: console.print("[bold]no deployment configured[/bold] (`rya login <url> --key …`)"))


# --------------------------------------------------------------------------
# Coding-agent skills
# --------------------------------------------------------------------------
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


if __name__ == "__main__":  # pragma: no cover
    app()
