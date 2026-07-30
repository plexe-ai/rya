# `rya.cli` - the `rya` command-line interface

Typer-based CLI. Every command takes `--json` (machine-readable) and errors carry
a stable `E_*` code + semantic exit code, so a coding agent can branch on outcomes.

## Files

- `main.py` - the `rya` app and all commands: `create`/`init` (scaffold),
  `dev`, `events send`, `runs`, `approvals`, `jobs`, `worker`, `serve`, `mcp`,
  `status`, `agents`, `tools`, `models`, `channels`, `secrets`, `schedules`,
  `queue`, `workspaces`, `keys`, `connections`, `skills`, `cloud`, `token`,
  `deploy`.
- `scaffold.py` - project templates. `write_project(target, name, template=)`:
  - `minimal` (default): real seams only - no mocked domain data.
  - `demo`: the full showcase (mock CRM domain, approval gate, cron) - what the
    test-suite exercises. Add a new template by extending `scaffold_files`.
- `deploy_templates.py` - `rya deploy` artifact generation (docker-compose etc.).
- `client.py` - the **client** subset of the CLI: the `rya` console script as the
  thin SDK ships it (D16 / §14). `create`, `init`, `check`, `bundle`,
  `login`/`logout`/`whoami`, `skills`. Its import closure is SDK-only, which is
  why it exists separately: `main.py` imports `..runtime`, `..store`,
  `..sdk.context`, `..config` and `..models.registry` at module scope.

## Notes

- A command that needs the runtime, the store or a provider belongs in `main.py`
  only. Adding one to `client.py` - or a module-scope platform import to
  `scaffold.py`/`deploy_templates.py` - fails `tests/test_sdk_surface.py`.
- `__init__.py` resolves `app` lazily (PEP 562) so `from rya.cli import scaffold`
  does not drag the operator CLI in. Do not restore the eager re-export.

- `create --template demo` is the only path to the mocked domain; keep the
  default mock-free.
- The CLI calls the engine from sync code (no event loop) - see
  `runtime._run_coro`.
