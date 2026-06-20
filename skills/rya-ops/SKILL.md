---
name: rya-ops
description: >-
  Run, inspect, debug, and deploy Rya agents from the terminal — rya
  context/dev/events/runs/approvals/serve, webhooks, auth, multi-tenancy, and the
  rya_* MCP tools. Use when triggering runs, handling approvals, reading traces,
  or deploying a Rya agent.
---

# Operating a Rya agent

## Always start here

```bash
rya context --json    # whole backend state in ONE call: manifest, tools, handlers,
                      # recent runs, pending approvals, store/llm backend, rules, next steps
```

`rya context` (MCP: `rya_context`) exists so you DON'T discover state by trial
and error. Read it first; it tells you the next action.

## The loop

```bash
rya dev --json                                   # validate manifest + agent code
rya events send --payload '{"email":"a@b.com"}' --json   # trigger a run
rya runs trace <run_id> --json                   # full durable trace
rya status --json                                # counts + active backend
```

## Reading output (CRITICAL)

- stdout of any `--json` command is ONE JSON object; first key is `ok`.
- Structured logs go to **stderr** — don't parse them as the result.
- On failure: `{"ok": false, "error": {"code": "E_*", "message", "hint", "exit_code"}}`.
  Read `hint`, fix, retry. Exit codes: 3=manifest, 4=not-found, 5=permission, 6=bad-state, 7=validation.

## Approvals (runs pause on them)

```bash
rya approvals list --status pending --json
rya approvals approve <approval_id> --json   # resume -> runStatus: completed
rya approvals reject  <approval_id> --json   # terminate -> runStatus: rejected
```

## Serve + webhooks (production)

```bash
export RYA_TOKEN=$(rya token --json | jq -r .token)   # require operator token
export RYA_WEBHOOK_SECRET=whsec                        # require signed webhooks
rya serve --port 8787
# POST /inbound (HMAC-signed) triggers a real run; control routes need the token.
```

Durable on Postgres: set `RYA_DATABASE_URL` and runs survive restarts.

## Multi-tenancy (Postgres)

```bash
rya workspaces create acme              # a tenant
rya keys create --workspace ws_… --json  # per-workspace API key (shown once)
# With RYA_MULTITENANT=1, callers send Authorization: Bearer rya_sk_… and are
# isolated by Postgres RLS.
```

## MCP tools

`rya_context` (call first), `rya_create_agent`, `rya_validate_manifest`,
`rya_trigger_event`, `rya_list_runs`, `rya_get_run_trace`, `rya_list_approvals`,
`rya_approve_action`, `rya_reject_action`, `rya_register_tool`,
`rya_register_model`, `rya_create_schedule`, `rya_connect_channel`, `rya_status`.
