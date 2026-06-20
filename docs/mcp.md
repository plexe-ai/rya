# Rya MCP + Skill

Two surfaces let coding agents drive Rya without a human: an **MCP server**
(MCP-native agents call Rya as tools) and a **Skill** (teaches the workflow so
the agent doesn't rediscover it each session). Both are built and wrap the same
operations as the CLI.

## MCP server

```bash
pip install 'rya[mcp]'
rya mcp                      # serves over stdio
```

Register it with a coding agent (Claude Code example — `.mcp.json` or
`claude mcp add`):

```json
{
  "mcpServers": {
    "rya": {
      "command": "rya",
      "args": ["mcp"]
    }
  }
}
```

Every tool returns structured JSON with a stable `ok` flag; failures carry
`{code, message, hint, exit_code}`. Tools accept an optional `project_dir` (the
folder containing `rya.agent.yaml`); it defaults to the server's working
directory.

### Tools (19)

| MCP tool | What it does | CLI / API equivalent |
|----------|--------------|----------------------|
| `rya_context` | **One-shot snapshot of the whole backend** (call first) | `rya context` |
| `rya_create_agent` | Scaffold a project | `rya create` |
| `rya_get_agent` | Full manifest | `rya agents inspect` / `GET /agents/:id` |
| `rya_validate_manifest` | Validate manifest + agent code | `rya dev` |
| `rya_deploy_agent` | Validate + deploy plan | `rya deploy` |
| `rya_trigger_event` | Trigger a run | `rya events send` / `POST /agents/:id/events` |
| `rya_list_runs` | Recent runs | `rya runs list` / `GET /agents/:id/runs` |
| `rya_get_run_trace` | Full durable trace | `rya runs trace` / `GET /runs/:id/trace` |
| `rya_list_tools` | Tools + permissions | `rya tools list` / `GET /tools` |
| `rya_register_tool` | Declare a tool | `rya tools register` |
| `rya_list_approvals` | Approval state | `rya approvals list` / `GET /approvals` |
| `rya_approve_action` | Approve → resume run | `rya approvals approve` / `POST /approvals/:id/approve` |
| `rya_reject_action` | Reject → terminate run | `rya approvals reject` / `POST /approvals/:id/reject` |
| `rya_list_models` | Model registry | `rya models list` / `GET /models` |
| `rya_register_model` | Declare a model | `rya models register` |
| `rya_create_schedule` | Add a cron trigger | `rya schedules create` |
| `rya_list_channels` | Channels | `rya channels list` / `GET /channels` |
| `rya_connect_channel` | Enable a channel | `rya channels connect` |
| `rya_status` | Run/approval/job counts | `rya status` |

The tool logic lives in [src/rya/mcp/ops.py](../src/rya/mcp/ops.py) (plain,
unit-tested functions); [src/rya/mcp/server.py](../src/rya/mcp/server.py) is the
thin FastMCP wrapper.

## Skills (two modules, progressive disclosure)

```bash
rya skills install            # → ./.claude/skills/{rya,rya-ops}/SKILL.md
rya skills install --global   # → ~/.claude/skills/{rya,rya-ops}/SKILL.md
```

Split so only the matching module's body loads into context (the InsForge
pattern):

- **`rya`** ([skills/rya/SKILL.md](../skills/rya/SKILL.md)) — *authoring*: manifest,
  `define_agent()` handler, `ctx.*`, permission rules.
- **`rya-ops`** ([skills/rya-ops/SKILL.md](../skills/rya-ops/SKILL.md)) — *operating*:
  `rya context` first, the run/inspect/approve loop, `--json`/error codes, serve +
  webhooks + auth, multi-tenancy, MCP tool names.

## Context-first (token efficiency)

`rya context` / `rya_context` returns the entire live backend state — manifest,
tools + permissions, models, channels, handlers, recent runs, pending
approvals/jobs, active store + LLM backend, the invariants to respect, and the
suggested next actions — in **one call**, so a coding agent doesn't discover
state through trial and error.
