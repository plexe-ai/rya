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
