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

## Notes

- `create --template demo` is the only path to the mocked domain; keep the
  default mock-free.
- The CLI calls the engine from sync code (no event loop) - see
  `runtime._run_coro`.
