# Rya DevEx for Coding Agents

Rya treats the coding agent as a first-class developer. The CLI is the primary
interface; the dashboard is optional. A coding agent should be able to do
everything without clicking.

## Agent-friendly CLI contract

- **`--json` on every command** — stdout is a single JSON object. The first key
  is always `ok` (`true`/`false`).
- **`--non-interactive`** — supported on mutating commands; never prompts.
- **Stable error codes** — every failure returns `{"ok": false, "error":
  {"code": "E_*", "message", "hint", "exit_code"}}`.
- **Semantic exit codes** — branch on these without parsing prose:

  | code | exit | meaning |
  |------|------|---------|
  | `E_MANIFEST_*`, `E_ENTRYPOINT_NOT_FOUND`, `E_AGENT_NOT_DEFINED` | 3 | manifest/entrypoint problem |
  | `E_*_NOT_FOUND` | 4 | missing run/approval/tool/model/job |
  | `E_TOOL_PERMISSION_DENIED` | 5 | permission denied |
  | `E_APPROVAL_NOT_PENDING`, `E_RUN_NOT_PAUSED` | 6 | invalid state transition |
  | `E_VALIDATION` | 7 | bad input |
  | `E_RUNTIME` | 1 | generic runtime error |

- **`hint`** always suggests the next action.

## Inspect → configure → deploy → verify

```bash
rya status --json                       # 1. inspect current state
rya dev --json                          # 2. validate manifest + agent code
rya events send --type ... --json       # 3. trigger a test event
rya runs trace <run_id> --json          # 4. inspect the trace
rya approvals approve <id> --json        # 5. resolve a gate
rya runs list --json                    # 6. verify success
```

A coding agent never has to guess whether a run worked: the run status and
trace are explicit and queryable across separate invocations.

## Machine-readable project state

```bash
rya agents inspect --json     # full manifest
rya tools list --json         # tool registry + permissions
rya models list --json        # model registry
rya runs list --json          # recent runs
rya approvals list --json     # approval state
rya jobs list --json          # job queue
rya schedules list --json     # cron schedules
```

## Idempotency & determinism

Mocked tools/models/LLM are deterministic, so runs are reproducible in tests and
CI. Manifest-mutating commands (`tools register`, `models register`,
`channels connect`, `schedules create`) reject duplicates with `E_VALIDATION`.
