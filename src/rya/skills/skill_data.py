"""Bundled Rya skills, written to disk by `rya skills install`.

Two modules (the InsForge pattern) so progressive disclosure keeps the agent's
context lean — only the matching skill's body loads:
  - ``rya``     — authoring: manifest + handler + ctx.*
  - ``rya-ops`` — operating: run/inspect/approve/deploy from the terminal
"""

AUTHORING_SKILL = '''\
---
name: rya
description: >-
  Write a Rya agent — the rya.agent.yaml manifest, the define_agent() handler,
  ctx.* (memory, tools, models, llm, approvals, jobs, channels), and tool
  permission rules. Use when creating or editing a Rya agent's code or manifest.
---

# Authoring a Rya agent

Rya gives the agent production infrastructure; you write the logic. Before
editing, read live state with `rya context --json` (see the `rya-ops` skill).

## Manifest (`rya.agent.yaml`)

Declare every tool with a permission: `read_only | allowed | approval_required | disabled`.

```yaml
name: my-agent
runtime: python
entrypoint: src/agent.py
memory:
  collections: [conversations]
tools:
  - id: crm.lookup
    permission: allowed
  - id: email.send
    permission: approval_required   # external side effect — gate it
models:
  - id: churn-risk-v1
    type: custom
    permission: allowed
triggers:
  - id: daily
    type: cron
    schedule: "0 9 * * *"
    handler: daily_followup
```

## Handler (`src/agent.py`)

```python
from rya import define_agent

agent = define_agent()

@agent.on_event
async def handle_event(ctx, event):
    await ctx.memory.append("conversations", {"event": event.model_dump()})
    customer = await ctx.tools.call("crm.lookup", {"email": event.payload["email"]})
    risk = await ctx.models.call("churn-risk-v1", {"customer_id": customer["id"]})
    if risk["score"] > 0.8:
        msg = await ctx.llm.respond(system="Draft a follow-up.", input={"customer": customer})
        result = await ctx.approvals.request(           # PAUSES the run
            title="Send follow-up", body=msg.text,
            action={"tool": "email.send", "input": {"to": customer["email"], "body": msg.text}},
        )
        await ctx.channels.send("email", {"messageId": result["actionResult"]["messageId"]})

@agent.job("daily_followup")
async def daily_followup(ctx, job):
    ...
```

`ctx` exposes: `llm, models, memory, tools, channels, jobs, cron, approvals,
logs, traces, secrets, events`.

## Invariants (or it breaks)

- An `approval_required` tool CANNOT be called via `ctx.tools.call` — gate it with
  `ctx.approvals.request(action={"tool": id, "input": {...}})`; it runs only after approval.
- `ctx.approvals.request` PAUSES the run; it resumes later by replaying the
  handler with prior steps memoized — so issue ctx operations in a deterministic order.
- Read secrets via `ctx.secrets.get(NAME)`; never hard-code them.
- `ctx.llm` is a real model when `ANTHROPIC_API_KEY` is set, a mock otherwise — same code.
'''

OPS_SKILL = '''\
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
'''

# name -> markdown
SKILLS = {"rya": AUTHORING_SKILL, "rya-ops": OPS_SKILL}

# Back-compat: the original single-skill export.
SKILL_MD = AUTHORING_SKILL
