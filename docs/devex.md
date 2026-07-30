# Rya DevEx for Coding Agents

Rya treats the coding agent as a first-class developer. The CLI is the primary
interface; the dashboard is optional. A coding agent should be able to do
everything without clicking.

## Two CLIs, one console script

There are two `rya` CLIs, and both own the `rya` console script:

- **`src/rya/cli/client.py`** — the thin SDK CLI that ships in the `rya` wheel:
  `create`, `init`, `check`, `bundle`, `publish`, `login`/`logout`/`whoami`,
  `skills install`.
- **`src/rya/cli/main.py`** — the operator CLI, shipped only in `rya-server`
  (exposed as both `rya` and `rya-server`). Everything else in this doc —
  `status`, `dev`, `deploy`, `events`, `runs`, `versions`, `envs`, `gate`,
  `quotas`, `keys`, `workspaces` — lives **only** here.

`publish` and `check` are defined in the client CLI and re-registered in the
operator one, so they do not vanish when an editable install of the platform
repoints the console script. [PACKAGING.md](PACKAGING.md) draws the full line;
assume a command below is operator-only unless it is in the client list above.

## Agent-friendly CLI contract

- **`--json` on every command** — stdout is a single JSON object. The first key
  is always `ok` (`true`/`false`).
- **`--non-interactive`** — accepted on `rya deploy`, `rya provision`,
  `rya eval`, `rya events send`, `rya approvals approve`, `rya approvals reject`
  and `rya publish`. It is a *parity* flag: none of those prompt with or without
  it, and every other command is unconditionally non-interactive. The one real
  prompt in the tree is `rya deploy destroy`'s "delete ALL their data?"
  confirmation, and that one is skipped with `--yes`, not `--non-interactive`.
- **Stable error codes** — every failure returns `{"ok": false, "error":
  {"code": "E_*", "message", "hint", "exit_code"}}`.
- **Semantic exit codes** — branch on these without parsing prose.
  `_CODE_EXIT` in [src/rya/errors.py](../src/rya/errors.py) is the whole registry:

  | code | exit | meaning |
  |------|------|---------|
  | `E_MANIFEST_*`, `E_ENTRYPOINT_NOT_FOUND`, `E_AGENT_NOT_DEFINED` | 3 | manifest/entrypoint problem |
  | any other `E_*_NOT_FOUND` | 4 | missing run/approval/tool/model/model route/job/session/handler/version/environment/bundle |
  | `E_TOOL_PERMISSION_DENIED`, `E_UNAUTHORIZED`, `E_EGRESS_BLOCKED` | 5 | permission denied |
  | `E_APPROVAL_NOT_PENDING`, `E_RUN_NOT_PAUSED`, `E_QUEUE_CONFLICT` | 6 | invalid state transition |
  | `E_BUNDLE_MISMATCH`, `E_VERSION_RETIRED`, `E_VERSION_IN_USE`, `E_JOURNAL_DRIFT`, `E_QUOTA_EXCEEDED` | 6 | the artifact, version or workspace is not in a state that allows this |
  | `E_VALIDATION` | 7 | bad input |
  | `E_PROMOTION_BLOCKED`, `E_NOT_PRODUCTION_READY`, `E_HANDLER_SET_INCOMPLETE`, `E_NO_EVENT_HANDLER` | 7 | refused to ship: a gate or a completeness check said no |
  | `E_RUNTIME`, `E_TIMEOUT`, `E_TOOL_UPSTREAM`, `E_BUNDLE_STORE` | 1 | generic runtime error |

  The two `_NOT_FOUND` rows are ordered on purpose: `E_MANIFEST_NOT_FOUND` and
  `E_ENTRYPOINT_NOT_FOUND` are exit **3**, and the wildcard covers what is left.
  Matching `E_*_NOT_FOUND` first would send both to 4.

  **Any code not in the table exits 1** (`EXIT_GENERIC`), and several real ones
  are not — `E_REMOTE`, the client's own transport failure, among them. Read
  exit 1 as "unclassified", not as "the runtime crashed", and branch on `code`.

- **`hint`** always suggests the next action.

### The contract survives the network

`rya publish` reaches a control plane over HTTP, and
`RemoteClient._http_error` ([src/rya/cloud.py](../src/rya/cloud.py)) re-raises
the **server's** `E_*` code instead of collapsing every HTTP failure into
`E_REMOTE`. So the exit code still says which thing broke: a content-hash
mismatch is `E_BUNDLE_MISMATCH` (409 → exit 6), an unreachable bundle bucket is
`E_BUNDLE_STORE` (503 → exit 1), a refused promotion gate is
`E_PROMOTION_BLOCKED` (422 → exit 7). Three different fixes, three distinguishable
codes, across the wire.

One case still degrades: a failure generated *before* the api sees the request has
no envelope to re-raise. The common one is a reverse proxy's HTML 413 — nginx
defaults `client_max_body_size` to 1 MB, well under Rya's 20 MB
`RYA_MAX_BUNDLE_BYTES` — which carries no code and falls back to `E_REMOTE`,
exit 1. Rya's own 413 does carry one.

## Inspect → configure → deploy → verify

```bash
rya status --json                       # 1. inspect current state
rya dev --json                          # 2. validate manifest + agent code
rya events send --type ... --json       # 3. trigger a test event
rya runs trace <run_id> --json          # 4. inspect the trace
rya approvals approve <id> --json       # 5. resolve a gate
rya runs list --json                    # 6. verify the run succeeded
rya publish --env prod --json           # 7. ship it as an immutable version
rya envs show prod --json               # 8. verify the pointer actually flipped
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
rya versions list --json      # immutable, content-hashed versions
rya envs show <env> --json    # an environment's current version + history
rya gate show --json          # what each environment requires before accepting one
```

The last three are the deployment surface, and they are what an agent inspects
*after* `rya publish`: which version was recorded, whether the pointer actually
flipped, and — when it did not — what the gate wanted.

## Idempotency & determinism

Mocked tools/models/LLM are deterministic, so runs are reproducible in tests and
CI. Manifest-mutating commands (`tools register`, `models register`,
`channels connect`, `schedules create`) reject duplicates with `E_VALIDATION`.

## Linting & formatting

[Ruff](https://docs.astral.sh/ruff/) is both the linter and the formatter. It
ships in the `dev` extra (`pip install -e '.[dev]'`) and is configured entirely
in `pyproject.toml` — no separate config file.

```bash
ruff check .                  # lint
ruff check --fix .            # apply the safe autofixes
ruff format .                 # format
ruff format --check .         # verify formatting without writing
ruff check path/to/file.py    # scope either command to what you touched
```

The existing tree is **not** clean against this config yet, and neither command
runs in CI. The config is the target we converge on incrementally: when you
touch a file, lint and format that file. Don't reformat the repo in a single
pass — it would bury real changes under thousands of lines of churn.

Rules are selected in `[tool.ruff.lint]`; each `ignore` entry carries a comment
explaining why. If a rule fights a deliberate pattern in the codebase, add it
there with a reason rather than sprinkling `# noqa`.
