# `rya.cli` - the `rya` command-line interface

Typer-based CLI. Every command takes `--json` (machine-readable) and errors carry
a stable `E_*` code + semantic exit code, so a coding agent can branch on outcomes.

## Files

- `main.py` - the `rya` app and all commands: `create`/`init` (scaffold),
  `dev`, `events send`, `runs`, `approvals`, `jobs`, `worker`, `serve`, `mcp`,
  `status`, `agents`, `tools`, `models`, `channels`, `secrets`, `schedules`,
  `queue`, `workspaces`, `keys`, `connections`, `skills`, `cloud`, `token`,
  `deploy`. It also re-registers `publish` from `client.py` — see the note below.
- `scaffold.py` - project templates. `write_project(target, name, template=)`:
  - `minimal` (default): real seams only - no mocked domain data.
  - `demo`: the full showcase (mock CRM domain, approval gate, cron) - what the
    test-suite exercises. Add a new template by extending `scaffold_files`.
- `deploy_templates.py` - `rya deploy` artifact generation (docker-compose etc.).
- `client.py` - the **client** subset of the CLI: the `rya` console script as the
  thin SDK ships it (D16 / §14). `create`, `init`, `check`, `bundle`, `publish`,
  `login`/`logout`/`whoami`, `skills`. Its import closure is SDK-only, which is
  why it exists separately: `main.py` imports `..runtime`, `..store`,
  `..sdk.context`, `..config` and `..models.registry` at module scope.
  - `_check_project()` is the body of `check`, shared with `publish` so the two
    cannot drift. It deliberately does NOT verify that every declared tool has an
    implementation: a tool may be served by a platform built-in from
    `tools/registry.py`, which this distribution does not ship, so the SDK cannot
    tell a built-in from a hole. The worker's preflight owns that check.
  - `publish` uploads the bundle over HTTP (`POST /agents/{id}/versions`) and is
    the client-side twin of `main.py`'s `deploy --env`. It cannot attest readiness:
    `readiness.py` is platform-only, and the control plane does not import bundles
    either, so nothing on this path can produce that evidence.

## Notes

- A command that needs the runtime, the store or a provider belongs in `main.py`
  only. Adding one to `client.py` - or a module-scope platform import to
  `scaffold.py`/`deploy_templates.py` - fails `tests/test_sdk_surface.py`.
- A command that a CLIENT repo needs belongs in `client.py`, and `main.py` must
  re-register it (`app.command(name=…)(fn)`) rather than reimplement it. Both
  distributions own the `rya` console script, so a client-only command disappears
  the moment someone editable-installs the platform - which is exactly what a
  developer dev-linking their agent repo against a local checkout does. `publish`
  is the worked example. main.py -> client.py is platform -> SDK, the direction
  the boundary test allows.
- `__init__.py` resolves `app` lazily (PEP 562) so `from rya.cli import scaffold`
  does not drag the operator CLI in. Do not restore the eager re-export.

- `create --template demo` is the only path to the mocked domain; keep the
  default mock-free.
- The CLI calls the engine from sync code (no event loop) - see
  `runtime._run_coro`.
